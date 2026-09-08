"""User confirmation path: CANDIDATE → PASSED transition.

3rd-round review (2026-09-08) found that a contract whose spec
has a ``judge == "user"`` criterion lands in
``acceptance_status = CANDIDATE`` after a verifier pass, and
the dispatcher deliberately skips it. But no code path
transitioned the contract out of CANDIDATE — the user-confirm
transition was missing.  This file pins the new
``contract/user-confirm`` tool (Principal-gated) and the state
machine permission for the transition.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import AcceptanceStatus
from lhgp.contracts.state_machine import is_valid_acceptance_transition
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope
from longtask.contracts.schema import ContractState
from longtask.mcp_server import tool_user_confirm_spec_verdict
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _seed_candidate_contract(
    tmp_path: Path, *, client_id: str = "cli"
) -> tuple[sqlite3.Connection, str, RequestEnvelope]:
    """Contract with a user-judged criterion, parked in CANDIDATE."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-uc-1"
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "summary.md"},
            {"judge": "user", "question": "summary 满意吗？"},
        ],
    }
    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",), spec=spec, spec_hash="h-uc"),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW,
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    envelope = RequestEnvelope(
        method=Method.CONTRACT_USER_CONFIRM,
        request_id="req-uc-1",
        client_id=client_id,
        protocol_version=2,
        params={"contract_id": cid, "note": "looks good"},
    )
    return conn, cid, envelope


# ── state machine: the transition is now legal ────────────────


def test_state_machine_allows_candidate_to_passed() -> None:
    """The new transition the user-confirm tool performs is
    explicitly permitted by the state machine."""
    assert (
        is_valid_acceptance_transition(AcceptanceStatus.CANDIDATE, AcceptanceStatus.PASSED) is True
    )
    # PENDING → PASSED remains the verifier path (unchanged).
    assert (
        is_valid_acceptance_transition(AcceptanceStatus.PENDING, AcceptanceStatus.PASSED) is False
    )
    # PASSED is terminal.
    assert (
        is_valid_acceptance_transition(AcceptanceStatus.PASSED, AcceptanceStatus.CANDIDATE) is False
    )


# ── principal gate ─────────────────────────────────────────────


def test_model_client_cannot_user_confirm(tmp_path: Path) -> None:
    """A model client calling the user-confirm tool is rejected
    with AUTH_FAILED — only the user can mark a user criterion
    resolved."""
    conn, cid, envelope = _seed_candidate_contract(tmp_path, client_id="mcp")
    with pytest.raises(RpcError) as exc_info:
        tool_user_confirm_spec_verdict(
            envelope.params,
            ctx={"conn": conn, "now": NOW, "envelope": envelope},
        )
    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    # CANDIDATE state preserved (no accidental change)
    from longtask.persistence.store import get_contract

    assert get_contract(conn, cid).acceptance_status == AcceptanceStatus.CANDIDATE
    conn.close()


def test_unknown_client_rejected(tmp_path: Path) -> None:
    conn, _cid, envelope = _seed_candidate_contract(tmp_path, client_id="rogue")
    with pytest.raises(RpcError) as exc_info:
        tool_user_confirm_spec_verdict(
            envelope.params,
            ctx={"conn": conn, "now": NOW, "envelope": envelope},
        )
    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    conn.close()


# ── happy path ─────────────────────────────────────────────────


def test_user_confirm_moves_candidate_to_passed(tmp_path: Path) -> None:
    """A user-class client (CLI) can confirm a CANDIDATE contract.
    The contract transitions to ``acceptance_status = PASSED``,
    the state moves to ``COMPLETE`` (full-completion path, not
    just a status flip), and a ``ACCEPTANCE_STATUS_CHANGED``
    audit event is written with the resolved principal as the
    actor."""
    conn, cid, envelope = _seed_candidate_contract(tmp_path, client_id="cli")
    result = tool_user_confirm_spec_verdict(
        envelope.params,
        ctx={"conn": conn, "now": NOW, "envelope": envelope},
    )
    assert result["user_confirmed"] is True
    assert result["acceptance_status"] == "passed"
    assert result["contract_state"] == "complete"
    # DB state
    from longtask.persistence.store import get_contract

    view = get_contract(conn, cid)
    assert view.acceptance_status == AcceptanceStatus.PASSED
    assert view.state == ContractState.COMPLETE
    # Audit event
    audits = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.ACCEPTANCE_STATUS_CHANGED.value
        and (e.payload_json or "").find("user-confirmed") >= 0
    ]
    assert len(audits) == 1
    assert audits[0].actor == "user", (
        f"actor must be the resolved Principal; got {audits[0].actor!r}"
    )
    # Full-completion audit event also written (filtered to the
    # user_confirm one — earlier events in the seed are
    # state-only transitions, not CONTRACT_COMPLETED).
    completed = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.CONTRACT_COMPLETED.value
        and (e.payload_json or "").find("user_confirmed") >= 0
    ]
    assert len(completed) == 1, (
        f"expected one user_confirm CONTRACT_COMPLETED event, got {len(completed)}"
    )
    assert completed[0].actor == "user"
    conn.close()


