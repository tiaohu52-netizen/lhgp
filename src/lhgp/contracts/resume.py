"""Attempt resume entry point.

When a session crashes, runs out of context, or hits a 502-then-fail, the next
attempt needs a self-contained brief that does not require re-deriving the
goal.  lhgp already materialises ``active.md`` (the per-attempt snapshot) and
``handover.md`` (the next-action memo) on every attempt; this module reads
both, assembles a single string, and (optionally) records an
``attempt/resumed`` audit event so operators can see the resume happened.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lhgp.persistence.events_query import append_event

# TODO: use EventType.ATTEMPT_RESUMED once the events.py consolidation
# commit lands.  The wire string is the source of truth until then.
_RESUMED_EVENT_TYPE = "attempt/resumed"

_CONTRACT_DIR = "contracts"
_CONTEXT_DIR = "context"
_ATTEMPTS_DIR = "attempts"
_ACTIVE_FILE = "active.md"
_HANDOVER_FILE = "handover.md"


@dataclass(frozen=True, slots=True)
class ResumeBrief:
    contract_id: str
    attempt_id: str
    next_attempt_id: str
    active_md_path: Path
    handover_md_path: Path
    body: str


def _derive_next_attempt_id(attempt_id: str, now: datetime) -> str:
    """Stable, pure-function next-attempt id from inputs only.

    No DB lookup: the caller can override via ``next_attempt_id`` if they
    want a specific id (e.g. one already minted by the dispatch path).
    """
    return f"{attempt_id}-resumed-{now.strftime('%Y%m%d%H%M%S')}"


def build_resume_brief(
    data_root: Path,
    contract_id: str,
    attempt_id: str,
    next_attempt_id: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> ResumeBrief:
    """Load active.md + handover.md and assemble a single self-contained brief.

    The body is structured as::

        # Resume for <contract_id> / <attempt_id>
        # Resuming into <next_attempt_id> at <iso8601>

        ## Active context snapshot
        <active.md contents, verbatim>

        ## Last handover
        <handover.md contents, verbatim, or "_(no handover.md written)_">

        ## Resume instructions
        Continue from the handover's next_action. The active.md above
        is the bounded context; treat it as authoritative for state.

    Raises ``FileNotFoundError`` if ``active.md`` is missing (the only
    authoritative piece; ``handover.md`` is optional and may not exist for
    a fresh attempt that never reached the handover write step).
    """
    contract_dir = data_root / _CONTRACT_DIR / contract_id
    active_path = contract_dir / _CONTEXT_DIR / _ATTEMPTS_DIR / attempt_id / _ACTIVE_FILE
    handover_path = contract_dir / _HANDOVER_FILE
    if not active_path.is_file():
        raise FileNotFoundError(
            f"active.md not found for {contract_id} / {attempt_id} at {active_path}"
        )

    active_text = active_path.read_text(encoding="utf-8")
    if handover_path.is_file():
        handover_text = handover_path.read_text(encoding="utf-8")
    else:
        handover_text = "_(no handover.md written)_"

    timestamp = now if now is not None else datetime.now(UTC)
    resolved_next = next_attempt_id or _derive_next_attempt_id(attempt_id, timestamp)

    body = (
        f"# Resume for {contract_id} / {attempt_id}\n"
        f"# Resuming into {resolved_next} at {timestamp.isoformat()}\n"
        "\n"
        "## Active context snapshot\n"
        f"{active_text}\n"
        "\n"
        "## Last handover\n"
        f"{handover_text}\n"
        "\n"
        "## Resume instructions\n"
        "Continue from the handover's next_action. The active.md above\n"
        "is the bounded context; treat it as authoritative for state.\n"
    )

    if conn is not None:
        append_event(
            conn,
            contract_id=contract_id,
            event_type=_RESUMED_EVENT_TYPE,
            payload={
                "from_attempt_id": attempt_id,
                "next_attempt_id": resolved_next,
            },
            now=timestamp,
            attempt_id=attempt_id,
            actor="user",
        )

    return ResumeBrief(
        contract_id=contract_id,
        attempt_id=attempt_id,
        next_attempt_id=resolved_next,
        active_md_path=active_path,
        handover_md_path=handover_path,
        body=body,
    )


__all__ = ["ResumeBrief", "build_resume_brief"]
