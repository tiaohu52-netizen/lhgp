"""P6: per-contract trace log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lhgp.feedback.store import get_latest_diff, list_evaluations


@dataclass(frozen=True, slots=True)
class TraceEntry:
    event_id: int
    event_type: str
    occurred_at: str
    actor: str
    payload: dict[str, Any]


def _latest_diff_row(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    contract_revision: int | None = None,
) -> dict[str, Any] | None:
    diff = get_latest_diff(conn, contract_id, contract_revision=contract_revision)
    return diff.to_db_row() if diff is not None else None


def trace_contract(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    contract_revision: int | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Return the full timeline for a contract.

    Includes: the most recent user evaluation, the most recent
    acceptance diff (if any), and the events stream in chronological
    order. The MCP / CLI use this for the ``trace`` command.
    """
    clauses = ["contract_id = ?"]
    params: list[Any] = [contract_id]
    if contract_revision is not None:
        clauses.append("contract_revision = ?")
        params.append(int(contract_revision))
    where_sql = " AND ".join(clauses)
    params.append(int(limit))

    sql = (
        "SELECT event_id, event_type, created_at, actor, payload_json, "  # noqa: S608
        "contract_revision, attempt_id "
        "FROM events "
        f"WHERE {where_sql} "
        "ORDER BY created_at ASC, event_id ASC "
        "LIMIT ?"
    )
    rows = conn.execute(sql, params).fetchall()

    entries: list[TraceEntry] = []
    for r in rows:
        try:
            payload = json.loads(r[4]) if r[4] else {}
        except json.JSONDecodeError:
            payload = {"_raw": r[4]}
        entries.append(
            TraceEntry(
                event_id=int(r[0]),
                event_type=str(r[1]),
                occurred_at=str(r[2]),
                actor=str(r[3] or ""),
                payload=payload,
            )
        )

    return {
        "contract_id": contract_id,
        "contract_revision": contract_revision,
        "events_total": len(entries),
        "entries": [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at,
                "actor": e.actor,
                "payload": e.payload,
            }
            for e in entries
        ],
        "latest_user_evaluation": (
            list_evaluations(conn, contract_id=contract_id, limit=1)[0].to_db_row()
            if list_evaluations(conn, contract_id=contract_id, limit=1)
            else None
        ),
        "latest_acceptance_diff": _latest_diff_row(
            conn,
            contract_id,
            contract_revision=contract_revision,
        ),
        "generated_at": datetime.now(UTC).isoformat(),
    }
