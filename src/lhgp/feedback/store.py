"""P6: SQLite CRUD for user_evaluations and acceptance_diffs.

Both tables are populated by :mod:`lhgp.feedback` callers (CLI / MCP / webui)
and read by :mod:`lhgp.learning` to extract improvement signals.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lhgp.feedback.types import AcceptanceDiff, UserEvaluation


def record_evaluation(conn: sqlite3.Connection, evaluation: UserEvaluation) -> int:
    """Insert a new user evaluation; return the assigned evaluation_id."""
    row = evaluation.to_db_row()
    row.pop("evaluation_id", None)
    cur = conn.execute(
        """
        INSERT INTO user_evaluations (
            contract_id, contract_revision, attempt_id, evaluator,
            rating, verdict, comments, created_at, schema_version
        ) VALUES (
            :contract_id, :contract_revision, :attempt_id, :evaluator,
            :rating, :verdict, :comments, :created_at, 3
        )
        """,
        row,
    )
    lastrowid = cur.lastrowid
    if lastrowid is None:
        raise RuntimeError("sqlite cursor returned no lastrowid for evaluation insert")
    return int(lastrowid)


def list_evaluations(
    conn: sqlite3.Connection,
    *,
    contract_id: str | None = None,
    verdict: str | None = None,
    limit: int = 100,
) -> list[UserEvaluation]:
    """List user evaluations, newest first. Filterable by contract or verdict."""
    clauses: list[str] = []
    params: list[Any] = []
    if contract_id is not None:
        clauses.append("contract_id = ?")
        params.append(contract_id)
    if verdict is not None:
        clauses.append("verdict = ?")
        params.append(verdict)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT evaluation_id, contract_id, contract_revision, attempt_id, "  # noqa: S608
        "evaluator, rating, verdict, comments, created_at "
        "FROM user_evaluations "
        f"{where_sql} "
        "ORDER BY created_at DESC, evaluation_id DESC "
        "LIMIT ?"
    )
    params.append(int(limit))
    return [UserEvaluation.from_db_row(r) for r in conn.execute(sql, params).fetchall()]


def record_diff(conn: sqlite3.Connection, diff: AcceptanceDiff) -> int:
    """Insert a new acceptance diff; return the assigned diff_id."""
    row = diff.to_db_row()
    row.pop("diff_id", None)
    cur = conn.execute(
        """
        INSERT INTO acceptance_diffs (
            contract_id, contract_revision, attempt_id,
            snapshot_before_json, snapshot_after_json,
            files_changed_json, summary, computed_at, schema_version
        ) VALUES (
            :contract_id, :contract_revision, :attempt_id,
            :snapshot_before_json, :snapshot_after_json,
            :files_changed_json, :summary, :computed_at, 3
        )
        """,
        row,
    )
    lastrowid = cur.lastrowid
    if lastrowid is None:
        raise RuntimeError("sqlite cursor returned no lastrowid for diff insert")
    return int(lastrowid)


def get_latest_diff(
    conn: sqlite3.Connection,
    contract_id: str,
    contract_revision: int | None = None,
) -> AcceptanceDiff | None:
    """Return the most recent :class:`AcceptanceDiff` for a contract."""
    clauses = ["contract_id = ?"]
    params: list[Any] = [contract_id]
    if contract_revision is not None:
        clauses.append("contract_revision = ?")
        params.append(int(contract_revision))
    where_sql = " AND ".join(clauses)
    sql = (
        "SELECT diff_id, contract_id, contract_revision, attempt_id, "  # noqa: S608
        "snapshot_before_json, snapshot_after_json, files_changed_json, "
        "summary, computed_at "
        "FROM acceptance_diffs "
        f"WHERE {where_sql} "
        "ORDER BY computed_at DESC, diff_id DESC "
        "LIMIT 1"
    )
    row = conn.execute(sql, params).fetchone()
    return AcceptanceDiff.from_db_row(row) if row else None


def list_diffs(
    conn: sqlite3.Connection,
    contract_id: str | None = None,
    limit: int = 100,
) -> list[AcceptanceDiff]:
    clauses: list[str] = []
    params: list[Any] = []
    if contract_id is not None:
        clauses.append("contract_id = ?")
        params.append(contract_id)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT diff_id, contract_id, contract_revision, attempt_id, "  # noqa: S608
        "snapshot_before_json, snapshot_after_json, files_changed_json, "
        "summary, computed_at "
        "FROM acceptance_diffs "
        f"{where_sql} "
        "ORDER BY computed_at DESC, diff_id DESC "
        "LIMIT ?"
    )
    params.append(int(limit))
    return [AcceptanceDiff.from_db_row(r) for r in conn.execute(sql, params).fetchall()]


def attach_evaluation_event(
    conn: sqlite3.Connection,
    event_id: int,
    evaluation_id: int,
) -> None:
    """Link a recorded event (user/evaluation-submitted) to the new row.

    Uses the existing events table's payload convention: the event's
    payload_json carries a ``{"evaluation_id": N}`` reference. We do not
    add a foreign-key column to keep the v3 migration additive.
    """
    # No-op by design: callers build the event payload themselves via
    # EventInput(payload={"evaluation_id": ...}). This helper exists so
    # future schema work has a single place to add the link.
    del conn, event_id, evaluation_id
