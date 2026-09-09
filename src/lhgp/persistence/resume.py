"""Attempt resume entry point.

When a session crashes, runs out of context, or hits a 502-then-fail, the next
attempt needs a self-contained brief that does not require re-deriving the
goal.  lhgp already materialises ``active.md`` (the per-attempt snapshot) and
``handover.md`` (the next-action memo) on every attempt; this module reads
both, assembles a single string, and (optionally) records an
``attempt/resumed`` audit event so operators can see the resume happened.

P1 review fix (2026-09-08): the previous implementation joined
``contract_id`` and ``attempt_id`` directly into a file path under
``data_root`` without verifying that the resolved path stayed inside
``contracts/<contract_id>/``.  A caller could pass an absolute path or
``../etc`` as ``attempt_id`` and read a file outside the contract's
directory.  The fix is a strict whitelist on the path components plus
a ``Path.is_relative_to`` check after resolution.

The audit event's ``actor`` is now a parameter, not a hard-coded
``"user"`` string.  The MCP path passes its own actor
(``"agent:mcp"``/``"agent:<id>"``); the CLI passes ``"user:cli"``;
the Python API defaults to ``"user"`` for back-compat.

Layer note: this module reads file projections, queries the
``attempts`` table and appends an audit event, so it lives in
``persistence`` — not in ``contracts``, which ARCHITECTURE.md defines
as the zero-dependency data layer (no ``persistence``, no ``cli``).
It was previously ``lhgp/contracts/resume.py``, which the architecture
gate could not see because its rules only matched ``longtask.*``
imports; fixing that rule is what surfaced this placement error.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event

_RESUMED_EVENT_TYPE = EventType.ATTEMPT_RESUMED

_CONTRACT_DIR = "contracts"
_CONTEXT_DIR = "context"
_ATTEMPTS_DIR = "attempts"
_ACTIVE_FILE = "active.md"
_HANDOVER_FILE = "handover.md"

# Path-component whitelist: ``[A-Za-z0-9._-]`` covers contract IDs
# (``lt-...``), attempt IDs (``att-YYYYMMDDhhmmss-seq``), goal IDs and the
# fixed suffixes.  Anything else — absolute paths, ``..``, separators,
# whitespace, NULs, shell metacharacters — is rejected before it ever
# touches the filesystem.  This is the primary defense; the
# ``is_relative_to`` check below is the safety net for symlink escapes.
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


class ResumeBriefError(ValueError):
    """Raised when a resume request is unsafe or unmatched.

    Subclass of ``ValueError`` so existing callers that catch
    ``ValueError`` keep working, while callers that want to distinguish
    security rejections can catch this specifically.
    """


def _validate_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or not _SAFE_PATH_COMPONENT.match(value):
        raise ResumeBriefError(
            f"{label} must match {_SAFE_PATH_COMPONENT.pattern!r}: got {value!r}"
        )
    return value


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


def _assert_within_contract_dir(candidate: Path, contract_dir_root: Path, *, label: str) -> None:
    """Defence-in-depth: even if the components passed the whitelist,
    resolve symlinks and ``..`` and confirm the result is still inside
    ``contract_dir_root``.  Without this, a malicious symlink planted
    inside the contract's directory would let the read escape it."""

    try:
        resolved = candidate.resolve(strict=False)
        contract_dir_root_resolved = contract_dir_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ResumeBriefError(f"{label} path resolution failed: {exc}") from exc
    if not resolved.is_relative_to(contract_dir_root_resolved):
        raise ResumeBriefError(
            f"{label} escapes contract directory: {candidate} -> {resolved} "
            f"not under {contract_dir_root_resolved}"
        )


def _attempt_belongs_to_contract(
    conn: sqlite3.Connection, contract_id: str, attempt_id: str
) -> bool:
    """DB-level check that ``attempt_id`` was actually written under
    ``contract_id``.  Without this an attacker who could drop a file
    named ``active.md`` anywhere under ``contracts/`` could mint a
    resume brief for a contract they don't own.  Returns False (no
    match) if the attempts table is empty or the attempt does not
    exist for this contract.
    """
    row = conn.execute(
        "SELECT 1 FROM attempts WHERE contract_id = ? AND attempt_id = ? LIMIT 1",
        (contract_id, attempt_id),
    ).fetchone()
    return row is not None


def build_resume_brief(
    data_root: Path,
    contract_id: str,
    attempt_id: str,
    next_attempt_id: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    actor: str = "user",
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
    Raises :class:`ResumeBriefError` (a ``ValueError`` subclass) if
    ``contract_id`` or ``attempt_id`` fail the path-safety whitelist, if
    the resolved path escapes the contract directory, or if a
    ``conn`` is supplied and the attempt is not recorded under this
    contract in the DB.
    """
    contract_id = _validate_id(contract_id, "contract_id")
    attempt_id = _validate_id(attempt_id, "attempt_id")
    if next_attempt_id is not None:
        _validate_id(next_attempt_id, "next_attempt_id")

    contract_dir = data_root / _CONTRACT_DIR / contract_id
    active_path = contract_dir / _CONTEXT_DIR / _ATTEMPTS_DIR / attempt_id / _ACTIVE_FILE
    handover_path = contract_dir / _HANDOVER_FILE
    _assert_within_contract_dir(active_path, contract_dir, label="active.md path")
    # P2 review (2026-09-08): the active.md check above stops direct
    # path-traversal in attempt_id, but handover.md would still leak:
    # ``is_file()`` follows symlinks, and ``read_text()`` would happily
    # read a symlinked file that points outside the contract directory.
    # The reviewer's repro: a valid contract + valid attempt, with
    # ``handover.md`` replaced by a symlink to ``/tmp/secret.md``; the
    # resume brief inlined the secret.  Fix: same boundary check as
    # active.md, applied *before* the read.
    _assert_within_contract_dir(handover_path, contract_dir, label="handover.md path")

    if conn is not None and not _attempt_belongs_to_contract(conn, contract_id, attempt_id):
        raise ResumeBriefError(
            f"attempt {attempt_id!r} is not recorded under contract "
            f"{contract_id!r} in the DB; refusing to read its active.md "
            "(path-binding mismatch)"
        )

    if not active_path.is_file():
        raise FileNotFoundError(
            f"active.md not found for {contract_id} / {attempt_id} at {active_path}"
        )

    active_text = active_path.read_text(encoding="utf-8")
    # Same symlink defense for handover.md: even if the path itself
    # is inside the contract dir, an attacker-planted symlink can
    # resolve to anywhere.  Resolve the *link target* and re-check
    # the boundary before reading.
    if handover_path.is_file():
        try:
            resolved_handover = handover_path.resolve(strict=False)
        except OSError as exc:
            raise ResumeBriefError(f"handover.md path resolution failed: {exc}") from exc
        contract_dir_resolved = contract_dir.resolve(strict=False)
        if not resolved_handover.is_relative_to(contract_dir_resolved):
            raise ResumeBriefError(
                f"handover.md is a symlink that escapes the contract "
                f"directory: {handover_path} -> {resolved_handover} not under "
                f"{contract_dir_resolved}"
            )
        handover_text = resolved_handover.read_text(encoding="utf-8")
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
            actor=actor,
        )

    return ResumeBrief(
        contract_id=contract_id,
        attempt_id=attempt_id,
        next_attempt_id=resolved_next,
        active_md_path=active_path,
        handover_md_path=handover_path,
        body=body,
    )


__all__ = ["ResumeBrief", "ResumeBriefError", "build_resume_brief"]
