"""Spec-only synthesizer carries ``auto_approve`` across stages.

The 3-stage submit-and-leave E2E (``test_3stage_submit_and_leave.py``)
exercises the **inline-draft** path through
``_auto_create_next_stage_contract``.  The 4th-round verifier
(V3, finding #1) flagged that the spec-only synthesizer path —
``stage.spec`` envelope, no inline ``draft`` — was not directly
covered.  This test fills that gap: the synthesizer's
``auto_approve`` inheritance must work without an inline draft,
and the result must round-trip through
``parse_contract_draft`` and ``auto_approve_drafted_contract``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.goals.stage import StageSpec
from longtask.cli.tick import _resolve_previous_auto_approve, _synthesize_stage_draft
from longtask.persistence.store import (
    StoreConfig,
    auto_approve_drafted_contract,
    connect,
    ensure_schema,
    get_contract,
    patch_goal,
    save_contract,
)
from longtask.rpc.handlers._common import parse_contract_draft

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _seed_stage1_with_auto_approve(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, str, dict]:
    """Save stage 1 with ``auto_approve`` enabled and the full
    action scope, then return (conn, stage1_cid, stage1_dict).
    """
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-spec-inherit-1"
    draft = ContractDraft(
        title="stage 1",
        objective="first goal",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(
            enabled=True,
            actions=("read file", "write file", "run command"),
            max_budget_increment=3,
            max_spec_changes=2,
        ),
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    return conn, cid, draft.to_dict()


def test_resolve_previous_auto_approve_returns_contract_scope(tmp_path: Path) -> None:
    """When the previous stage's contract is in the DB,
    ``_resolve_previous_auto_approve`` returns its exact
    ``auto_approve`` (not a copy, not the default).
    """
    conn, cid, _ = _seed_stage1_with_auto_approve(tmp_path)
    goal = {
        "plan": {
            "stages": [
                {"id": "s1", "contract_id": cid},
                {"id": "s2"},  # current stage, no contract_id yet
            ]
        }
    }
    prev = _resolve_previous_auto_approve(goal, {"id": "s2"}, conn)
    assert prev is not None
    assert prev.enabled is True
    assert prev.actions == ("read file", "write file", "run command")
    assert prev.max_budget_increment == 3
    assert prev.max_spec_changes == 2
    conn.close()


def test_resolve_returns_none_for_first_stage(tmp_path: Path) -> None:
    conn, cid, _ = _seed_stage1_with_auto_approve(tmp_path)
    goal = {
        "plan": {
            "stages": [
                {"id": "s1", "contract_id": cid},
            ]
        }
    }
    assert _resolve_previous_auto_approve(goal, {"id": "s1"}, conn) is None
    conn.close()


def test_resolve_returns_none_when_no_conn(tmp_path: Path) -> None:
    """Legacy callers (unit tests) pass ``conn=None``; the
    function must silently skip rather than raise.
    """
    goal = {
        "plan": {
            "stages": [
                {"id": "s1"},
                {"id": "s2"},
            ]
        }
    }
    assert _resolve_previous_auto_approve(goal, {"id": "s2"}, None) is None


def test_resolve_matches_by_id_not_by_dict_equality(tmp_path: Path) -> None:
    """The id-based match (defensive, 4th-round verifier 3
    finding #2) must succeed even when a JSON round-trip gives
    a fresh dict that is ``==``-equal to the original but
    identity-different.  We simulate this by looking up s2
    after the goal has been re-decoded from a string."""
    import json

    conn, cid, _ = _seed_stage1_with_auto_approve(tmp_path)
    original_goal = {
        "plan": {
            "stages": [
                {"id": "s1", "contract_id": cid},
                {"id": "s2", "spec": StageSpec(goal="second goal").to_dict()},
            ]
        }
    }
    re_decoded = json.loads(json.dumps(original_goal))
    # `stage` parameter is a fresh dict from the re-decoded plan,
    # identity-different but ``==``-equal to the original.
    fresh_stage = re_decoded["plan"]["stages"][1]
    prev = _resolve_previous_auto_approve(re_decoded, fresh_stage, conn)
    assert prev is not None
    assert prev.enabled is True
    conn.close()


def test_synthesize_inherits_auto_approve_for_spec_only_stage(tmp_path: Path) -> None:
    """End-to-end: stage 1 (with auto_approve) lives in the DB;
    stage 2 has only a spec (no inline draft); the synthesizer
    builds stage 2's draft with the inherited ``auto_approve``;
    the draft round-trips through ``parse_contract_draft`` and
    can be auto-promoted by ``auto_approve_drafted_contract``.
    """
    conn, cid, _ = _seed_stage1_with_auto_approve(tmp_path)
    goal = {
        "plan": {
            "stages": [
                {"id": "s1", "contract_id": cid},
                {
                    "id": "s2",
                    "spec": StageSpec(
                        goal="second goal",
                        acceptance={
                            "all": [
                                {
                                    "judge": "machine",
                                    "kind": "file-exists",
                                    "target": "summary.md",
                                }
                            ]
                        },
                    ).to_dict(),
                },
            ]
        }
    }
    s2 = goal["plan"]["stages"][1]
    synthesized = _synthesize_stage_draft(goal, s2, previous_evidence=None, now=NOW, conn=conn)
    assert "auto_approve" in synthesized, "spec-only synthesizer must carry auto_approve forward"
    assert synthesized["auto_approve"]["enabled"] is True
    assert "write file" in synthesized["auto_approve"]["actions"]

    # Round-trip through the contract RPC parser (the same path
    # ``contract/prepare`` would take).
    parsed = parse_contract_draft(synthesized)
    assert parsed.auto_approve.enabled is True
    assert "write file" in parsed.auto_approve.actions

    # Persist and auto-approve; the spec-only draft's
    # ``workspace_root`` is empty, but the spec-only path is
    # not blocked from auto-approve.
    #
    # 5th-round follow-up: the auto-approve primitive's trusted
    # source is the bound Goal's ``plan.pre_authorized`` (a
    # user-pinned dict).  Without a Goal binding and a
    # ``pre_authorized`` grant, the contract is NOT
    # auto-approved even if the contract's own auto_approve
    # field is True.  This is the model-self-authorize fix.
    new_cid = "lt-spec-inherit-2"
    new_goal_id = "lt-spec-inherit-goal"
    # Bootstrap the goal: ``save_contract`` with a new
    # ``goal_id`` auto-creates the goal row (see
    # ``save_contract`` ON CONFLICT) so ``patch_goal`` can
    # then CAS-update its plan.
    save_contract(
        conn,
        draft=ContractDraft(
            title="goal bootstrap",
            objective="x",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=f"{new_goal_id}-bootstrap",
        now=NOW,
        actor="user",
        goal_id=new_goal_id,
    )
    patch_goal(
        conn,
        goal_id=new_goal_id,
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "pre_authorized": {
                "enabled": True,
                "actions": ["read file", "write file", "run command"],
            },
        },
    )
    save_contract(
        conn,
        draft=ContractDraft(
            title=synthesized["title"],
            objective=synthesized["objective"],
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints=synthesized["hard_constraints"],
            acceptance=parsed.acceptance,
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
            auto_approve=parsed.auto_approve,
        ),
        contract_id=new_cid,
        now=NOW,
        actor="daemon",
        goal_id=new_goal_id,
    )
    view = get_contract(conn, new_cid)
    assert view is not None
    assert view.state.value == "drafted"
    promoted = auto_approve_drafted_contract(conn, view, NOW)
    assert promoted is True, (
        "auto_approve must fire when the bound Goal's "
        "plan.pre_authorized covers the contract's claimed actions"
    )
    view = get_contract(conn, new_cid)
    assert view.state.value == "active"
    conn.close()
