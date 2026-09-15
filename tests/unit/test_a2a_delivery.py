"""A2A delivery hardening: TTL, per-receiver dedup, acknowledgement events.

3rd-round review 2026-09-08:
- Directive TTL filter on pending_directives
- Per-receiver dedup (idempotent on directive_id+to_agent)
- Per-receiver delivery confirmation (directive/acknowledged event)
- spec_hash binding in the plan gate
- Auto-create next stage contract from stage spec (not just inline draft)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.goals.stage import StageSpec, validate_stage_entry
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from lhgp.persistence.messages import send_message
from longtask.persistence.context import (
    compile_context_snapshot,
    mark_directives_consumed,
)
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    return conn


def _make_draft(
    contract_id: str, *, spec: dict | None = None, spec_hash: str | None = None
) -> tuple[ContractDraft, dict[str, str | dict]]:
    acceptance_kwargs: dict[str, str | dict] = {"standard": "s", "checks": ("c1",)}
    if spec is not None:
        acceptance_kwargs["spec"] = spec
        acceptance_kwargs["spec_hash"] = spec_hash
    return (
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard=acceptance_kwargs["standard"],  # type: ignore[arg-type]
                checks=acceptance_kwargs["checks"],  # type: ignore[arg-type]
                spec=spec,
                spec_hash=spec_hash,
            ),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
            auto_approve=AutoApprove(),
        ),
        acceptance_kwargs,
    )


def _save_draft(conn: sqlite3.Connection, draft: ContractDraft, contract_id: str) -> None:
    save_contract(conn, draft=draft, contract_id=contract_id, now=NOW, actor="user")


# ── A2A: TTL filter ───────────────────────────────────────────────


def test_pending_directives_ttl_filter_drops_expired(tmp_path: Path) -> None:
    """A directive older than max_age_seconds is dropped from pending."""
    from lhgp.persistence.messages import pending_directives

    conn = _make_conn(tmp_path)
    draft, _ = _make_draft("lt-ttl-1")
    _save_draft(conn, draft, "lt-ttl-1")
    send_message(
        conn,
        contract_id="lt-ttl-1",
        from_actor="user",
        kind="directive",
        text="old news",
        now=NOW - timedelta(hours=2),
    )
    # No TTL: visible
    assert len(pending_directives(conn, contract_id="lt-ttl-1")) == 1
    # 30-minute TTL: filtered out (2 hours old)
    out = pending_directives(
        conn,
        contract_id="lt-ttl-1",
        now=NOW,
        max_age_seconds=30 * 60,
    )
    assert out == []


def test_pending_directives_ttl_keeps_fresh(tmp_path: Path) -> None:
    """A directive within max_age_seconds is still returned."""
    from lhgp.persistence.messages import pending_directives

    conn = _make_conn(tmp_path)
    draft, _ = _make_draft("lt-ttl-2")
    _save_draft(conn, draft, "lt-ttl-2")
    send_message(
        conn,
        contract_id="lt-ttl-2",
        from_actor="user",
        kind="directive",
        text="still fresh",
        now=NOW - timedelta(seconds=10),
    )
    out = pending_directives(
        conn,
        contract_id="lt-ttl-2",
        now=NOW,
        max_age_seconds=60,
    )
    assert len(out) == 1
    assert out[0]["text"] == "still fresh"


# ── A2A: per-receiver dedup ────────────────────────────────────────


def test_per_receiver_dedup_skips_already_acked(tmp_path: Path) -> None:
    """snapshot compile skips directives already in the per-agent ack set."""

    conn = _make_conn(tmp_path)
    draft, _ = _make_draft("lt-dedup-1")
    _save_draft(conn, draft, "lt-dedup-1")
    _did = send_message(
        conn,
        contract_id="lt-dedup-1",
        from_actor="user",
        kind="directive",
        text="important",
        now=NOW,
        to_agent="agent-a",
    )
    # First snapshot for agent-a: should see the directive
    _active, _scratch, max_id, consumed_ids = compile_context_snapshot(
        tmp_path,
        conn,
        type("C", (), {"draft": draft, "contract_id": "lt-dedup-1", "revision": 1})(),
        "att-1",
        NOW,
        to_agent="agent-a",
    )
    assert len(consumed_ids) == 1
    # Mark consumed (writes the per-agent ack set)
    assert (
        mark_directives_consumed(
            conn,
            "lt-dedup-1",
            max_id,
            to_agent="agent-a",
            consumed_ids=consumed_ids,
            now=NOW,
        )
        is True
    )
    # Second snapshot for agent-a: dedup should skip the directive
    _active2, _scratch2, _max_id2, consumed_ids2 = compile_context_snapshot(
        tmp_path,
        conn,
        type("C", (), {"draft": draft, "contract_id": "lt-dedup-1", "revision": 1})(),
        "att-2",
        NOW,
        to_agent="agent-a",
    )
    assert consumed_ids2 == []


# ── A2A: delivery confirmation event ──────────────────────────────


def test_mark_directives_consumed_writes_ack_events(tmp_path: Path) -> None:
    """Each newly consumed directive gets a directive/acknowledged event."""
    conn = _make_conn(tmp_path)
    draft, _ = _make_draft("lt-ack-1")
    _save_draft(conn, draft, "lt-ack-1")
    did1 = send_message(
        conn,
        contract_id="lt-ack-1",
        from_actor="user",
        kind="directive",
        text="first",
        now=NOW,
        to_agent="agent-b",
    )
    did2 = send_message(
        conn,
        contract_id="lt-ack-1",
        from_actor="user",
        kind="directive",
        text="second",
        now=NOW,
        to_agent="agent-b",
    )
    assert (
        mark_directives_consumed(
            conn,
            "lt-ack-1",
            max(did1, did2),
            to_agent="agent-b",
            consumed_ids=[did1, did2],
            now=NOW,
        )
        is True
    )
    # Two ack events should exist
    acks = [
        e
        for e in get_events(conn, contract_id="lt-ack-1")
        if str(e.event_type) == EventType.DIRECTIVE_ACKNOWLEDGED.value
    ]
    assert len(acks) == 2
    ack_ids = sorted(json.loads(a.payload_json or "{}")["directive_event_id"] for a in acks)
    assert ack_ids == sorted([did1, did2])
    # Actor should be agent-b
    assert all(a.actor == "agent:agent-b" for a in acks)


def test_mark_directives_consumed_idempotent_on_repeat(tmp_path: Path) -> None:
    """Re-marking with the same cursor / same ids writes zero new events."""
    conn = _make_conn(tmp_path)
    draft, _ = _make_draft("lt-ack-2")
    _save_draft(conn, draft, "lt-ack-2")
    did = send_message(
        conn,
        contract_id="lt-ack-2",
        from_actor="user",
        kind="directive",
        text="x",
        now=NOW,
        to_agent="agent-c",
    )
    # First mark: returns True
    assert (
        mark_directives_consumed(
            conn,
            "lt-ack-2",
            did,
            to_agent="agent-c",
            consumed_ids=[did],
            now=NOW,
        )
        is True
    )
    # Second mark: cursor already advanced → no-op, returns False
    assert (
        mark_directives_consumed(
            conn,
            "lt-ack-2",
            did,
            to_agent="agent-c",
            consumed_ids=[did],
            now=NOW,
        )
        is False
    )
    # Only one ack event
    acks = [
        e
        for e in get_events(conn, contract_id="lt-ack-2")
        if str(e.event_type) == EventType.DIRECTIVE_ACKNOWLEDGED.value
    ]
    assert len(acks) == 1


# ── spec_hash binding in plan gate ─────────────────────────────────


def test_plan_gate_rejects_stale_spec_hash(tmp_path: Path) -> None:
    """Approval with old spec_hash does NOT clear the gate after spec change."""
    from lhgp.persistence.events import EventType
    from longtask.cli.dispatch import _has_recent_plan_approval
    from longtask.persistence.store import append_event

    conn = _make_conn(tmp_path)
    draft, _ = _make_draft("lt-spec-1", spec={"all": []}, spec_hash="hash-v1")
    _save_draft(conn, draft, "lt-spec-1")
    # Approve plan with spec_hash v1
    append_event(
        conn,
        contract_id="lt-spec-1",
        event_type=EventType.PLAN_APPROVED,
        payload={
            "contract_revision": 1,
            "accepted_check_ids": ["c1"],
            "spec_hash": "hash-v1",
        },
        now=NOW,
        actor="daemon",
    )
    # Now the contract's spec changes to v2 → gate must reject
    assert _has_recent_plan_approval(conn, "lt-spec-1", 1, ["c1"], "hash-v2", NOW) is False
    # And a contract that lost its spec since approval is also stale
    assert _has_recent_plan_approval(conn, "lt-spec-1", 1, ["c1"], None, NOW) is False


def test_plan_gate_accepts_matching_spec_hash(tmp_path: Path) -> None:
    """Approval with current spec_hash clears the gate."""
    from lhgp.persistence.events import EventType
    from longtask.cli.dispatch import _has_recent_plan_approval
    from longtask.persistence.store import append_event

    conn = _make_conn(tmp_path)
    draft, _ = _make_draft("lt-spec-2", spec={"all": []}, spec_hash="hash-x")
    _save_draft(conn, draft, "lt-spec-2")
    append_event(
        conn,
        contract_id="lt-spec-2",
        event_type=EventType.PLAN_APPROVED,
        payload={
            "contract_revision": 1,
            "accepted_check_ids": ["c1"],
            "spec_hash": "hash-x",
        },
        now=NOW,
        actor="daemon",
    )
    assert _has_recent_plan_approval(conn, "lt-spec-2", 1, ["c1"], "hash-x", NOW) is True


# ── Stage spec validation ─────────────────────────────────────────


def test_stage_spec_validation_surfaces_required_fields() -> None:
    spec_dict = {
        "goal": "实现 X",
        "acceptance": {"all": [{"judge": "machine", "kind": "file-exists", "target": "x"}]},
    }
    errs = validate_stage_entry({"id": "s1", "title": "X", "spec": spec_dict})
    assert errs == []
    errs = validate_stage_entry({"id": "s1", "title": "X"})
    assert any("stage.spec is required" in e for e in errs)
    errs = validate_stage_entry({"id": "s1", "title": "X", "spec": {"goal": ""}})
    assert any("goal" in e for e in errs)


def test_stage_spec_hash_changes_on_edit() -> None:
    a = StageSpec(
        goal="x", acceptance={"all": [{"judge": "machine", "kind": "file-exists", "target": "x"}]}
    )
    b = StageSpec(
        goal="x", acceptance={"all": [{"judge": "machine", "kind": "file-exists", "target": "y"}]}
    )
    assert a.spec_hash() != b.spec_hash()
