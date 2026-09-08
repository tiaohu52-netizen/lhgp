"""Focused coverage for the 3rd-round review spec dispatch gaps.

Catches two regression classes:
1. _synthesize_stage_draft must forward spec budget/deadline/dependencies
   /artifacts (not just the boolean body).
2. The synthesis must walk the boolean spec recursively (all / any /
   leaf criterion), not just the top-level "all" array.
3. _has_recent_plan_approval must cover all 4 spec_hash match
   combinations.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event
from longtask.cli.dispatch import _has_recent_plan_approval
from longtask.cli.tick import _synthesize_stage_draft
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
)
from longtask.rpc.handlers.goal import handle_goal_prepare
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _connect(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    return conn


# ── _synthesize_stage_draft: spec body → acceptance.checks ──────────


def test_synthesize_walks_all_combinator(tmp_path: Path) -> None:
    goal: dict = {"objective": "x"}
    stage: dict = {
        "id": "s1",
        "title": "实现",
        "spec": {
            "all": [
                {"judge": "machine", "kind": "file-exists", "target": "a.txt"},
                {"judge": "machine", "kind": "file-exists", "target": "b.txt"},
            ]
        },
    }
    draft = _synthesize_stage_draft(goal, stage, previous_evidence=None, now=NOW)
    targets = [c["target"] for c in draft["acceptance"]["checks"]]
    assert "a.txt" in targets
    assert "b.txt" in targets


def test_synthesize_walks_any_combinator(tmp_path: Path) -> None:
    """The any[] branch was a real plan-gate misalignment bug — the
    synthesis only walked the top-level all[] array, leaving the any
    branch's machine criteria as the placeholder."""
    goal: dict = {"objective": "x"}
    stage: dict = {
        "id": "s1",
        "title": "实现",
        "spec": {
            "any": [
                {"judge": "machine", "kind": "file-exists", "target": "b.txt"},
                {"judge": "machine", "kind": "file-exists", "target": "c.txt"},
            ]
        },
    }
    draft = _synthesize_stage_draft(goal, stage, previous_evidence=None, now=NOW)
    targets = [c["target"] for c in draft["acceptance"]["checks"]]
    assert "b.txt" in targets, "any[] branch must populate acceptance.checks"
    assert "c.txt" in targets
    # And it must NOT have fallen back to the placeholder
    assert all(c["kind"] != "artifact-present" for c in draft["acceptance"]["checks"])


def test_synthesize_walks_nested_combinators(tmp_path: Path) -> None:
    goal: dict = {"objective": "x"}
    stage: dict = {
        "id": "s1",
        "title": "实现",
        "spec": {
            "all": [
                {"judge": "machine", "kind": "file-exists", "target": "a.txt"},
                {
                    "any": [
                        {"judge": "machine", "kind": "file-exists", "target": "b.txt"},
                        {"judge": "machine", "kind": "file-exists", "target": "c.txt"},
                    ]
                },
            ]
        },
    }
    draft = _synthesize_stage_draft(goal, stage, previous_evidence=None, now=NOW)
    targets = [c["target"] for c in draft["acceptance"]["checks"]]
    assert set(targets) == {"a.txt", "b.txt", "c.txt"}


def test_synthesize_skips_user_criteria(tmp_path: Path) -> None:
    """User-judged criteria have no kind/target; synthesis must not
    fall back to the placeholder just because the spec contains them."""
    goal: dict = {"objective": "x"}
    stage: dict = {
        "id": "s1",
        "title": "实现",
        "spec": {
            "all": [
                {"judge": "machine", "kind": "file-exists", "target": "x.txt"},
                {"judge": "user", "question": "is it good?"},
            ]
        },
    }
    draft = _synthesize_stage_draft(goal, stage, previous_evidence=None, now=NOW)
    targets = [c["target"] for c in draft["acceptance"]["checks"]]
    assert targets == ["x.txt"]


# ── _synthesize_stage_draft: spec body → deadline / budget ──────────


def test_synthesize_forwards_deadline_from_stage_budget(tmp_path: Path) -> None:
    """Stage declares deadline_at under time_budget; synthesized draft
    must use it (not the 24h fallback)."""
    goal: dict = {"objective": "x"}
    stage: dict = {
        "id": "s1",
        "title": "实现",
        "spec": {"all": [{"judge": "machine", "kind": "file-exists", "target": "x"}]},
        "time_budget": {"deadline_at": "2026-12-31T00:00:00+00:00"},
    }
    draft = _synthesize_stage_draft(goal, stage, previous_evidence=None, now=NOW)
    assert draft["deadline_at"] == "2026-12-31T00:00:00+00:00"


