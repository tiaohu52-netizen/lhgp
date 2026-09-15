"""至少一次通知 outbox（SPEC §10.5）。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from longtask.persistence.schema import transaction


@dataclass(frozen=True, slots=True)
class Notification:
    notification_id: int
    idempotency_key: str
    goal_id: str | None
    event_type: str
    channel: str
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: datetime
    lease_until: datetime | None
    last_error: str | None


def enqueue_notification(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    event_type: str,
    channel: str,
    payload: dict[str, Any],
    now: datetime,
    goal_id: str | None = None,
    available_at: datetime | None = None,
) -> Notification:
    """入队；相同幂等键重复调用返回原记录，不产生第二条通知。"""

    if not idempotency_key.strip() or not event_type.strip() or not channel.strip():
        raise ValueError("idempotency_key, event_type and channel must be non-empty")
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO notification_outbox
              (idempotency_key, goal_id, event_type, channel, payload_json,
               available_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                idempotency_key,
                goal_id,
                event_type,
                channel,
                json.dumps(payload, ensure_ascii=False),
                (available_at or now).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
    return get_by_key(conn, idempotency_key)  # type: ignore[return-value]


def _row(row: sqlite3.Row | tuple[Any, ...]) -> Notification:
    values = tuple(row)
    return Notification(
        notification_id=int(values[0]),
        idempotency_key=str(values[1]),
        goal_id=values[2],
        event_type=str(values[3]),
        channel=str(values[4]),
        payload=json.loads(values[5]),
        status=str(values[6]),
        attempts=int(values[7]),
        available_at=datetime.fromisoformat(values[8]),
        lease_until=datetime.fromisoformat(values[9]) if values[9] else None,
        last_error=values[10],
    )


def get_by_key(conn: sqlite3.Connection, key: str) -> Notification | None:
    row = conn.execute(
        "SELECT notification_id, idempotency_key, goal_id, event_type, channel, "
        "payload_json, status, attempts, available_at, lease_until, last_error "
        "FROM notification_outbox WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    return _row(row) if row else None


def list_notifications(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    goal_id: str | None = None,
    limit: int = 50,
) -> list[Notification]:
    """只读列出通知，供控制面审计与 MCP 查看。"""
    if limit <= 0:
        raise ValueError("limit must be positive")
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in {"pending", "leased", "sent"}:
            raise ValueError(f"unknown notification status: {status}")
        clauses.append("status = ?")
        params.append(status)
    if goal_id is not None:
        if not goal_id.strip():
            raise ValueError("goal_id must be non-empty when provided")
        clauses.append("goal_id = ?")
        params.append(goal_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT notification_id, idempotency_key, goal_id, event_type, channel, "
        "payload_json, status, attempts, available_at, lease_until, last_error "
        f"FROM notification_outbox{where} ORDER BY notification_id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row(row) for row in rows]


def claim_notifications(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    lease_seconds: int = 300,
    limit: int = 20,
) -> list[Notification]:
    """原子领取到期通知；崩溃遗留 lease 会自动重新可领取。"""

    if lease_seconds <= 0 or limit <= 0:
        raise ValueError("lease_seconds and limit must be positive")
    now_text = now.isoformat()
    lease_until = now + timedelta(seconds=lease_seconds)
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT notification_id FROM notification_outbox
            WHERE (status = 'pending' AND available_at <= ?)
               OR (status = 'leased' AND lease_until <= ?)
            ORDER BY available_at, notification_id LIMIT ?
            """,
            (now_text, now_text, limit),
        ).fetchall()
        ids = [int(row[0]) for row in rows]
        for notification_id in ids:
            conn.execute(
                "UPDATE notification_outbox SET status='leased', attempts=attempts+1, "
                "lease_until=?, updated_at=? WHERE notification_id=?",
                (lease_until.isoformat(), now_text, notification_id),
            )
    return list_by_ids(conn, ids)


def list_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[Notification]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT notification_id, idempotency_key, goal_id, event_type, channel, "
        "payload_json, status, attempts, available_at, lease_until, last_error "
        f"FROM notification_outbox WHERE notification_id IN ({placeholders}) "
        "ORDER BY notification_id",
        ids,
    ).fetchall()
    return [_row(row) for row in rows]


def mark_sent(conn: sqlite3.Connection, *, notification_id: int, now: datetime) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE notification_outbox SET status='sent', lease_until=NULL, updated_at=? "
            "WHERE notification_id=? AND status='leased'",
            (now.isoformat(), notification_id),
        )


def mark_failed(
    conn: sqlite3.Connection,
    *,
    notification_id: int,
    now: datetime,
    error: str,
    retry_at: datetime,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE notification_outbox SET status='pending', lease_until=NULL, "
            "available_at=?, last_error=?, updated_at=? "
            "WHERE notification_id=? AND status='leased'",
            (retry_at.isoformat(), error, now.isoformat(), notification_id),
        )


def prune_sent(
    conn: sqlite3.Connection,
    *,
    before: datetime,
    keep_latest: int = 1000,
) -> int:
    """删除过旧的 sent 记录，保留最近 ``keep_latest`` 条审计窗口。"""
    if keep_latest < 0:
        raise ValueError("keep_latest must be non-negative")
    with transaction(conn):
        conn.execute(
            """
            DELETE FROM notification_outbox
            WHERE status = 'sent'
              AND updated_at < ?
              AND notification_id NOT IN (
                SELECT notification_id FROM notification_outbox
                WHERE status = 'sent' ORDER BY notification_id DESC LIMIT ?
              )
            """,
            (before.isoformat(), keep_latest),
        )
        return int(conn.execute("SELECT changes()").fetchone()[0])


def drain_notifications(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    deliver: Callable[[Notification], None],
    limit: int = 20,
    retry_delay_seconds: int = 60,
) -> int:
    """领取并投递一批通知；返回成功发送数量。

    ``deliver`` 是渠道适配器，抛异常表示渠道未接受，通知会回到 pending。
    outbox 本身提供至少一次语义，因此渠道必须使用 notification.idempotency_key
    去重；本函数不吞掉失败事实。
    """

    claimed = claim_notifications(conn, now=now, limit=limit)
    sent = 0
    for notification in claimed:
        try:
            deliver(notification)
        except Exception as exc:  # channel failures are retryable facts
            mark_failed(
                conn,
                notification_id=notification.notification_id,
                now=now,
                error=str(exc),
                retry_at=now + timedelta(seconds=retry_delay_seconds),
            )
        else:
            mark_sent(conn, notification_id=notification.notification_id, now=now)
            sent += 1
    return sent


__all__ = [
    "Notification",
    "claim_notifications",
    "drain_notifications",
    "enqueue_notification",
    "get_by_key",
    "list_notifications",
    "mark_failed",
    "mark_sent",
    "prune_sent",
]