def test_model_rejected_when_ctx_has_no_envelope(tmp_path: Path) -> None:
    """4th-round regression: the real MCP stdio context does NOT
    carry an ``envelope`` key.  A model client that goes through
    the tool with the legacy context shape must still be
    rejected — the Principal gate lives in the handler, not the
    wrapper, so the bypass that the 3rd-round unit tests
    permitted is closed."""
    conn, cid, _envelope = _seed_candidate_contract(tmp_path, client_id="mcp")
    # ctx without ``envelope`` — the shape the real MCP server
    # builds (see :func:`longtask.mcp_server._make_context`).
    with pytest.raises(RpcError) as exc_info:
        tool_user_confirm_spec_verdict(
            {"contract_id": cid, "note": "model self-signing"},
            ctx={"conn": conn, "now": NOW},
        )
    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    # State preserved
    from longtask.persistence.store import get_contract

    view = get_contract(conn, cid)
    assert view.acceptance_status == AcceptanceStatus.CANDIDATE
    assert view.state == ContractState.ACTIVE
    conn.close()


def test_user_confirm_advances_goal_stage(tmp_path: Path) -> None:
    """Full-completion path: when the confirmed contract is bound
    to a Goal stage, the user-confirm handler advances the Goal
    so the next contract is generated by the next tick (instead
    of the contract being re-dispatched forever)."""
    from lhgp.goals.stage import StageSpec
    from longtask.cli.tick import _synthesize_stage_draft
    from longtask.persistence.store import get_goal, patch_goal

    conn, cid, envelope = _seed_candidate_contract(tmp_path, client_id="cli")
    # Seed a 2-stage plan on the same goal; stage-1 binds the
    # current contract so advancement is allowed.
    goal_id = "lt-uc-1"  # the seeded contract's goal_id
    s1_envelope = StageSpec(
        goal="first goal",
        acceptance={"all": [{"judge": "user", "question": "summary 满意吗？"}]},
    ).to_dict()
    s2_envelope = StageSpec(
        goal="second goal",
        acceptance={"all": [{"judge": "machine", "kind": "file-exists", "target": "out.md"}]},
    ).to_dict()
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "stages": [
                {"id": "s1", "title": "first", "spec": s1_envelope, "contract_id": cid},
                {"id": "s2", "title": "second", "spec": s2_envelope},
            ]
        },
    )
    # User-confirm — should advance Goal from s1 → s2.
    tool_user_confirm_spec_verdict(
        envelope.params,
        ctx={"conn": conn, "now": NOW, "envelope": envelope},
    )
    goal = get_goal(conn, goal_id)
    assert goal["progress"]["current"] == "s2", (
        f"user-confirm must advance Goal stage; got {goal['progress']!r}"
    )
    # The synthesized next-stage draft must pass contract/prepare
    # (4th-round fix in _synthesize_stage_draft).
    plan = goal["plan"]
    s2 = next(s for s in plan["stages"] if s["id"] == "s2")
    s2_draft = _synthesize_stage_draft(
        goal, s2, previous_evidence=None, now=NOW + timedelta(hours=1)
    )
    from longtask.rpc.handlers._common import parse_contract_draft

    parsed = parse_contract_draft(s2_draft)
    assert parsed.acceptance.spec is not None
    assert parsed.acceptance.spec_hash is not None
    assert parsed.title == "second"  # from stage.title
    conn.close()


def test_user_confirm_rejects_non_candidate(tmp_path: Path) -> None:
    """A user-confirm on a non-CANDIDATE contract is rejected
    with VALIDATION_FAILED — guards against accidentally
    promoting a not-yet-passed or already-passed contract."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-uc-2"
    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    # Activate but stay in PENDING (default) — not CANDIDATE.
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW,
    )
    envelope = RequestEnvelope(
        method=Method.CONTRACT_USER_CONFIRM,
        request_id="req-uc-2",
        client_id="cli",
        protocol_version=2,
        params={"contract_id": cid, "note": "trying anyway"},
    )
    with pytest.raises(RpcError) as exc_info:
        tool_user_confirm_spec_verdict(
            envelope.params,
            ctx={"conn": conn, "now": NOW, "envelope": envelope},
        )
    assert exc_info.value.code == ErrorCode.VALIDATION_FAILED
    assert "CANDIDATE" in str(exc_info.value)
    conn.close()
