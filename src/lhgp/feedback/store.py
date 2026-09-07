"""P6: SQLite CRUD for user_evaluations and acceptance_diffs.

Both tables are populated by :mod:`lhgp.feedback` callers (CLI / MCP / webui)
and read by :mod:`lhgp.learning` to extract improvement signals.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lhgp.feedback.types import AcceptanceDiff, EvaluationVerdict, UserEvaluation


def _maybe_record_memory_from_evaluation(
    conn: sqlite3.Connection,
    evaluation: UserEvaluation,
    evaluation_id: int,
) -> None:
    """P6+1 / memory-and-wiki Phase 2: auto-mine a long-term memory from a
    user evaluation. Rating >= 4 with comments (or REJECT with comments) is
    a signal worth keeping; without comments there is nothing to remember.

    Failures here must NOT poison the evaluation write — auto-mining is
    best-effort, the rating is the source of truth.
    """

    from lhgp.memory import MemoryKind, MemoryScope, make_pattern_memory, record_memory

    if not evaluation.comments.strip():
        return
    try:
        # EvaluationRating is a StrEnum where value is "1".."5" — int()
        # of the str enum is the int, but be defensive against bad inputs.
        rating_int = int(evaluation.rating)
    except (TypeError, ValueError):
        return
    if rating_int < 4 and evaluation.verdict != EvaluationVerdict.REJECT:
        return
    if rating_int >= 4:
        kind = MemoryKind.PATTERN
        title = f"[{evaluation.contract_id}] what worked (rating {rating_int}/5)"
        score = 0.6 + 0.1 * (rating_int - 4)  # 4 -> 0.6, 5 -> 0.7
    else:
        kind = MemoryKind.GOTCHA
        title = f"[{evaluation.contract_id}] what to avoid (rejected)"
        score = 0.7  # rejections are signal-dense
    memory = make_pattern_memory(
        title=title,
        body_md=evaluation.comments.strip(),
        tags=("source/evaluation", f"rating/{rating_int}"),
        source_contract_id=evaluation.contract_id,
        source_event_id=evaluation_id,
        source_actor=f"eval:{evaluation.evaluator}",
        score=score,
        expires_in_days=365 if kind == MemoryKind.GOTCHA else 180,
    )
    # Scope: project-level by default; if comments mention a specific
    # domain tag like "topic: persistence" we put it on domain scope.
    if "topic:" in evaluation.comments:
        memory = memory.__class__(
            scope=MemoryScope.DOMAIN,
            kind=kind,
            title=memory.title,
            body_md=memory.body_md,
            tags=memory.tags,
            source_contract_id=memory.source_contract_id,
            source_event_id=memory.source_event_id,
            source_actor=memory.source_actor,
            score=memory.score,
            created_at=memory.created_at,
            expires_at=memory.expires_at,
            schema_version=memory.schema_version,
        )
    try:
        record_memory(conn, memory)
    except Exception as exc:
        # The evaluation is the source of truth; don't fail the write if
        # auto-mining chokes. The auto-mine path itself should be unit-tested
        # so this branch is rarely taken.
        import logging

        logging.getLogger("lhgp.feedback").warning(
            "auto-mine memory from evaluation failed: %s", exc
        )


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
    evaluation_id = int(lastrowid)
    # P6+1 / memory-and-wiki Phase 2: best-effort auto-mine a long-term
    # memory from this evaluation. Failures here are swallowed (the
    # rating is the source of truth, the memory is a side effect).
    _maybe_record_memory_from_evaluation(conn, evaluation, evaluation_id)
    return evaluation_id


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
