"""租约 CAS 与 fencing（DESIGN §7、§7.1）。

从 persistence/store.py 拆出。包含：
- get_lease：按 (contract_id, partition_id) 查询当前活跃租约
- acquire_lease：CAS 获取租约（首次或换人）；generation+1
- reclaim_lease：心跳超时回收旧租约并交给新持有者
- renew_lease：代次心跳续约
- release_lease：终态后释放租约
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from longtask.persistence.errors import LeaseCASError, LeaseFencedError
from longtask.persistence.events import EventType
from longtask.persistence.events_query import append_event
from longtask.persistence.schema import (
    parse_partition_key,
    partition_key,
    transaction,
)
from longtask.persistence.types import StoredLease

# leases 表 7 列固定顺序：与 SELECT 配合使用避免列名拼写漂移
_LEASE_COLS = (
    "contract_id",
    "partition_id",
    "holder_attempt_id",
    "generation",
    "heartbeat_at",
    "timeout_seconds",
    "updated_at",
)
# 预拼好的 SELECT 列字符串（模块级常量，不接受外部输入）
_LEASE_COLS_LIST = ", ".join(_LEASE_COLS)


def _goal_id_for_contract(conn: sqlite3.Connection, contract_id: str) -> str:
    """Return the persistent Goal identity used by lease audit events."""
    row = conn.execute(
        "SELECT goal_id FROM contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()
    return str(row[0]) if row and row[0] else contract_id


def get_lease(
    conn: sqlite3.Connection,
    contract_id: str,
    partition_id: str | None = None,
) -> StoredLease | None:
    """查询租约（DESIGN §7、§7.1）。"""
    part_key = partition_key(partition_id)
    row = conn.execute(
        "SELECT " + _LEASE_COLS_LIST + " FROM leases WHERE contract_id = ? AND partition_id = ?",
        (contract_id, part_key),
    ).fetchone()
    if row is None:
        return None
    return StoredLease(
        contract_id=row[0],
        partition_id=parse_partition_key(row[1]),
        holder_attempt_id=row[2],
        generation=int(row[3]),
        heartbeat_at=datetime.fromisoformat(row[4]),
        timeout=timedelta(seconds=float(row[5])),
    )


def acquire_lease(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    holder_attempt_id: str,
    expected_generation: int,
    heartbeat_at: datetime,
    timeout: timedelta,
    partition_id: str | None = None,
    request_id: str | None = None,
    actor: str = "daemon",
    payload: dict[str, object] | None = None,
    schema_version: int = 2,
    role: str | None = None,  # P1
    contract_revision: int | None = None,  # P1
) -> StoredLease:
    """获取/抢占租约（CAS：expected_generation）（DESIGN §7、§7.1）。

    - expected_generation 必须与数据库内当前 generation 一致（首次获取 expected=0）；
    - 冲突抛 LeaseCASError，成功 generation+1 并追加 lease/acquired 事件；
    - 幂等：若 request_id 已存在，直接返回已存租约，不递增 generation。
    """
    part_key = partition_key(partition_id)

    with transaction(conn):
        if request_id:
            from longtask.persistence.events_query import get_events_by_request_id

            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                existing_lease = get_lease(conn, contract_id, partition_id)
                if existing_lease is not None:
                    return existing_lease

        current = get_lease(conn, contract_id, partition_id)
        current_gen = current.generation if current else 0

        if expected_generation != current_gen:
            raise LeaseCASError(
                f"acquire lease CAS failed on contract {contract_id} (partition '{partition_id}'): "
                f"expected generation {expected_generation}, actual {current_gen}"
            )

        new_generation = expected_generation + 1
        timeout_seconds = timeout.total_seconds()
        now_str = heartbeat_at.isoformat()

        conn.execute(
            """
            INSERT INTO leases (
                contract_id, partition_id, holder_attempt_id,
                generation, heartbeat_at, timeout_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (contract_id, partition_id) DO UPDATE SET
                holder_attempt_id = excluded.holder_attempt_id,
                generation = excluded.generation,
                heartbeat_at = excluded.heartbeat_at,
                timeout_seconds = excluded.timeout_seconds,
                updated_at = excluded.updated_at
            """,
            (
                contract_id,
                part_key,
                holder_attempt_id,
                new_generation,
                now_str,
                timeout_seconds,
                now_str,
            ),
        )

        event_body = {
            "actor": actor,
            "holder_attempt_id": holder_attempt_id,
            "generation": new_generation,
            "partition_id": partition_id,
            **(payload or {}),
        }
        append_event(
            conn,
            contract_id=contract_id,
            attempt_id=holder_attempt_id,
            lease_generation=new_generation,
            event_type=EventType.LEASE_ACQUIRED,
            payload=event_body,
            now=heartbeat_at,
            request_id=request_id,
            actor=actor,
            schema_version=schema_version,
            goal_id=_goal_id_for_contract(conn, contract_id),
            contract_revision=contract_revision,
            role=role or actor,
            payload_schema_version=schema_version,
        )

    lease = get_lease(conn, contract_id, partition_id)
    if lease is None:
        raise LeaseCASError(
            f"lease for contract {contract_id} could not be retrieved after acquire"
        )
    return lease


def reclaim_lease(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    expected_generation: int,
    heartbeat_at: datetime,
    timeout: timedelta,
    new_holder_attempt_id: str | None = None,
    partition_id: str | None = None,
    request_id: str | None = None,
    actor: str = "daemon",
    reason: str = "heartbeat timeout",
    payload: dict[str, object] | None = None,
    schema_version: int = 2,
    role: str | None = None,
    contract_revision: int | None = None,
) -> StoredLease:
    """回收心跳超时的旧租约并交给新持有者（DESIGN §7、§7.1）。

    - 与 acquire_lease 区别：保留 leases 行只换持有者与 generation（CAS 不变）；
    - 写 lease/reclaimed 事件而不是 lease/acquired；
    - 幂等：若 request_id 已存在，直接返回已存租约。
    """
    part_key = partition_key(partition_id)

    with transaction(conn):
        if request_id:
            from longtask.persistence.events_query import get_events_by_request_id

            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                existing_lease = get_lease(conn, contract_id, partition_id)
                if existing_lease is not None:
                    return existing_lease

        current = get_lease(conn, contract_id, partition_id)
        if current is None:
            raise LeaseCASError(
                f"reclaim lease on contract {contract_id} (partition '{partition_id}'): "
                "no lease to reclaim"
            )
        if current.generation != expected_generation:
            raise LeaseCASError(
                f"reclaim lease CAS failed on contract {contract_id} (partition '{partition_id}'): "
                f"expected generation {expected_generation}, actual {current.generation}"
            )

        new_generation = expected_generation + 1
        timeout_seconds = timeout.total_seconds()
        now_str = heartbeat_at.isoformat()

        conn.execute(
            """
            UPDATE leases
            SET holder_attempt_id = ?,
                generation = ?,
                heartbeat_at = ?,
                timeout_seconds = ?,
                updated_at = ?
            WHERE contract_id = ? AND partition_id = ?
            """,
            (
                new_holder_attempt_id,
                new_generation,
                now_str,
                timeout_seconds,
                now_str,
                contract_id,
                part_key,
            ),
        )

        event_body = {
            "actor": actor,
            "previous_holder_attempt_id": current.holder_attempt_id,
            "holder_attempt_id": new_holder_attempt_id,
            "generation": new_generation,
            "partition_id": partition_id,
            "reason": reason,
            **(payload or {}),
        }
        append_event(
            conn,
            contract_id=contract_id,
            attempt_id=new_holder_attempt_id,
            lease_generation=new_generation,
            event_type=EventType.LEASE_RECLAIMED,
            payload=event_body,
            now=heartbeat_at,
            request_id=request_id,
            actor=actor,
            schema_version=schema_version,
            goal_id=_goal_id_for_contract(conn, contract_id),
            contract_revision=contract_revision,
            role=role or actor,
            payload_schema_version=schema_version,
        )

    lease = get_lease(conn, contract_id, partition_id)
    if lease is None:
        raise LeaseCASError(
            f"lease for contract {contract_id} could not be retrieved after reclaim"
        )
    return lease


def renew_lease(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    holder_attempt_id: str,
    lease_generation: int,
    heartbeat_at: datetime,
    timeout: timedelta,
    partition_id: str | None = None,
    request_id: str | None = None,
    actor: str = "adapter",
    schema_version: int = 2,
    role: str | None = None,
    contract_revision: int | None = None,
) -> StoredLease:
    """心跳续约（DESIGN §7、§7.1）。

    - holder + generation 必须匹配，否则 LeaseFencedError；
    - 同一 request_id 重放直接返回当前租约（幂等）。
    """
    part_key = partition_key(partition_id)

    with transaction(conn):
        if request_id:
            from longtask.persistence.events_query import get_events_by_request_id

            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                existing_lease = get_lease(conn, contract_id, partition_id)
                if existing_lease is not None:
                    return existing_lease

        current = get_lease(conn, contract_id, partition_id)
        if current is None:
            raise LeaseFencedError(
                f"renew lease on contract {contract_id} (partition '{partition_id}'): "
                "no active lease"
            )
        if current.generation != lease_generation or current.holder_attempt_id != holder_attempt_id:
            raise LeaseFencedError(
                f"renew lease fenced on contract {contract_id}: "
                f"expected holder={holder_attempt_id}/gen={lease_generation}, "
                f"actual holder={current.holder_attempt_id}/gen={current.generation}"
            )

        timeout_seconds = timeout.total_seconds()
        now_str = heartbeat_at.isoformat()

        conn.execute(
            """
            UPDATE leases
            SET heartbeat_at = ?,
                timeout_seconds = ?,
                updated_at = ?
            WHERE contract_id = ? AND partition_id = ?
            """,
            (
                now_str,
                timeout_seconds,
                now_str,
                contract_id,
                part_key,
            ),
        )

        append_event(
            conn,
            contract_id=contract_id,
            attempt_id=holder_attempt_id,
            lease_generation=lease_generation,
            event_type=EventType.LEASE_RENEWED,
            payload={
                "actor": actor,
                "holder_attempt_id": holder_attempt_id,
                "generation": lease_generation,
                "partition_id": partition_id,
            },
            now=heartbeat_at,
            request_id=request_id,
            actor=actor,
            schema_version=schema_version,
            goal_id=_goal_id_for_contract(conn, contract_id),
            contract_revision=contract_revision,
            role=role or actor,
            payload_schema_version=schema_version,
        )

    lease = get_lease(conn, contract_id, partition_id)
    if lease is None:
        raise LeaseFencedError(
            f"lease for contract {contract_id} could not be retrieved after renew"
        )
    return lease


def release_lease(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    holder_attempt_id: str,
    lease_generation: int,
    now: datetime,
    partition_id: str | None = None,
    request_id: str | None = None,
    actor: str = "adapter",
    schema_version: int = 2,
    role: str | None = None,
    contract_revision: int | None = None,
) -> None:
    """释放租约（DESIGN §7）。

    - 校验 generation 与 holder，不符抛 LeaseFencedError；
    - 成功删除租约行并追加 lease/released 事件；
    - 幂等：若 request_id 已存在，直接返回。
    """
    part_key = partition_key(partition_id)

    with transaction(conn):
        if request_id:
            from longtask.persistence.events_query import get_events_by_request_id

            existing_events = get_events_by_request_id(conn, request_id)
            if existing_events:
                return

        current = get_lease(conn, contract_id, partition_id)
        if current is None:
            return
        if current.generation != lease_generation:
            raise LeaseFencedError(
                f"release lease fenced on contract {contract_id}: "
                f"expected generation {lease_generation}, actual {current.generation}"
            )
        if current.holder_attempt_id != holder_attempt_id:
            raise LeaseFencedError(
                f"release lease fenced on contract {contract_id}: "
                f"expected holder {holder_attempt_id}, actual {current.holder_attempt_id}"
            )

        conn.execute(
            "DELETE FROM leases WHERE contract_id = ? AND partition_id = ?",
            (contract_id, part_key),
        )

        append_event(
            conn,
            contract_id=contract_id,
            attempt_id=holder_attempt_id,
            lease_generation=lease_generation,
            event_type=EventType.LEASE_RELEASED,
            payload={
                "actor": actor,
                "holder_attempt_id": holder_attempt_id,
                "generation": lease_generation,
                "partition_id": partition_id,
            },
            now=now,
            request_id=request_id,
            actor=actor,
            schema_version=schema_version,
            goal_id=_goal_id_for_contract(conn, contract_id),
            contract_revision=contract_revision,
            role=role or actor,
            payload_schema_version=schema_version,
        )


__all__ = [
    "acquire_lease",
    "get_lease",
    "reclaim_lease",
    "release_lease",
    "renew_lease",
]
