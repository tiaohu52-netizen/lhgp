"""P6+1 / memory-and-wiki Phase 2: failure-driven memory mining.

Counterpart of :mod:`lhgp.feedback.store`::_maybe_record_memory_from_evaluation,
which auto-mines a PATTERN/GOTCHA memory when a *positive* evaluation arrives.
This module auto-mines a GLOBAL-scope GOTCHA memory when an attempt's
*failures* accumulate, so future contracts see the lesson instead of
re-paying the same cost.

The check is cheap on the no-action path (a single COUNT/MAX query) so it
can run on every REJECT evaluation without blowing the daemon budget.
Idempotent: a second call within the same failure cluster returns None
until a new failure arrives.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

_LOG = logging.getLogger("lhgp.feedback.lessons")


def _count_failures_since_last_lesson(
    conn: sqlite3.Connection,
    contract_id: str,
) -> tuple[int, int, int | None]:
    """Return (fail_count, reject_count, last_fail_event_id) since last lesson.

    ``fail_count`` is the number of ATTEMPT_FAILED events for the contract
    with event_id strictly greater than the last MEMORY_LESSON_MINED
    event. ``reject_count`` is the number of user_evaluations with
    verdict=reject for the contract with created_at strictly greater
    than that same lesson event. Both signals are terminal failure
    signals; either alone is enough to trip the lesson once the sum
    crosses ``min_failures``.
    """
    from lhgp.persistence.events import EventType

    boundary = conn.execute(
        "SELECT event_id, created_at FROM events "
        "WHERE contract_id = ? AND event_type = ? "
        "ORDER BY event_id DESC LIMIT 1",
        (contract_id, EventType.MEMORY_LESSON_MINED.value),
    ).fetchone()
    last_lesson_event_id = int(boundary[0]) if boundary and boundary[0] is not None else 0
    last_lesson_at = boundary[1] if boundary else None

    fail_row = conn.execute(
        "SELECT COUNT(*), MAX(event_id) FROM events "
        "WHERE contract_id = ? AND event_type = ? AND event_id > ?",
        (contract_id, EventType.ATTEMPT_FAILED.value, last_lesson_event_id),
    ).fetchone()
    fail_count = int(fail_row[0] or 0)
    last_fail_event_id = int(fail_row[1]) if fail_row[1] is not None else None

    if last_lesson_at is None:
        reject_row = conn.execute(
            "SELECT COUNT(*) FROM user_evaluations WHERE contract_id = ? AND verdict = ?",
            (contract_id, "reject"),
        ).fetchone()
    else:
        reject_row = conn.execute(
            "SELECT COUNT(*) FROM user_evaluations "
            "WHERE contract_id = ? AND verdict = ? AND created_at > ?",
            (contract_id, "reject", last_lesson_at),
        ).fetchone()
    reject_count = int(reject_row[0] or 0)
    return fail_count, reject_count, last_fail_event_id


def _build_lesson_body(
    contract_id: str,
    fail_count: int,
    reject_count: int,
    last_fail_event_id: int | None,
) -> str:
    """Assemble a short markdown body summarising the failure cluster.

    Kept compact and structured: a future contract's context compiler
    reads the body verbatim, so the format must stay stable and
    machine-greppable.
    """
    parts: list[str] = [
        f"Contract `{contract_id}` accumulated terminal failures since the last lesson:",
        f"- ATTEMPT_FAILED events: {fail_count}",
        f"- REJECT evaluations: {reject_count}",
    ]
    if last_fail_event_id is not None:
        parts.append(f"- last failure event_id: {last_fail_event_id}")
    parts.append("")
    parts.append(
        "Treat this as a GOTCHA: do not repeat the pattern that produced "
        "these failures on a new contract without first validating the fix."
    )
    return "\n".join(parts)


def _emit_lesson_audit(
    conn: sqlite3.Connection,
    contract_id: str,
    memory_id: int,
    failure_count: int,
    last_failure_event_id: int | None,
    now: datetime,
) -> None:
    """Append a MEMORY_LESSON_MINED event so the lesson is auditable.

    Best-effort, like the auto-mine audit: if the events table is
    unavailable the memory still stands as the source of truth.
    """
    from lhgp.persistence.events import EventType
    from lhgp.persistence.events_query import append_event

    payload: dict[str, Any] = {
        "contract_id": contract_id,
        "memory_id": memory_id,
        "failure_count": int(failure_count),
    }
    if last_failure_event_id is not None:
        payload["last_failure_event_id"] = int(last_failure_event_id)
    try:
        append_event(
            conn,
            contract_id=contract_id,
            goal_id=contract_id,
            event_type=EventType.MEMORY_LESSON_MINED,
            payload=payload,
            now=now,
            actor="auto-lesson",
            role="system",
        )
    except Exception as exc:
        _LOG.warning("lesson audit event failed: %s", exc)


def mine_lesson_if_due(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    min_failures: int = 3,
    now: datetime | None = None,
) -> int | None:
    """Mine a GLOBAL-scope GOTCHA memory if the contract has accumulated
    enough terminal failures since the last lesson.

    Failure signals counted (either kind alone is enough to trip):
      - ``ATTEMPT_FAILED`` events for the contract since the last
        ``MEMORY_LESSON_MINED`` event (compared by ``event_id``)
      - user evaluations with ``verdict=reject`` since the last
        ``MEMORY_LESSON_MINED`` event (compared by ``created_at``)

    When the sum reaches ``min_failures`` a GOTCHA memory of
    ``scope=GLOBAL`` is written (so future contracts see it) and a
    ``MEMORY_LESSON_MINED`` audit event is appended. Returns the new
    memory id. On the no-action path returns ``None`` after a single
    SQL count, so the call is cheap on every REJECT evaluation.

    Idempotent: a second call within the same failure cluster returns
    ``None`` because the new lesson's event_id/created_at push the
    count of "since last lesson" back to zero.
    """
    if min_failures < 1:
        raise ValueError("min_failures must be >= 1")
    now = now or datetime.now(UTC)
    fail_count, reject_count, last_fail_event_id = _count_failures_since_last_lesson(
        conn, contract_id
    )
    total = fail_count + reject_count
    if total < min_failures:
        return None

    from lhgp.memory import MemoryKind, MemoryScope, make_memory, record_memory

    body_md = _build_lesson_body(contract_id, fail_count, reject_count, last_fail_event_id)
    title = f"[{contract_id}] failure cluster ({total} signals) — auto-mined lesson"
    memory = make_memory(
        title=title,
        body_md=body_md,
        tags=("source/lesson", f"contract/{contract_id}", f"failures/{total}"),
        source_contract_id=contract_id,
        source_event_id=last_fail_event_id,
        source_actor="auto-lesson",
        score=0.75,
        expires_in_days=365,
        kind=MemoryKind.GOTCHA,
        scope=MemoryScope.GLOBAL,
    )
    try:
        memory_id = record_memory(conn, memory)
    except Exception as exc:
        _LOG.warning("lesson memory write failed for %s: %s", contract_id, exc)
        return None
    _emit_lesson_audit(
        conn,
        contract_id,
        memory_id,
        total,
        last_fail_event_id,
        now,
    )
    return memory_id


__all__ = ["mine_lesson_if_due"]
