"""Persistence operations for ``next_decision_at`` bookkeeping."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event
from lhgp.persistence.schema import transaction


def set_next_decision_at(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    when: datetime,
    now: datetime,
    reason: str,
    goal_id: str | None = None,
    contract_revision: int | None = None,
) -> bool:
    # 读-判-写+事件包进同一事务（审计持久化-R4）：两个并发调用都能通过
    # 「值未变」检查后各写一次事件的竞态，BEGIN IMMEDIATE 下不可能发生。
    with transaction(conn):
        row = conn.execute(
            "SELECT next_decision_at FROM contracts WHERE contract_id = ?", (contract_id,)
        ).fetchone()
        if row is None:
            return False
        new_value = when.isoformat()
        if row[0] == new_value:
            return False
        conn.execute(
            "UPDATE contracts SET next_decision_at = ?, updated_at = ? WHERE contract_id = ?",
            (new_value, now.isoformat(), contract_id),
        )
        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.NEXT_DECISION_AT_SET,
            payload={"at": new_value, "reason": reason},
            now=now,
            actor="daemon",
            goal_id=goal_id,
            contract_revision=contract_revision,
            role="promoter",
        )
    return True


def earliest_next_decision_at(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    states: tuple[str, ...] = ("active", "blocked"),
) -> datetime | None:
    placeholders = ", ".join("?" for _ in states)
    row = conn.execute(
        f"SELECT MIN(next_decision_at) FROM contracts WHERE state IN ({placeholders})",  # noqa: S608
        tuple(states),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    value = datetime.fromisoformat(row[0])
    # 过期的决策点必须原样返回（调用方钳到 0 立即唤醒），而不是返回
    # None——None 会让 daemon 回退到整周期休眠，deadline 决策反而被
    # 一个早已过期的 blocked 合同拖到下个周期才处理（审查 R1）。
    return value


def list_decisions(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the immutable decision history for one contract.

    The contract binding is deliberate: decisions from another revision or a
    newer contract under the same Goal must never leak into this view.  Older
    databases may contain rows without ``contract_id``; those rows are kept out
    of this contract-scoped API because ownership cannot be proven safely.
    """
    bounded_limit = max(1, min(int(limit), 200))
    rows = conn.execute(
        """
        SELECT decision_id, contract_id, goal_id, contract_revision, tier,
               decision_type, reason, budget_dispatches_left,
               budget_escalations_left, payload_json, recorded_at, actor
        FROM decisions
        WHERE contract_id = ?
        ORDER BY recorded_at DESC, decision_id DESC
        LIMIT ?
        """,
        (contract_id, bounded_limit),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "decision_id": int(row[0]),
                "contract_id": row[1],
                "goal_id": row[2],
                "contract_revision": int(row[3]),
                "tier": int(row[4]) if row[4] is not None else None,
                "decision_type": row[5],
                "reason": row[6],
                "budget_dispatches_left": row[7],
                "budget_escalations_left": row[8],
                "payload": _decode_payload(row[9]),
                "recorded_at": row[10],
                "actor": row[11],
            }
        )
    return result


def _decode_payload(raw: str | None) -> dict[str, Any]:
    import json

    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["earliest_next_decision_at", "list_decisions", "set_next_decision_at"]
