"""P6: SQLite CRUD for user_evaluations and acceptance_diffs.

Both tables are populated by :mod:`lhgp.feedback` callers (CLI / MCP / webui)
and read by :mod:`lhgp.learning` to extract improvement signals.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from lhgp.feedback.types import AcceptanceDiff, EvaluationVerdict, UserEvaluation

# Anchored form: ``topic: <domain>`` at the start of the comment (after
# optional whitespace). The leading anchor stops the heuristic from
# firing on mid-prose mentions like "the topic: choice is yours".
_TOPIC_PATTERN = re.compile(r"^\s*topic:\s*([a-zA-Z][\w\-]+)")

_LOG = logging.getLogger("lhgp.feedback")


# ---------------------------------------------------------------------------
# P6+1 / memory-and-wiki Phase 2 — auto-mine hook
# ---------------------------------------------------------------------------


def _should_mine(evaluation: UserEvaluation) -> int | None:
    """Return the rating int if the evaluation is worth mining, else None.

    Mining conditions (any one suffices):
      - rating >= 4 with non-empty comments
      - REJECT verdict with non-empty comments
    """
    if not evaluation.comments.strip():
        return None
    try:
        rating_int = int(evaluation.rating)
    except (TypeError, ValueError):
        return None
    if rating_int >= 4:
        return rating_int
    if evaluation.verdict is EvaluationVerdict.REJECT:
        return rating_int
    return None


def _mine_plan(rating_int: int, contract_id: str) -> tuple[Any, str, float, int]:
    """Pick the (kind, title, score, expires_in_days) for a minable eval.

    High ratings become PATTERN memories (what worked); rejections
    become GOTCHA memories (what to avoid). The score bands are
    defined in the design — see CHANGELOG 0.1.0a8.
    """
    from lhgp.memory import MemoryKind

    if rating_int >= 4:
        return (
            MemoryKind.PATTERN,
            f"[{contract_id}] what worked (rating {rating_int}/5)",
            0.6 + 0.1 * (rating_int - 4),  # 4 -> 0.6, 5 -> 0.7
            180,
        )
    return (
        MemoryKind.GOTCHA,
        f"[{contract_id}] what to avoid (rejected)",
        0.7,  # rejections are signal-dense
        365,
    )


def _extract_topic_domain(comments: str) -> str | None:
    """Return the lowercase domain if the comment starts with ``topic: <x>``."""
    match = _TOPIC_PATTERN.match(comments)
    return match.group(1).lower() if match is not None else None


def _build_memory(
    evaluation: UserEvaluation,
    evaluation_id: int,
    rating_int: int,
    domain: str | None,
) -> Any:
    """Assemble the auto-mined Memory, including the optional topic-flip."""
    from lhgp.memory import MemoryScope, make_memory

    kind, title, score, expires_in_days = _mine_plan(rating_int, evaluation.contract_id)
    tags: tuple[str, ...] = ("source/evaluation", f"rating/{rating_int}")
    scope = MemoryScope.PROJECT
    if domain is not None:
        # Without the topic tag, the index filter would drop this memory
        # and the auto-mine would write to a dead-letter box.
        scope = MemoryScope.DOMAIN
        tags = (*tags, f"topic/{domain}")
    return make_memory(
        title=title,
        body_md=evaluation.comments.strip(),
        tags=tags,
        scope=scope,
        kind=kind,
        source_contract_id=evaluation.contract_id,
        source_event_id=evaluation_id,
        source_actor=f"eval:{evaluation.evaluator}",
        score=score,
        expires_in_days=expires_in_days,
    )


def _maybe_record_memory_from_evaluation(
    conn: sqlite3.Connection,
    evaluation: UserEvaluation,
    evaluation_id: int,
) -> None:
    """Auto-mine a long-term memory from a user evaluation.

    Mining conditions: rating >= 4 with non-empty comments, OR a REJECT
    verdict with non-empty comments. Failures here are swallowed (the
    rating is the source of truth, the memory is a side effect).

    On success or failure the events table gets one row so an
    investigator can answer "why did this evaluation produce / not
    produce a memory" without grepping logs.
    """
    from lhgp.memory import record_memory

    rating_int = _should_mine(evaluation)
    if rating_int is None:
        return
    domain = _extract_topic_domain(evaluation.comments)
    memory = _build_memory(evaluation, evaluation_id, rating_int, domain)
    try:
        new_id = record_memory(conn, memory)
    except Exception as exc:
        _LOG.warning("auto-mine memory from evaluation failed: %s", exc)
        _emit_auto_mine_audit(
            conn,
            evaluation,
            evaluation_id,
            success=False,
            memory_id=None,
            error=str(exc),
        )
        return
    _emit_auto_mine_audit(
        conn,
        evaluation,
        evaluation_id,
        success=True,
        memory_id=new_id,
        error=None,
    )


def _maybe_mine_lesson(
    conn: sqlite3.Connection,
    evaluation: UserEvaluation,
) -> None:
    """Failure-driven memory hook.

    Only runs on REJECT verdicts: a positive evaluation is a success
    signal, not a failure signal. The lesson is mined by
    :func:`lhgp.feedback.lessons.mine_lesson_if_due`, which is
    idempotent and cheap on the no-action path. Exceptions are
    swallowed so the evaluation write path stays fail-loud for the
    caller.
    """
    if evaluation.verdict is not EvaluationVerdict.REJECT:
        return
    try:
        from lhgp.feedback.lessons import mine_lesson_if_due

        mine_lesson_if_due(conn, evaluation.contract_id)
    except Exception as exc:
        _LOG.warning("lesson mine failed for %s: %s", evaluation.contract_id, exc)


def _emit_auto_mine_audit(
    conn: sqlite3.Connection,
    evaluation: UserEvaluation,
    evaluation_id: int,
    *,
    success: bool,
    memory_id: int | None,
    error: str | None,
) -> None:
    """Append a MEMORY_AUTO_MINED(_FAILED) event.

    Best-effort: if the events table is unavailable the audit loss
    is acceptable (the source-of-truth evaluation row already exists).
    """
    from datetime import UTC, datetime

    try:
        from longtask.persistence.events import EventType
        from longtask.persistence.store import append_event

        event_type = EventType.MEMORY_AUTO_MINED if success else EventType.MEMORY_AUTO_MINE_FAILED
        payload: dict[str, object] = {
            "evaluation_id": evaluation_id,
            "contract_id": evaluation.contract_id,
            "verdict": evaluation.verdict.value,
            "rating": str(evaluation.rating),
        }
        if success:
            payload["memory_id"] = memory_id
        else:
            payload["error"] = error
        append_event(
            conn,
            contract_id=evaluation.contract_id,
            goal_id=None,
            event_type=event_type,
            payload=payload,
            now=datetime.now(UTC),
            actor=f"auto-mine:{evaluation.evaluator}",
            role="system",
        )
    except Exception as exc:
        _LOG.warning("auto-mine audit event failed: %s", exc)


# ---------------------------------------------------------------------------
# user_evaluations CRUD
# ---------------------------------------------------------------------------


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
    _maybe_record_memory_from_evaluation(conn, evaluation, evaluation_id)
    _maybe_mine_lesson(conn, evaluation)
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
