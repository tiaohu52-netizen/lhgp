"""attempts 实体表读写（SPEC §7.4 attempt 轴、§11.3 持久外部句柄）。

从 store.py 拆出：本模块只管 attempt 行，不碰合同/租约/事件。
方向遵循架构分层（persistence → contracts），零上层依赖。

外部句柄的落库是 §11.3 的硬要求——「只把 subprocess.Popen 存在内存中」
不满足跨守护进程重启的连续性，reconcile 无从判定外部 run 的死活。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from longtask.contracts.state_machine import ATTEMPT_TERMINAL_STATES

# 需要 reconcile 关注的 attempt 状态：终态之外全部（orphaned 也在内——
# 它要在宽限期后 fence 并让位给新 attempt）。
RECONCILABLE_STATES: tuple[str, ...] = ("admitted", "starting", "running", "waiting", "orphaned")

_ATTEMPT_COLS = (
    "attempt_id",
    "goal_id",
    "contract_id",
    "contract_revision",
    "role",
    "executor_id",
    "model_id",
    "state",
    "lease_generation",
    "admitted_at",
    "started_at",
    "terminal_at",
    "return_code",
    "error_class",
    "payload_json",
    "external_run_id",
    "session_locator",
    "recovery_strategy",
    "process_identity_json",
    "capability_snapshot_json",
    "handle_registered_at",
    "orphaned_at",
    "usage_json",
)
_COLS_SQL = ", ".join(_ATTEMPT_COLS)


@dataclass(frozen=True, slots=True)
class StoredAttempt:
    """attempts 表一行的只读视图（§7.4 + §11.3 句柄）。"""

    attempt_id: str
    goal_id: str
    contract_revision: int
    role: str
    executor_id: str | None
    model_id: str | None
    state: str
    lease_generation: int | None
    admitted_at: datetime
    started_at: datetime | None
    terminal_at: datetime | None
    return_code: int | None
    error_class: str | None
    payload: dict[str, Any]
    external_run_id: str | None
    session_locator: str | None
    recovery_strategy: str | None
    process_identity: dict[str, Any]
    capability_snapshot: dict[str, Any]
    handle_registered_at: datetime | None
    orphaned_at: datetime | None
    usage: dict[str, Any]
    contract_id: str | None = None

    def is_terminal(self) -> bool:
        return self.state in {s.value for s in ATTEMPT_TERMINAL_STATES}


def _row_to_attempt(data: dict[str, Any]) -> StoredAttempt:
    def _ts(key: str) -> datetime | None:
        raw = data.get(key)
        return datetime.fromisoformat(raw) if raw else None

    def _json_obj(key: str) -> dict[str, Any]:
        raw = data.get(key)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    admitted_raw = data["admitted_at"]
    return StoredAttempt(
        attempt_id=str(data["attempt_id"]),
        goal_id=str(data["goal_id"]),
        contract_revision=int(data["contract_revision"]),
        role=str(data["role"]),
        executor_id=data["executor_id"],
        model_id=data.get("model_id"),
        state=str(data["state"]),
        lease_generation=data["lease_generation"],
        admitted_at=datetime.fromisoformat(admitted_raw),
        started_at=_ts("started_at"),
        terminal_at=_ts("terminal_at"),
        return_code=data["return_code"],
        error_class=data["error_class"],
        payload=_json_obj("payload_json"),
        external_run_id=data["external_run_id"],
        session_locator=data["session_locator"],
        recovery_strategy=data["recovery_strategy"],
        process_identity=_json_obj("process_identity_json"),
        capability_snapshot=_json_obj("capability_snapshot_json"),
        handle_registered_at=_ts("handle_registered_at"),
        orphaned_at=_ts("orphaned_at"),
        usage=_json_obj("usage_json"),
        contract_id=str(data["contract_id"]) if data.get("contract_id") else None,
    )


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    """按列名配对的查询结果。

    显式用 cursor.description 组装 dict，不依赖连接的 row_factory
    （调用方可能另有设置），本模块因此可独立使用。
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        names = [str(desc[0]) for desc in (cursor.description or ())]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> StoredAttempt | None:
    """按主键读一行 attempt；不存在返回 None。"""
    rows = _fetch_all(
        conn,
        "SELECT " + _COLS_SQL + " FROM attempts WHERE attempt_id = ?",
        (attempt_id,),
    )
    if not rows:
        return None
    return _row_to_attempt(rows[0])


def list_contract_attempts(
    conn: sqlite3.Connection, *, contract_id: str, limit: int = 20
) -> list[StoredAttempt]:
    """列出单合同 attempt，按最近 admission 倒序。"""
    bounded_limit = max(1, min(int(limit), 100))
    rows = _fetch_all(
        conn,
        "SELECT " + _COLS_SQL + " FROM attempts WHERE contract_id = ? "
        "ORDER BY admitted_at DESC, attempt_id DESC LIMIT ?",
        (contract_id, bounded_limit),
    )
    return [_row_to_attempt(row) for row in rows]


