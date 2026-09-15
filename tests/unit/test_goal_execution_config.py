"""Goal-level execution config (5th-round follow-up, review #2).

The 5th-round external review (2026-09-08) found that a
spec-only stage synth path drops the user's ``workspace_root``
and ``executor_grant`` — the synthesised contract has no
executor to dispatch against and ends up
``BLOCKED(NO_EXECUTOR)``.  The fix moves these to
``Goal.plan.execution_config`` (user-pinned, Principal-gated
via ``goal/update``) and the synthesiser / inline-draft path
both inherit from it.
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
from lhgp.goals.stage import StageSpec
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    patch_goal,
    save_contract,
)
from longtask.persistence.stage_synth import synthesize_stage_draft
from longtask.persistence.store import (
    get_goal,
    list_contracts,
)
from longtask.rpc.handlers._lifecycle import auto_create_next_stage_contract

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.real_entry


def _stage_spec(target: str) -> dict:
    return StageSpec(
        goal="second stage",
        acceptance={
            "all": [
                {
                    "judge": "machine",
                    "kind": "file-exists",
                    "target": target,
                }
            ]
        },
    ).to_dict()


def _bootstrap_goal_with_execution_config(
    tmp_path: Path,
    *,
    workspace_root: str = "/data/runs/test",
    executor_grant: list[dict] | None = None,
) -> tuple[sqlite3.Connection, str, str]:
    """Create a Goal with ``plan.execution_config`` and return
    ``(conn, goal_id, stage1_cid)``.  Stage 1 is bound to a
    real (executor-equipped) contract so the
    ``_resolve_previous_auto_approve`` fallback path is
    exercised; the auto-create-next-stage flow creates stage 2
    from a spec-only stage entry."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    goal_id = "lt-exec-cfg-goal"
    stage1_cid = "lt-exec-cfg-stage1"
    # Bootstrap the goal via save_contract (auto-creates the
    # goal row).
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
            auto_approve=AutoApprove(enabled=True, actions=("read file",)),
        ),
        contract_id=f"{goal_id}-bootstrap",
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    # Bind stage 1 to a real contract (so resolve-previous finds
    # it) and write the execution_config.
    save_contract(
        conn,
        draft=ContractDraft(
            title="stage 1",
            objective="first stage",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
            auto_approve=AutoApprove(enabled=True, actions=("read file",)),
        ),
        contract_id=stage1_cid,
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "stages": [
                {"id": "stage-1", "contract_id": stage1_cid},
                {
                    "id": "stage-2",
                    "spec": _stage_spec("summary.md"),
                },
            ],
            "pre_authorized": {
                "enabled": True,
                "actions": ["read file", "write file"],
            },
            "execution_config": {
                "workspace_root": workspace_root,
                "executor_grant": executor_grant
                if executor_grant is not None
                else [
                    {
                        "executor_id": "exec-default",
                        "models": ["*"],
                        "roles": ["executor"],
                    },
                    {
                        "executor_id": "ver-default",
                        "models": ["*"],
                        "roles": ["verifier"],
                    },
                ],
            },
        },
    )
    return conn, goal_id, stage1_cid


def test_synthesize_inherits_workspace_from_goal(tmp_path: Path) -> None:
    """Spec-only synthesised draft must carry the Goal's
    ``execution_config.workspace_root`` into
    ``hard_constraints.file_effects.workspace_root`` so the
    dispatcher can resolve the executor's cwd.
    """
    conn, goal_id, _ = _bootstrap_goal_with_execution_config(
        tmp_path, workspace_root="/data/runs/synth"
    )
    goal = get_goal(conn, goal_id)
    stage2 = goal["plan"]["stages"][1]
    synth = synthesize_stage_draft(goal, stage2, previous_evidence=None, now=NOW, conn=conn)
    file_effects = synth["hard_constraints"].get("file_effects") or {}
    assert file_effects.get("workspace_root") == "/data/runs/synth", (
        f"spec-only synth must inherit workspace_root from Goal.execution_config; "
        f"got {file_effects!r}"
    )
    assert file_effects.get("mode") == "workspace-write"
    conn.close()


