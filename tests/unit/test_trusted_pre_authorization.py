"""Trusted pre-authorization for auto_approve (5th-round follow-up).

The 5th-round external review (2026-09-08) found that the daemon's
``auto_approve_drafted_contract`` check trusted the contract's
``draft.auto_approve.enabled`` field — a value the model itself
sets when calling the MCP path.  A model that called
``contract/prepare`` with
``{"auto_approve": {"enabled": true, "actions": ["write file"]}}``
saw the daemon auto-promote its own DRAFTED contract to ACTIVE
without any user sign-off.

This file pins the layered fix:

1. ``parse_contract_draft`` strips ``auto_approve`` when the caller
   is ``client_id="mcp"`` so a model cannot even *claim* a scope.
2. ``auto_approve_drafted_contract`` reads the trusted source:
   the contract's bound Goal's ``plan.pre_authorized`` (a dict
   the user pins at ``goal/update`` time, Principal-gated).  The
   contract's ``draft.auto_approve`` is now a *request* the
   model/client makes; auto-approve only fires when the
   user-pinned scope covers the request (or the user pinned
   a generic grant with no model claim).
3. The synthesised next-stage contract inherits the Goal's
   ``pre_authorized`` so multi-stage flows don't need the
   model to re-claim at every stage.
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
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope
from longtask.contracts.schema import ContractState
from longtask.persistence.store import (
    StoreConfig,
    auto_approve_drafted_contract,
    connect,
    ensure_schema,
    get_contract,
    patch_goal,
    save_contract,
)

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.real_entry


def _seed_drafted(
    tmp_path: Path,
    *,
    goal_id: str | None = None,
    auto_approve: AutoApprove | None = None,
) -> sqlite3.Connection:
    """Contract in DRAFTED, bound to ``goal_id`` if given."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-tpa-1"
    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=auto_approve or AutoApprove(),
    )
    save_contract(
        conn,
        draft=draft,
        contract_id=cid,
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    return conn


def test_mcp_caller_cannot_self_authorize(tmp_path: Path) -> None:
    """An MCP-issued contract with ``auto_approve: {enabled: true}``
    is parsed as ``enabled=False``.  Defense in depth: even though
    ``auto_approve_drafted_contract`` now reads the Goal's
    ``pre_authorized`` (not the contract's claim), the parse-time
    strip ensures the contract view the dispatcher sees is
    honest about what the model set.
    """
    from longtask.rpc.handlers._common import parse_contract_draft

    envelope = RequestEnvelope(
        method=Method.CONTRACT_PREPARE,
        request_id="req-tpa-mcp",
        client_id="mcp",
        protocol_version=2,
        params={
            "contract_id": "lt-tpa-mcp",
            "draft": {
                "title": "t",
                "objective": "objective",
                "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                "hard_constraints": {},
                "acceptance": {"standard": "s", "checks": ["c1"]},
                "workload_estimate": {"initial_hours": 1.0},
                "budget": {
                    "max_dispatches": 5,
                    "max_escalations": 1,
                    "max_concurrent_attempts": 1,
                    "max_attempt_minutes": 30,
                    "max_output_bytes": 1_048_576,
                },
                "auto_approve": {
                    "enabled": True,
                    "actions": ["write file", "run command"],
                },
            },
        },
    )
    parsed = parse_contract_draft(envelope.params, envelope=envelope)
    assert parsed.auto_approve.enabled is False, (
        "MCP-issued contract must NOT carry the model's auto_approve claim; "
        "the trusted source is Goal.plan.pre_authorized (user-pinned)"
    )
    assert parsed.auto_approve.actions == ()


def test_cli_caller_can_still_set_auto_approve(tmp_path: Path) -> None:
    """The CLI path is Principal-gated; it can still set
    ``auto_approve`` directly.  Defense in depth only strips
    the MCP path, not the human-in-the-loop CLI path."""
    from longtask.rpc.handlers._common import parse_contract_draft

    envelope = RequestEnvelope(
        method=Method.CONTRACT_PREPARE,
        request_id="req-tpa-cli",
        client_id="cli",
        protocol_version=2,
        params={
            "contract_id": "lt-tpa-cli",
            "draft": {
                "title": "t",
                "objective": "objective",
                "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                "hard_constraints": {},
                "acceptance": {"standard": "s", "checks": ["c1"]},
                "workload_estimate": {"initial_hours": 1.0},
                "budget": {
                    "max_dispatches": 5,
                    "max_escalations": 1,
                    "max_concurrent_attempts": 1,
                    "max_attempt_minutes": 30,
                    "max_output_bytes": 1_048_576,
                },
                "auto_approve": {
                    "enabled": True,
                    "actions": ["write file"],
                },
            },
        },
    )
    parsed = parse_contract_draft(envelope.params, envelope=envelope)
    assert parsed.auto_approve.enabled is True
    assert parsed.auto_approve.actions == ("write file",)


def test_auto_approve_requires_goal_pre_authorized(tmp_path: Path) -> None:
    """A DRAFTED contract with ``auto_approve.enabled=True`` but no
    Goal binding, or a Goal without ``pre_authorized``, is NOT
    auto-approved.  The per-contract field is the model's claim;
    the Goal's ``pre_authorized`` is the user's grant.
    """
    conn = _seed_drafted(
        tmp_path,
        goal_id="lt-tpa-goal-1",
        auto_approve=AutoApprove(enabled=True, actions=("write file",)),
    )
    contract = get_contract(conn, "lt-tpa-1")
    assert contract is not None
    # No Goal.pre_authorized → no auto-approve.
    assert auto_approve_drafted_contract(conn, contract, NOW) is False
    assert get_contract(conn, "lt-tpa-1").state == ContractState.DRAFTED
    conn.close()


def test_auto_approve_fires_when_goal_pre_authorized_covers_claim(
    tmp_path: Path,
) -> None:
    """A DRAFTED contract bound to a Goal whose
    ``plan.pre_authorized`` covers the contract's claimed
    actions is auto-approved.  The user's grant at the Goal
    level is the trusted source.
    """
    conn = _seed_drafted(
        tmp_path,
        goal_id="lt-tpa-goal-2",
        auto_approve=AutoApprove(enabled=True, actions=("write file", "run command")),
    )
    patch_goal(
        conn,
        goal_id="lt-tpa-goal-2",
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "stages": [{"id": "s1", "title": "first"}],
            "pre_authorized": {
                "enabled": True,
                "actions": ["write file", "run command"],
            },
        },
    )
    contract = get_contract(conn, "lt-tpa-1")
    assert contract is not None
    assert auto_approve_drafted_contract(conn, contract, NOW) is True
    assert get_contract(conn, "lt-tpa-1").state == ContractState.ACTIVE
    conn.close()