def count_running_by_executor(conn: sqlite3.Connection) -> dict[str, int]:
    """按执行器统计未终态 attempt 数（并发限额准入，DESIGN §8.2 条件 4）。

    审计调度-R3：match_candidates 的 running_attempts 入参在两条生产派发
    路径上都未注入，合同/执行器的 max_concurrent_attempts 从未真正生效。
    """
    rows = conn.execute(
        "SELECT executor_id, COUNT(*) FROM attempts"
        " WHERE state IN ('admitted', 'running', 'orphaned')"
        " AND executor_id IS NOT NULL GROUP BY executor_id"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def list_reconcilable_attempts(conn: sqlite3.Connection) -> list[StoredAttempt]:
    """列出所有非终态 attempt（reconcile 的扫描集，§9 步骤 2 / §11.3）。

    含 orphaned：它还欠一次宽限到期后的 fence，跳过就永远没人收尾。
    """
    placeholders = ", ".join("?" for _ in RECONCILABLE_STATES)
    rows = _fetch_all(
        conn,
        "SELECT " + _COLS_SQL + " FROM attempts WHERE state IN (" + placeholders + ")"
        " ORDER BY admitted_at, attempt_id",
        tuple(RECONCILABLE_STATES),
    )
    return [_row_to_attempt(row) for row in rows]


def register_attempt_handle(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    external_run_id: str,
    session_locator: str,
    recovery_strategy: str,
    process_identity: dict[str, Any] | None = None,
    capability_snapshot: dict[str, Any] | None = None,
    now: datetime,
) -> None:
    """把 spawn 返回的外部句柄写进 attempt 行（§11.3 MUST 持久返回）。

    形参是句柄的四个扁平字段而不是句柄对象：handle 类型属于 adapters 协议
    面，依赖方向只能是 adapters → persistence，本模块不许反向 import
    （arch 门强制）。调用方（执行桥接层）拆字段传入即可。

    只更新句柄列：state 由 set_attempt_state 单独负责，避免两处写状态。
    """
    conn.execute(
        """
        UPDATE attempts
        SET external_run_id = ?,
            session_locator = ?,
            recovery_strategy = ?,
            process_identity_json = ?,
            capability_snapshot_json = ?,
            handle_registered_at = ?,
            updated_at = ?
        WHERE attempt_id = ?
        """,
        (
            external_run_id,
            session_locator,
            recovery_strategy,
            json.dumps(process_identity or {}, ensure_ascii=False),
            json.dumps(capability_snapshot or {}, ensure_ascii=False),
            now.isoformat(),
            now.isoformat(),
            attempt_id,
        ),
    )


def set_attempt_state(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    state: str,
    now: datetime,
    error_class: str | None = None,
    return_code: int | None = None,
    clear_orphaned_at: bool = False,
) -> None:
    """更新 attempt 状态（§7.4 轴）。终态写入 terminal_at，非终态清空。

    不在此处校验迁移合法性：状态机是纯函数（state_machine.py），
    调用方负责先判定；本模块只做忠实落库。
    """
    terminal = state in {s.value for s in ATTEMPT_TERMINAL_STATES}
    conn.execute(
        """
        UPDATE attempts
        SET state = ?,
            started_at = CASE
                WHEN ? = 'running' THEN COALESCE(started_at, ?)
                ELSE started_at
            END,
            terminal_at = ?,
            error_class = COALESCE(?, error_class),
            return_code = COALESCE(?, return_code),
            orphaned_at = CASE WHEN ? THEN NULL ELSE orphaned_at END,
            updated_at = ?
        WHERE attempt_id = ?
        """,
        (
            state,
            state,
            now.isoformat(),
            now.isoformat() if terminal else None,
            error_class,
            return_code,
            clear_orphaned_at,
            now.isoformat(),
            attempt_id,
        ),
    )


def mark_attempt_orphaned(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    now: datetime,
) -> None:
    """标记 orphaned 并起算宽限（§11.3 分支 3）。

    幂等：已有 orphaned_at 时不覆盖——重扫不能把宽限期无限续期，
    否则「宽限后 fence 并重新派发」永远等不到。
    """
    conn.execute(
        """
        UPDATE attempts
        SET state = 'orphaned',
            orphaned_at = COALESCE(orphaned_at, ?),
            updated_at = ?
        WHERE attempt_id = ?
        """,
        (now.isoformat(), now.isoformat(), attempt_id),
    )


__all__ = [
    "RECONCILABLE_STATES",
    "StoredAttempt",
    "get_attempt",
    "list_contract_attempts",
    "list_reconcilable_attempts",
    "mark_attempt_orphaned",
    "register_attempt_handle",
    "set_attempt_state",
]