def test_synthesize_inherits_executor_grant_from_goal(tmp_path: Path) -> None:
    """Spec-only synthesised draft must carry the Goal's
    ``execution_config.executor_grant`` into
    ``authority.executors`` so the dispatcher finds an
    eligible executor (no more BLOCKED(NO_EXECUTOR)).
    """
    conn, goal_id, _ = _bootstrap_goal_with_execution_config(
        tmp_path,
        executor_grant=[
            {
                "executor_id": "exec-s12",
                "models": ["*"],
                "roles": ["executor"],
            },
            {
                "executor_id": "ver-s12",
                "models": ["*"],
                "roles": ["verifier"],
            },
        ],
    )
    goal = get_goal(conn, goal_id)
    stage2 = goal["plan"]["stages"][1]
    synth = synthesize_stage_draft(goal, stage2, previous_evidence=None, now=NOW, conn=conn)
    authority = synth.get("authority") or {}
    executors = authority.get("executors") or []
    assert [e["executor_id"] for e in executors] == ["exec-s12", "ver-s12"]
    assert authority.get("executor_policy") == "explicit_allow"
    conn.close()


def test_inline_stage_draft_inherits_workspace_when_missing(
    tmp_path: Path,
) -> None:
    """An inline stage draft that omits ``file_effects`` must
    still inherit the Goal's ``execution_config.workspace_root``
    — a stage author who provides a draft but forgets the
    workspace must not silently end up with no-executor.
    """
    conn, goal_id, _ = _bootstrap_goal_with_execution_config(
        tmp_path, workspace_root="/data/runs/inline"
    )
    goal = get_goal(conn, goal_id)
    # Inject an inline stage-2 draft (no file_effects) on top
    # of the existing stage-2 spec entry — replace just stage-2
    # in the stages list.  Preserve the Goal-level
    # ``execution_config`` and ``pre_authorized`` so the
    # inherit-from-Goal fallback path is exercised.
    new_stages = []
    for s in goal["plan"]["stages"]:
        if s.get("id") == "stage-2":
            new_stages.append(
                {
                    "id": "stage-2",
                    "spec": _stage_spec("summary.md"),
                    "draft": {
                        "title": "stage 2 inline",
                        "objective": "second stage",
                        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                        "hard_constraints": {},
                        "acceptance": {
                            "standard": "x",
                            "verifier": "cross_check",
                            "checks": [
                                {
                                    "kind": "file-exists",
                                    "target": "summary.md",
                                    "mandatory": True,
                                }
                            ],
                        },
                        "workload_estimate": {"initial_hours": 1.0},
                        "budget": {
                            "max_dispatches": 5,
                            "max_escalations": 1,
                            "max_concurrent_attempts": 1,
                            "max_attempt_minutes": 30,
                            "max_output_bytes": 1_048_576,
                        },
                        "context": {},
                        # NOTE: no file_effects, no authority
                    },
                }
            )
        else:
            new_stages.append(s)
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=2,
        actor="user",
        plan={
            "stages": new_stages,
            "pre_authorized": goal["plan"].get("pre_authorized"),
            "execution_config": goal["plan"].get("execution_config"),
        },
    )
    # Advance progress so ``progress.current == "stage-2"``;
    # the lifecycle helper reads this to decide which stage to
    # create a contract for.
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=3,
        actor="user",
        progress={"current": "stage-2", "completed": ["stage-1"]},
    )
    # Drive the inline-draft path through the lifecycle helper
    # (it builds a RequestEnvelope, calls handle_goal_prepare,
    # and auto-approves the new contract).
    stage1_view = get_contract(conn, "lt-exec-cfg-stage1")
    auto_create_next_stage_contract(conn, stage1_view, NOW)
    new_contracts = [
        c
        for c in list_contracts(conn)
        if c.contract_id not in ("lt-exec-cfg-stage1", f"{goal_id}-bootstrap")
    ]
    assert len(new_contracts) == 1
    new = new_contracts[0]
    file_effects = new.draft.hard_constraints.get("file_effects") or {}
    assert file_effects.get("workspace_root") == "/data/runs/inline", (
        f"inline stage without file_effects must inherit from Goal.execution_config; "
        f"got {file_effects!r}"
    )
    assert file_effects.get("mode") == "workspace-write"
    # Executor grant should also be inherited.
    executors = new.draft.authority.executors
    assert [e.executor_id for e in executors] == ["exec-default", "ver-default"], (
        f"inline stage without authority must inherit from Goal.execution_config; "
        f"got {[e.executor_id for e in executors]!r}"
    )
    conn.close()