def test_auto_approve_rejects_claim_outside_goal_scope(tmp_path: Path) -> None:
    """The model cannot escalate beyond the user's grant.  A
    contract that claims ``rm -rf`` when the Goal's
    ``pre_authorized`` only grants ``write file`` is NOT
    auto-approved.
    """
    conn = _seed_drafted(
        tmp_path,
        goal_id="lt-tpa-goal-3",
        auto_approve=AutoApprove(enabled=True, actions=("write file", "rm -rf /")),
    )
    patch_goal(
        conn,
        goal_id="lt-tpa-goal-3",
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "stages": [{"id": "s1", "title": "first"}],
            "pre_authorized": {
                "enabled": True,
                "actions": ["write file"],
            },
        },
    )
    contract = get_contract(conn, "lt-tpa-1")
    assert contract is not None
    assert auto_approve_drafted_contract(conn, contract, NOW) is False
    assert get_contract(conn, "lt-tpa-1").state == ContractState.DRAFTED
    conn.close()


def test_goal_pre_authorized_grants_generic_auto_approve(tmp_path: Path) -> None:
    """A user-pinned Goal-level pre-authorization with no
    specific actions still grants a generic auto-approve:
    the user said "this whole Goal is pre-authorised" so the
    synthesised next-stage contracts (which never claim any
    action scope) can be auto-approved without a per-contract
    claim.
    """
    conn = _seed_drafted(
        tmp_path,
        goal_id="lt-tpa-goal-4",
        auto_approve=AutoApprove(),  # empty claim
    )
    patch_goal(
        conn,
        goal_id="lt-tpa-goal-4",
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "stages": [{"id": "s1", "title": "first"}],
            "pre_authorized": {
                "enabled": True,
                "actions": ["write file", "run command"],
            },
        },
    )
    contract = get_contract(conn, "lt-tpa-1")
    assert contract is not None
    assert auto_approve_drafted_contract(conn, contract, NOW) is True
    assert get_contract(conn, "lt-tpa-1").state == ContractState.ACTIVE
    conn.close()


def test_goal_pre_authorized_disabled_blocks_auto_approve(
    tmp_path: Path,
) -> None:
    """A Goal with ``pre_authorized.enabled=False`` blocks
    auto-approve even when the contract claims a covered
    scope.  The user explicitly opted out at the Goal level.
    """
    conn = _seed_drafted(
        tmp_path,
        goal_id="lt-tpa-goal-5",
        auto_approve=AutoApprove(enabled=True, actions=("write file",)),
    )
    patch_goal(
        conn,
        goal_id="lt-tpa-goal-5",
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "stages": [{"id": "s1", "title": "first"}],
            "pre_authorized": {
                "enabled": False,
                "actions": ["write file"],
            },
        },
    )
    contract = get_contract(conn, "lt-tpa-1")
    assert contract is not None
    assert auto_approve_drafted_contract(conn, contract, NOW) is False
    assert get_contract(conn, "lt-tpa-1").state == ContractState.DRAFTED
    conn.close()
