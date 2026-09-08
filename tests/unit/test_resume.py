"""Attempt resume entry point: read active.md + handover.md, emit audit event.

Covers:
- active.md only (no handover.md) -> body uses placeholder
- active.md + handover.md -> body inlines both
- missing active.md -> FileNotFoundError with the contract/attempt in the message
- two calls with the same inputs produce the same body (idempotent on `now`)
- the optional `conn` writes an `attempt/resumed` audit event when supplied
- the optional `next_attempt_id` override is respected
- P1 review: path traversal in attempt_id/contract_id is rejected
- P1 review: attempt must be recorded in the DB when conn is supplied
- P1 review: actor is parameterized, not hard-coded to "user"
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lhgp.contracts import ResumeBrief, build_resume_brief
from lhgp.contracts.resume import ResumeBriefError
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


def _open_conn(root: Path) -> sqlite3.Connection:
    """Open the store and record the (contract_id, attempt_id) pair so
    the resume helper's path-binding check passes when a ``conn`` is
    supplied. Without this the helper would correctly refuse to read a
    file claiming to belong to a contract that doesn't know about it."""
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO attempts "
        "(attempt_id, goal_id, contract_id, contract_revision, role, state, "
        " admitted_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ATTEMPT_ID,
            CONTRACT_ID,
            CONTRACT_ID,
            1,
            "executor",
            "admitted",
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()
    return conn


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
    conn = _open_conn(root)
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
    conn = _open_conn(root)
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


