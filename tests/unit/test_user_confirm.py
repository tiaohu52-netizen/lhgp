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
    The contract transitions to ``acceptance_status = PASSED`` and
    a ``ACCEPTANCE_STATUS_CHANGED`` event is written with the
    resolved principal as the actor."""
    conn, cid, envelope = _seed_candidate_contract(tmp_path, client_id="cli")
    result = tool_user_confirm_spec_verdict(
        envelope.params,
        ctx={"conn": conn, "now": NOW, "envelope": envelope},
    )
    assert result["user_confirmed"] is True
    assert result["acceptance_status"] == "passed"
    # DB state
    from longtask.persistence.store import get_contract

    view = get_contract(conn, cid)
    assert view.acceptance_status == AcceptanceStatus.PASSED
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
