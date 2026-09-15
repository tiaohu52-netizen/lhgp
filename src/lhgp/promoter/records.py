"""Canonical persistence helpers for promoter attempts and decisions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from lhgp.persistence.events import EventType
from lhgp.promoter.urgency import UrgencyTier


def _record_attempt(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    contract_id: str | None = None,
    attempt_id: str,
    contract_revision: int,
    role: str,
    executor_id: str | None,
    model_id: str | None = None,
    state: str,
    admitted_at: datetime,
    started_at: datetime | None = None,
    terminal_at: datetime | None = None,
    return_code: int | None = None,
    error_class: str | None = None,
    payload: dict[str, Any] | None = None,
    updated_at: datetime,
) -> None:
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    conn.execute(
        """INSERT INTO attempts (
        attempt_id, goal_id, contract_id, contract_revision, role, executor_id, model_id, state,
        lease_generation, partition_id, admitted_at, started_at, terminal_at,
        return_code, error_class, payload_json, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (attempt_id) DO UPDATE SET state=excluded.state,
      lease_generation=excluded.lease_generation,
      model_id=COALESCE(excluded.model_id, attempts.model_id),
      started_at=COALESCE(excluded.started_at, attempts.started_at),
      terminal_at=excluded.terminal_at,
      return_code=excluded.return_code, error_class=excluded.error_class,
      payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
        (
            attempt_id,
            goal_id,
            contract_id,
            contract_revision,
            role,
            executor_id,
            model_id,
            state,
            admitted_at.isoformat(),
            started_at.isoformat() if started_at else None,
            terminal_at.isoformat() if terminal_at else None,
            return_code,
            error_class,
            payload_json,
            updated_at.isoformat(),
        ),
    )


def _record_decision(
    conn: sqlite3.Connection,
    *,
    goal_id: str,
    contract_id: str | None = None,
    contract_revision: int,
    tier: UrgencyTier | None,
    decision_type: str,
    reason: str,
    budget_dispatches_left: int,
    budget_escalations_left: int,
    now: datetime,
    actor: str,
) -> None:
    conn.execute(
        """INSERT INTO decisions (
        goal_id, contract_id, contract_revision, tier, decision_type, reason,
        budget_dispatches_left, budget_escalations_left, payload_json, recorded_at, actor
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
        (
            goal_id,
            contract_id,
            contract_revision,
            int(tier) if tier is not None else None,
            decision_type,
            reason,
            budget_dispatches_left,
            budget_escalations_left,
            now.isoformat(),
            actor,
        ),
    )


def _goal_id_for_contract(conn: sqlite3.Connection, contract_id: str) -> str:
    """Resolve the owning Goal identity for a contract-bound attempt query."""
    row = conn.execute(
        "SELECT goal_id FROM contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()
    return str(row[0]) if row and row[0] else contract_id


def _count_verifier_attempts(conn: sqlite3.Connection, contract_id: str) -> int:
    row = conn.execute(
        """SELECT COUNT(*) FROM attempts WHERE contract_id = ?
        AND role = 'verifier' AND state IN ('succeeded','failed','cancelled','stale','orphaned')""",
        (contract_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _estimate_stalled_from_attempts(conn: sqlite3.Connection, contract_id: str) -> bool:
    goal_id = _goal_id_for_contract(conn, contract_id)
    rows = conn.execute(
        """SELECT state, admitted_at, contract_revision, role FROM attempts
        WHERE goal_id = ? ORDER BY admitted_at DESC LIMIT 4""",
        (goal_id,),
    ).fetchall()
    recent = [row for row in rows if row[3] == "executor"]
    if (
        len(recent) < 2
        or recent[0][0] not in ("running", "admitted")
        or recent[1][0] in ("succeeded", "cancelled")
    ):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM attempts WHERE goal_id = ? AND role = 'verifier' LIMIT 1", (goal_id,)
        ).fetchone()
        is None
    )


def _last_event_at(
    conn: sqlite3.Connection, contract_id: str, event_type: EventType
) -> datetime | None:
    row = conn.execute(
        """SELECT created_at FROM events WHERE contract_id = ? AND event_type = ?
        ORDER BY event_id DESC LIMIT 1""",
        (contract_id, event_type.value),
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def _last_attempt_started_at(conn: sqlite3.Connection, contract_id: str) -> datetime | None:
    return _last_event_at(conn, contract_id, EventType.ATTEMPT_STARTED)


__all__ = [
    "_count_verifier_attempts",
    "_estimate_stalled_from_attempts",
    "_last_attempt_started_at",
    "_last_event_at",
    "_record_attempt",
    "_record_decision",
]