def test_synthesize_forwards_budget_from_stage(tmp_path: Path) -> None:
    """Stage declares budget; synthesized draft must use it (not the
    default max_dispatches=10)."""
    goal: dict = {"objective": "x"}
    stage: dict = {
        "id": "s1",
        "title": "实现",
        "spec": {"all": [{"judge": "machine", "kind": "file-exists", "target": "x"}]},
        "budget": {
            "max_retries": 2,
            "max_dispatches": 4,
            "max_concurrent_attempts": 3,
            "max_attempt_minutes": 45,
        },
    }
    draft = _synthesize_stage_draft(goal, stage, previous_evidence=None, now=NOW)
    assert draft["budget"]["max_dispatches"] == 4
    assert draft["budget"]["max_concurrent_attempts"] == 3
    assert draft["budget"]["max_attempt_minutes"] == 45


def test_synthesize_forwards_dependencies_artifacts_scope(tmp_path: Path) -> None:
    goal: dict = {"objective": "x"}
    stage: dict = {
        "id": "s1",
        "title": "实现",
        "spec": {"all": [{"judge": "machine", "kind": "file-exists", "target": "x"}]},
        "dependencies": ["other-stage-finished"],
        "artifacts": ["report.md", "data.json"],
        "permissions": {"modifiable_scope": ["src/"]},
    }
    draft = _synthesize_stage_draft(goal, stage, previous_evidence=None, now=NOW)
    assert draft["context"]["dependencies"] == ["other-stage-finished"]
    assert draft["context"]["expected_artifacts"] == ["report.md", "data.json"]
    assert draft["context"]["modifiable_scope"] == ["src/"]
    assert draft["hard_constraints"]["modifiable_scope"] == ["src/"]


# ── _has_recent_plan_approval: spec_hash matrix ───────────────────


def _make_conn_with_spec(
    tmp_path: Path, *, spec_hash: str | None
) -> tuple[sqlite3.Connection, str]:
    conn = _connect(tmp_path)
    draft = {
        "title": "t",
        "objective": "o",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {},
        "acceptance": {
            "standard": "s",
            "checks": ["c1"],
            "verifier": "cross_check",
            "spec_hash": spec_hash,
        },
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 5,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1_048_576,
        },
    }
    envelope = RequestEnvelope(
        method=Method.GOAL_PREPARE,
        request_id="req-1",
        client_id="mcp",
        protocol_version=2,
        params={"contract_id": "lt-spec-hash", "draft": draft},
    )
    handle_goal_prepare(envelope, conn=conn, now=NOW)
    return conn, "lt-spec-hash"


def _write_approval(conn: sqlite3.Connection, cid: str, stored_hash) -> None:
    payload: dict = {
        "contract_revision": 1,
        "accepted_check_ids": ["c1"],
        "auto_approved": True,
    }
    if stored_hash is not None:
        payload["spec_hash"] = stored_hash
    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.PLAN_APPROVED,
        payload=payload,
        now=NOW,
        actor="daemon",
    )


def test_plan_gate_hash_match(tmp_path: Path) -> None:
    conn, cid = _make_conn_with_spec(tmp_path, spec_hash="hash-x")
    _write_approval(conn, cid, stored_hash="hash-x")
    assert _has_recent_plan_approval(conn, cid, 1, ["c1"], "hash-x", NOW) is True


def test_plan_gate_hash_mismatch_rejects(tmp_path: Path) -> None:
    conn, cid = _make_conn_with_spec(tmp_path, spec_hash="hash-new")
    _write_approval(conn, cid, stored_hash="hash-old")
    assert _has_recent_plan_approval(conn, cid, 1, ["c1"], "hash-new", NOW) is False


def test_plan_gate_contract_lost_spec_rejects(tmp_path: Path) -> None:
    """Spec was attached at approval time but is now None on the
    contract; the gate must reject (avoid running a plan written
    against a spec the contract no longer has)."""
    conn, cid = _make_conn_with_spec(tmp_path, spec_hash="hash-x")
    _write_approval(conn, cid, stored_hash="hash-x")
    assert _has_recent_plan_approval(conn, cid, 1, ["c1"], None, NOW) is False


def test_plan_gate_approval_without_hash_accepts_when_contract_has_none(
    tmp_path: Path,
) -> None:
    """Legacy path: an approval without spec_hash binds to a contract
    that also has no spec_hash — both None, gate clears."""
    conn, cid = _make_conn_with_spec(tmp_path, spec_hash=None)
    _write_approval(conn, cid, stored_hash=None)
    assert _has_recent_plan_approval(conn, cid, 1, ["c1"], None, NOW) is True
