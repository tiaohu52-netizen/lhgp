"""Goal → next-stage contract orchestration.

Lives in ``longtask.rpc.handlers`` (not the daemon ``cli``)
so the contract RPC handler (e.g. ``handle_contract_user_confirm``)
can call it after advancing the Goal without violating the
``rpc → cli is forbidden`` arch rule.

Two paths to a draft:
1. ``stage.draft`` — model caller pre-supplied a draft. Used as-is.
2. ``stage.spec`` — structured stage spec. We synthesize a draft
   from the spec (title, objective, deadline, acceptance,
   budget) via :func:`synthesize_stage_draft`.

Failure is silent — the next ``goal/next`` call will surface
``create_contract`` so the caller can re-attempt with full
authority.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any

from longtask.persistence.stage_synth import synthesize_stage_draft
from longtask.persistence.store import (
    auto_approve_drafted_contract,
    get_contract,
    get_goal,
)


def auto_create_next_stage_contract(
    conn: sqlite3.Connection,
    contract: Any,
    now: datetime,
    previous_evidence: dict[str, Any] | None = None,
) -> None:
    """If the goal has a next stage without a bound contract, create it.

    Submit-and-leave: if the new contract is pre-authorised, push it
    from DRAFTED to ACTIVE so the dispatcher can pick it up without
    a follow-up call. Best-effort: any exception here is silent; the
    per-tick scan (``list_drafted_contracts``) will retry.
    """
    goal = get_goal(conn, contract.goal_id)
    if goal is None:
        return
    progress_raw = goal.get("progress")
    progress: dict[str, Any] = progress_raw if isinstance(progress_raw, dict) else {}
    next_stage_id = progress.get("current")
    if not next_stage_id:
        return
    plan_raw = goal.get("plan")
    plan: dict[str, Any] = plan_raw if isinstance(plan_raw, dict) else {}
    stages_raw = plan.get("stages")
    stages: list[Any] = stages_raw if isinstance(stages_raw, list) else []
    next_stage = next(
        (s for s in stages if isinstance(s, dict) and str(s.get("id", "")) == str(next_stage_id)),
        None,
    )
    if next_stage is None:
        return
    if next_stage.get("contract_id"):
        return
    inline_draft = next_stage.get("draft")
    if isinstance(inline_draft, dict):
        draft = dict(inline_draft)
        draft.setdefault("context", {})
        if previous_evidence:
            draft["context"]["previous_evidence"] = previous_evidence
        # 5th-round follow-up: even an inline draft that omits
        # ``authority`` / ``hard_constraints.file_effects``
        # inherits the Goal's pinned ``execution_config`` so the
        # dispatcher can find an eligible executor.  The inline
        # draft's own values (if any) take precedence — explicit
        # always wins over the Goal-level default.
        plan_dict = goal.get("plan") if isinstance(goal, dict) else None
        goal_execution_config = (
            plan_dict.get("execution_config") if isinstance(plan_dict, dict) else None
        )
        if isinstance(goal_execution_config, dict):
            goal_workspace = goal_execution_config.get("workspace_root")
            if isinstance(goal_workspace, str) and goal_workspace.strip():
                hard_constraints = dict(draft.get("hard_constraints") or {})
                file_effects = dict(hard_constraints.get("file_effects") or {})
                if not file_effects.get("workspace_root"):
                    file_effects["workspace_root"] = goal_workspace
                if not file_effects.get("mode"):
                    file_effects["mode"] = "workspace-write"
                hard_constraints["file_effects"] = file_effects
                draft["hard_constraints"] = hard_constraints
            goal_grant = goal_execution_config.get("executor_grant")
            if isinstance(goal_grant, list) and goal_grant:
                existing_authority = draft.get("authority") or {}
                existing_executors = existing_authority.get("executors") or []
                if not existing_executors:
                    clean: list[dict[str, Any]] = []
                    for item in goal_grant:
                        if not isinstance(item, dict):
                            continue
                        executor_id = str(item.get("executor_id") or "").strip()
                        if not executor_id:
                            continue
                        models_raw = item.get("models") or ["*"]
                        roles_raw = item.get("roles") or ["executor"]
                        clean.append(
                            {
                                "executor_id": executor_id,
                                "models": [str(m) for m in models_raw],
                                "roles": [str(r) for r in roles_raw],
                            }
                        )
                    if clean:
                        draft["authority"] = {
                            "executor_policy": "explicit_allow",
                            "executors": clean,
                        }
    elif isinstance(next_stage.get("spec"), dict):
        draft = synthesize_stage_draft(
            goal,
            next_stage,
            previous_evidence=previous_evidence,
            now=now,
            conn=conn,
        )
    else:
        return

    from longtask.rpc.handlers.goal import handle_goal_prepare
    from longtask.rpc.methods import Method
    from longtask.rpc.server import RequestEnvelope

    new_cid = f"lt-{now.strftime('%Y%m%d')}-{next_stage_id}-{uuid.uuid4().hex[:6]}"
    envelope = RequestEnvelope(
        method=Method.GOAL_PREPARE,
        request_id=f"req-auto-{next_stage_id}-{new_cid}",
        client_id="daemon",
        protocol_version=2,
        params={
            "contract_id": new_cid,
            "goal_id": contract.goal_id,
            "stage_id": str(next_stage_id),
            "draft": draft,
        },
    )
    try:
        handle_goal_prepare(envelope, conn=conn, now=now)
    except Exception:
        return

    try:
        new_contract = get_contract(conn, new_cid)
        if new_contract is not None:
            auto_approve_drafted_contract(conn, new_contract, now)
    except Exception:
        return


__all__ = [
    "auto_create_next_stage_contract",
]
