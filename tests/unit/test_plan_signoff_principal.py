"""Server-side Principal check for plan sign-off.

3rd-round review found that ``lhgp_plan_signoff`` took the caller's
``signoff_by`` verbatim and wrote the event with that actor.  A
model client could pass ``signoff_by="user:human"`` and promote
its own plan as if a user had signed off.  The fix routes the
actor through ``require_principal`` which gates the action on
``envelope.client_id``.

This file tests the gate at three layers:

1. The MCP ``tool_plan_signoff`` handler rejects a model-client
   call with ``AUTH_FAILED`` even when the caller passes
   ``signoff_by="user:human"``.
2. The recorded actor on the ``PLAN_APPROVED`` event is the
   resolved principal, not the caller's claim.
3. The CLI ``lhgp plan signoff`` path requires a user-class client
   via the same gate (CLI is a user client, so the gate clears).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope
from longtask.contracts.schema import ContractState
from longtask.mcp_server import tool_plan_signoff
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _seed_contract_with_plan_gate(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    """A contract that requires signoff (auto_approve off, plan out of scope)."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-sig-1"
    # auto_approve off, so a plan with 'write file' triggers
    # requires_signoff. The CLI must succeed; the model client
    # must be rejected.
    draft = ContractDraft(
        title="t",
        objective="ship the c1 deliverable",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    return conn, cid


def test_model_client_cannot_sign_off(tmp_path: Path) -> None:
    """A model client's request — even with ``signoff_by="user:human"``
    in the body — must be rejected by the Principal gate."""
    conn, cid = _seed_contract_with_plan_gate(tmp_path)
    envelope = RequestEnvelope(
        method=Method.CONTRACT_REQUEST_VERIFICATION,  # any valid Method
        request_id="req-sig-1",
        client_id="mcp",  # model client
        protocol_version=2,
        params={
            "contract_id": cid,
            "signoff_by": "user:human",  # attacker's claim
            "steps": [
                {
                    "step_id": 1,
                    "action": "write file",
                    "target": "build/c1",
                    "rationale": "for the spec",
                    "expected_outcome": "file appears",
                }
            ],
        },
    )
    with pytest.raises(RpcError) as exc_info:
        tool_plan_signoff(envelope.params, ctx={"conn": conn, "now": NOW, "envelope": envelope})
    assert exc_info.value.code == ErrorCode.AUTH_FAILED, (
        f"model client must be rejected, got {exc_info.value.code}"
    )
    assert "Principal" in exc_info.value.message or "user" in exc_info.value.message
    # No PLAN_APPROVED event should have been written.
    approvals = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.PLAN_APPROVED.value
    ]
    assert approvals == [], "no plan should be approved by a model client"
    conn.close()


def test_user_client_signoff_records_resolved_actor(tmp_path: Path) -> None:
    """A user-class client (CLI) must succeed; the recorded actor
    is the resolved Principal, not the caller's claim."""
    conn, cid = _seed_contract_with_plan_gate(tmp_path)
    envelope = RequestEnvelope(
        method=Method.CONTRACT_REQUEST_VERIFICATION,
        request_id="req-sig-2",
        client_id="cli",  # user client
        protocol_version=2,
        params={
            "contract_id": cid,
            "signoff_by": "user:human",  # ignored
            "steps": [
                {
                    "step_id": 1,
                    "action": "write file",
                    "target": "build/c1",
                    "rationale": "ship the c1 deliverable per objective",
                    "expected_outcome": "c1 produced",
                }
            ],
        },
    )
    result = tool_plan_signoff(
        envelope.params, ctx={"conn": conn, "now": NOW, "envelope": envelope}
    )
    assert result["signed_off"] is True, f"user signoff should succeed; got {result}"
    approvals = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.PLAN_APPROVED.value
    ]
    assert len(approvals) == 1
    # The recorded actor is the resolved Principal, not the
    # caller's claim.
    assert approvals[0].actor == "user", (
        f"actor must be the resolved Principal; got {approvals[0].actor!r}"
    )
    payload = json.loads(approvals[0].payload_json or "{}")
    assert payload.get("signed_off_by") == "user", (
        f"signed_off_by must be the resolved Principal; got {payload.get('signed_off_by')!r}"
    )
    conn.close()


def test_unknown_client_rejected(tmp_path: Path) -> None:
    conn, cid = _seed_contract_with_plan_gate(tmp_path)
    envelope = RequestEnvelope(
        method=Method.CONTRACT_REQUEST_VERIFICATION,
        request_id="req-sig-3",
        client_id="rogue-script",  # not in trusted list
        protocol_version=2,
        params={
            "contract_id": cid,
            "signoff_by": "user:human",
            "steps": [
                {
                    "step_id": 1,
                    "action": "write file",
                    "target": "build/c1",
                    "rationale": "for the spec",
                    "expected_outcome": "file appears",
                }
            ],
        },
    )
    with pytest.raises(RpcError) as exc_info:
        tool_plan_signoff(envelope.params, ctx={"conn": conn, "now": NOW, "envelope": envelope})
    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    conn.close()


def test_mcp_runtime_without_envelope_rejected_with_principal_only_guidance(
    tmp_path: Path,
) -> None:
    """5th-round follow-up: when ``tool_plan_signoff`` is invoked
    via the MCP runtime (the runtime builds a ``ctx`` without
    an ``envelope`` key — the model is not in the RPC path),
    the previous INTERNAL error was a poor diagnosis.  The
    tool is Principal-only; surface it as ``AUTH_FAILED``
    with guidance to escalate to the user via CLI.
    """
    conn, _cid = _seed_contract_with_plan_gate(tmp_path)
    with pytest.raises(RpcError) as exc_info:
        # ctx without envelope key — the real MCP runtime shape.
        tool_plan_signoff(
            {
                "contract_id": "lt-uc-mcp",
                "steps": [
                    {
                        "step_id": 1,
                        "action": "write file",
                        "target": "build/c1",
                        "rationale": "for the spec",
                        "expected_outcome": "file appears",
                    }
                ],
            },
            ctx={"conn": conn, "now": NOW},  # no envelope
        )
    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    assert "Principal" in str(exc_info.value)
    assert "CLI" in str(exc_info.value)
    conn.close()