class TestPathSafety:
    """P1 review (2026-09-08): the previous implementation joined
    ``contract_id`` / ``attempt_id`` directly into a file path with
    no whitelist or boundary check. A caller could pass an absolute
    path or ``../``-laden string and read a file outside the contract
    directory. These tests pin the new defenses."""

    def test_absolute_path_in_attempt_id_rejected(self, tmp_path: Path) -> None:
        _seed_attempt(tmp_path, with_handover=False)
        with pytest.raises(ResumeBriefError):
            build_resume_brief(
                tmp_path / "data",
                CONTRACT_ID,
                "C:/Windows/system32/active.md",
                now=NOW,
            )

    def test_traversal_in_attempt_id_rejected(self, tmp_path: Path) -> None:
        _seed_attempt(tmp_path, with_handover=False)
        with pytest.raises(ResumeBriefError):
            build_resume_brief(
                tmp_path / "data",
                CONTRACT_ID,
                "../../../etc/passwd",
                now=NOW,
            )

    def test_traversal_in_contract_id_rejected(self, tmp_path: Path) -> None:
        _seed_attempt(tmp_path, with_handover=False)
        with pytest.raises(ResumeBriefError):
            build_resume_brief(
                tmp_path / "data",
                "../../etc",
                ATTEMPT_ID,
                now=NOW,
            )

    def test_attempt_id_with_slash_rejected(self, tmp_path: Path) -> None:
        _seed_attempt(tmp_path, with_handover=False)
        with pytest.raises(ResumeBriefError):
            build_resume_brief(
                tmp_path / "data",
                CONTRACT_ID,
                "subdir/file",
                now=NOW,
            )

    def test_attempt_id_with_null_rejected(self, tmp_path: Path) -> None:
        _seed_attempt(tmp_path, with_handover=False)
        with pytest.raises(ResumeBriefError):
            build_resume_brief(
                tmp_path / "data",
                CONTRACT_ID,
                "att-\x00bad",
                now=NOW,
            )

    def test_unbound_attempt_refused_when_conn_supplied(self, tmp_path: Path) -> None:
        """Even with a whitelisted attempt_id, if the DB says no attempt
        with that id is recorded under the contract, the read is
        refused.  This stops an attacker who can plant a file inside
        the contract's directory from minting a resume brief for a
        contract they don't own."""

        _seed_attempt(tmp_path, with_handover=False)
        # Plant a *different* attempt_id so the DB-binding check fails
        # for the ATTEMPT_ID the helper will be asked to read.
        conn = connect(StoreConfig(db_path=tmp_path / "data" / "state.db"))
        ensure_schema(conn)
        try:
            conn.execute(
                "INSERT INTO attempts "
                "(attempt_id, goal_id, contract_id, contract_revision, role, state, "
                " admitted_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "att-OTHER-not-the-resume-target",
                    CONTRACT_ID,
                    CONTRACT_ID,
                    1,
                    "executor",
                    "admitted",
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
            conn.commit()
            with pytest.raises(ResumeBriefError, match="not recorded"):
                build_resume_brief(
                    tmp_path / "data",
                    CONTRACT_ID,
                    ATTEMPT_ID,
                    conn=conn,
                    now=NOW,
                )
        finally:
            conn.close()

    def test_bound_attempt_succeeds_with_conn(self, tmp_path: Path) -> None:
        """The DB-binding check is a feature, not a bug: the previous
        test plants a different attempt_id in the DB, so the planted
        file's attempt_id no longer matches and the call is refused.
        Here we plant the matching row and expect success."""

        _seed_attempt(tmp_path, with_handover=False)
        conn = _open_conn(tmp_path)
        try:
            brief = build_resume_brief(
                tmp_path / "data",
                CONTRACT_ID,
                ATTEMPT_ID,
                conn=conn,
                now=NOW,
            )
        finally:
            conn.close()
        assert brief.attempt_id == ATTEMPT_ID


class TestHandoverSymlinkDefense:
    """P2 review (2026-09-08): the original implementation checked
    the boundary on active.md but not on handover.md.  A valid
    contract + valid attempt, with ``handover.md`` replaced by a
    symlink to ``/tmp/secret.md`` (or any other external file),
    caused the resume brief to inline the secret.  Fix: same
    boundary check applied to handover.md *and* a symlink-target
    re-resolution so a symlink that resolves outside the contract
    dir is refused at read time."""

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX symlink semantics (this test sets up an external target via Path.symlink_to)",
    )
    def test_handover_symlink_outside_contract_dir_rejected(self, tmp_path: Path) -> None:
        # External file the symlink will point at.
        secret = tmp_path / "secret.md"
        secret.write_text("OUTSIDE_SECRET", encoding="utf-8")
        # Stage the contract with active.md and a symlinked handover.md.
        contract_dir = tmp_path / "data" / "contracts" / CONTRACT_ID
        attempt_dir = contract_dir / "context" / "attempts" / ATTEMPT_ID
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "active.md").write_text("ok", encoding="utf-8")
        handover = contract_dir / "handover.md"
        handover.symlink_to(secret)
        with pytest.raises(ResumeBriefError, match="symlink"):
            build_resume_brief(
                tmp_path / "data",
                CONTRACT_ID,
                ATTEMPT_ID,
                now=NOW,
            )

    def test_handover_inside_contract_dir_accepted(self, tmp_path: Path) -> None:
        """A symlink that points *inside* the contract dir is fine —
        it's a benign intra-project link, not an escape."""

        target_dir = (
            tmp_path / "data" / "contracts" / CONTRACT_ID / "context" / "attempts" / "att-other"
        )
        target_dir.mkdir(parents=True)
        (target_dir / "active.md").write_text("ok", encoding="utf-8")
        # Plant a legal active.md for the real attempt id.
        real_attempt_dir = (
            tmp_path / "data" / "contracts" / CONTRACT_ID / "context" / "attempts" / ATTEMPT_ID
        )
        real_attempt_dir.mkdir(parents=True, exist_ok=True)
        (real_attempt_dir / "active.md").write_text("ok", encoding="utf-8")
        # Symlink handover.md to a file inside the contract dir.
        handover_target = target_dir / "active.md"
        contract_dir = tmp_path / "data" / "contracts" / CONTRACT_ID
        handover = contract_dir / "handover.md"
        # Skip the symlink path on Windows where symlinks need
        # privileges the test env may not have.
        if not callable(Path.symlink_to):
            pytest.skip("symlinks unavailable in this environment")
        try:
            handover.symlink_to(handover_target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink not supported here: {exc}")
        # Should NOT raise; the brief should include the target's text.
        brief = build_resume_brief(tmp_path / "data", CONTRACT_ID, ATTEMPT_ID, now=NOW)
        assert "ok" in brief.body


class TestActorParameter:
    """P1 review: the audit event used to record ``actor="user"`` no
    matter who called the helper.  Forensic value is lost when an
    agent-initiated MCP call looks identical to a real user keystroke.
    The actor is now a parameter; default keeps the old behaviour for
    direct Python callers."""

    def test_default_actor_is_user(self, tmp_path: Path) -> None:
        from lhgp.persistence.events_query import get_events

        root = _seed_attempt(tmp_path, with_handover=False)
        conn = _open_conn(root)
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
        resumed = [e for e in events if e.event_type == "attempt/resumed"]
        assert len(resumed) == 1
        assert resumed[0].actor == "user"

    def test_mcp_actor_propagates(self, tmp_path: Path) -> None:
        from lhgp.persistence.events_query import get_events

        root = _seed_attempt(tmp_path, with_handover=False)
        conn = _open_conn(root)
        try:
            build_resume_brief(
                root,
                CONTRACT_ID,
                ATTEMPT_ID,
                conn=conn,
                now=NOW,
                actor="agent:mcp",
            )
            events = get_events(conn, contract_id=CONTRACT_ID)
        finally:
            conn.close()
        resumed = [e for e in events if e.event_type == "attempt/resumed"]
        assert len(resumed) == 1
        assert resumed[0].actor == "agent:mcp"