def test_explicit_stage_workspace_wins_over_goal(tmp_path: Path) -> None:
    """A stage that explicitly pins its own workspace_root
    wins over the Goal's pinned ``execution_config.workspace_root``.
    The inline-draft path keeps the per-stage value, the
    spec-only path falls back to the Goal.
    """
    conn, goal_id, _ = _bootstrap_goal_with_execution_config(
        tmp_path, workspace_root="/data/runs/goal-default"
    )
    goal = get_goal(conn, goal_id)
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=2,
        actor="user",
        plan={
            "stages": [
                {
                    "id": "stage-2",
                    "spec": _stage_spec("summary.md"),
                    "draft": {
                        "title": "stage 2 explicit",
                        "objective": "second stage",
                        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                        "hard_constraints": {
                            "file_effects": {
                                "mode": "workspace-write",
                                "workspace_root": "/data/runs/stage-explicit",
                            }
                        },
                        "acceptance": {
                            "standard": "x",
                            "verifier": "cross_check",
                            "checks": [
                                {
                                    "kind": "file-exists",
                                    "target": "summary.md",
                                    "mandatory": True,
                                }
                            ],
                        },
                        "workload_estimate": {"initial_hours": 1.0},
                        "budget": {
                            "max_dispatches": 5,
                            "max_escalations": 1,
                            "max_concurrent_attempts": 1,
                            "max_attempt_minutes": 30,
                            "max_output_bytes": 1_048_576,
                        },
                        "context": {},
                        "authority": {
                            "executor_policy": "explicit_allow",
                            "executors": [
                                {
                                    "executor_id": "exec-stage",
                                    "models": ["*"],
                                    "roles": ["executor"],
                                }
                            ],
                        },
                    },
                },
            ],
            "pre_authorized": goal["plan"].get("pre_authorized"),
            "execution_config": goal["plan"].get("execution_config"),
        },
    )
    # Advance progress so progress.current == "stage-2"; the
    # lifecycle helper reads this to decide which stage to
    # create a contract for.
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=3,
        actor="user",
        progress={"current": "stage-2", "completed": ["stage-1"]},
    )
    # Trigger the auto-create-next-stage path (inline-draft
    # branch).
    auto_create_next_stage_contract(conn, get_contract(conn, "lt-exec-cfg-stage1"), NOW)
    new = next(c for c in list_contracts(conn) if c.contract_id != "lt-exec-cfg-stage1")
    file_effects = new.draft.hard_constraints.get("file_effects") or {}
    assert file_effects.get("workspace_root") == "/data/runs/stage-explicit", (
        f"explicit per-stage workspace must win; got {file_effects!r}"
    )
    executors = new.draft.authority.executors
    assert [e.executor_id for e in executors] == ["exec-stage"]
    conn.close()


def test_no_executor_grant_blocks_synthesized_contract(tmp_path: Path) -> None:
    """Without the Goal's ``execution_config`` and without an
    inline draft, the spec-only synthesised contract has no
    executor — the dispatcher would mark it BLOCKED.  Pin
    the regression so the test suite catches any future
    silent loss of the inherit-from-Goal flow.
    """
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    goal_id = "lt-no-grant"
    save_contract(
        conn,
        draft=ContractDraft(
            title="bootstrap",
            objective="x",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=f"{goal_id}-bootstrap",
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    # No execution_config; spec-only stage 2.
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={
            "stages": [
                {"id": "stage-1", "contract_id": f"{goal_id}-bootstrap"},
                {"id": "stage-2", "spec": _stage_spec("summary.md")},
            ]
        },
    )
    goal = get_goal(conn, goal_id)
    stage2 = goal["plan"]["stages"][1]
    synth = synthesize_stage_draft(goal, stage2, previous_evidence=None, now=NOW, conn=conn)
    # No authority inherited from Goal: dispatcher would
    # BLOCK(NO_EXECUTOR).  Pin the regression.
    assert not synth.get("authority", {}).get("executors"), (
        "spec-only stage without execution_config.executor_grant "
        "should produce a contract with no executors (dispatcher "
        "will BLOCK(NO_EXECUTOR) — the user must set "
        "Goal.plan.execution_config.executor_grant)"
    )
    conn.close()
