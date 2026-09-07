"""Attempt resume entry point: read active.md + handover.md, emit audit event.

Covers:
- active.md only (no handover.md) -> body uses placeholder
- active.md + handover.md -> body inlines both
- missing active.md -> FileNotFoundError with the contract/attempt in the message
- two calls with the same inputs produce the same body (idempotent on `now`)
- the optional `conn` writes an `attempt/resumed` audit event when supplied
- the optional `next_attempt_id` override is respected
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lhgp.contracts import ResumeBrief, build_resume_brief
from lhgp.persistence.events_query import get_events
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.types import StoreConfig
from longtask.persistence.store import connect

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
CONTRACT_ID = "lt-resume01"
ATTEMPT_ID = "att-20260907120000-1"
ACTIVE_TEXT = (
    "## active context\nobjective: ship the resume entry point\nnext_action: wire MCP tool\n"
)
HANDOVER_TEXT = "## next_action\nwire MCP tool resume_attempt and the CLI subcommand\n"


def _seed_attempt(
    tmp_path: Path,
    *,
    with_handover: bool,
    active_text: str = ACTIVE_TEXT,
) -> Path:
    root = tmp_path / "data"
    attempt_dir = root / "contracts" / CONTRACT_ID / "context" / "attempts" / ATTEMPT_ID
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "active.md").write_text(active_text, encoding="utf-8")
    if with_handover:
        (root / "contracts" / CONTRACT_ID).mkdir(parents=True, exist_ok=True)
        (root / "contracts" / CONTRACT_ID / "handover.md").write_text(
            HANDOVER_TEXT, encoding="utf-8"
        )
    return root


def test_active_only_body_uses_placeholder(tmp_path: Path) -> None:
    root = _seed_attempt(tmp_path, with_handover=False)
    brief = build_resume_brief(root, CONTRACT_ID, ATTEMPT_ID, now=NOW)
    assert isinstance(brief, ResumeBrief)
    assert brief.contract_id == CONTRACT_ID
    assert brief.attempt_id == ATTEMPT_ID
    assert brief.next_attempt_id == f"{ATTEMPT_ID}-resumed-20260907120000"
    assert brief.active_md_path.is_file()
    assert not brief.handover_md_path.exists()
    assert "## Active context snapshot" in brief.body
    assert ACTIVE_TEXT in brief.body
    assert "_(no handover.md written)_" in brief.body
    assert "## Resume instructions" in brief.body


def test_active_plus_handover_body_inlines_both(tmp_path: Path) -> None:
    root = _seed_attempt(tmp_path, with_handover=True)
    brief = build_resume_brief(root, CONTRACT_ID, ATTEMPT_ID, now=NOW)
    assert brief.handover_md_path.is_file()
    assert ACTIVE_TEXT in brief.body
    assert HANDOVER_TEXT in brief.body
    assert "_(no handover.md written)_" not in brief.body


def test_missing_active_md_raises(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "contracts" / CONTRACT_ID).mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError) as excinfo:
        build_resume_brief(root, CONTRACT_ID, ATTEMPT_ID, now=NOW)
    msg = str(excinfo.value)
    assert CONTRACT_ID in msg
    assert ATTEMPT_ID in msg
    assert "active.md" in msg


def test_idempotent_body_with_same_now(tmp_path: Path) -> None:
    root = _seed_attempt(tmp_path, with_handover=True)
    first = build_resume_brief(root, CONTRACT_ID, ATTEMPT_ID, now=NOW)
    second = build_resume_brief(root, CONTRACT_ID, ATTEMPT_ID, now=NOW)
    assert first.body == second.body
    assert first.next_attempt_id == second.next_attempt_id


def test_next_attempt_id_override_wins(tmp_path: Path) -> None:
    root = _seed_attempt(tmp_path, with_handover=False)
    brief = build_resume_brief(
        root,
        CONTRACT_ID,
        ATTEMPT_ID,
        next_attempt_id="att-custom-next",
        now=NOW,
    )
    assert brief.next_attempt_id == "att-custom-next"
    assert "att-custom-next" in brief.body


def test_conn_writes_attempt_resumed_audit_event(tmp_path: Path) -> None:
    root = _seed_attempt(tmp_path, with_handover=False)
    db_path = root / "state.db"
    conn = connect(StoreConfig(db_path=db_path))
    ensure_schema(conn)
    try:
        brief = build_resume_brief(
            root,
            CONTRACT_ID,
            ATTEMPT_ID,
            conn=conn,
            now=NOW,
        )
    finally:
        conn.close()
    verify = sqlite3.connect(db_path)
    verify.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in verify.execute(
                "SELECT event_type, attempt_id, actor, payload_json "
                "FROM events WHERE contract_id = ? "
                "ORDER BY event_id",
                (CONTRACT_ID,),
            )
        ]
    finally:
        verify.close()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "attempt/resumed"
    assert rows[0]["attempt_id"] == ATTEMPT_ID
    assert rows[0]["actor"] == "user"
    assert f'"from_attempt_id": "{ATTEMPT_ID}"' in rows[0]["payload_json"]
    assert f'"next_attempt_id": "{brief.next_attempt_id}"' in rows[0]["payload_json"]


def test_get_events_sees_written_resume_event(tmp_path: Path) -> None:
    """Re-query via the canonical events_query API, not raw SQL, so the
    query layer stays the source of truth for any reader."""
    root = _seed_attempt(tmp_path, with_handover=False)
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    try:
        build_resume_brief(
            root,
            CONTRACT_ID,
            ATTEMPT_ID,
            conn=conn,
            now=NOW,
        )
        events = get_events(conn, contract_id=CONTRACT_ID)
    finally:
        conn.close()
    types = [e.event_type for e in events]
    assert "attempt/resumed" in types
