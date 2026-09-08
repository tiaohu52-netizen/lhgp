"""Stage → contract draft synthesis.

Lives in ``longtask.persistence`` (not the daemon ``cli``)
so the contract RPC handler can call it without crossing
the ``rpc → cli is forbidden`` arch rule.  The previous
home in :mod:`longtask.cli.tick` made user-confirm from
the MCP path impossible to thread back into next-stage
contract creation.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from lhgp.contracts.auto_approve import AutoApprove
from longtask.persistence.store import get_contract


def synthesize_stage_draft(
    goal: dict[str, Any],
    stage: dict[str, Any],
    *,
    previous_evidence: dict[str, Any] | None,
    now: datetime,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Build a contract draft from a stage's structured spec.

    Carries every spec field the ``ContractDraft`` can hold
    (title/objective/deadline/acceptance/budget) and forwards
    the rest (scope, dependencies, artifacts) into ``context``.
    When ``conn`` is given, the previous stage's ``auto_approve``
    is propagated via :meth:`AutoApprove.inherit_from` so
    pre-authorisation carries across stages.
    """
    raw_spec: dict[str, Any] = dict(stage["spec"]) if isinstance(stage.get("spec"), dict) else {}
    # 4th-round review (2026-09-08): ``validate_stage_entry``
    # treats ``stage.spec`` as a full StageSpec envelope
    # (``goal``/``scope``/``acceptance``/etc.) and rejects a
    # plain boolean body.  Read the envelope directly when
    # the spec looks like an envelope; fall back to the
    # legacy boolean-body shape for old plans.
    envelope_keys: set[str] = {
        "goal",
        "scope",
        "acceptance",
        "dependencies",
        "artifacts",
        "time_budget",
        "budget",
        "permissions",
    }
    if envelope_keys.intersection(raw_spec.keys()):
        spec_envelope: dict[str, Any] = dict(raw_spec)
        if "goal" not in spec_envelope or not str(spec_envelope["goal"]).strip():
            spec_envelope["goal"] = str(stage.get("title") or stage.get("id") or "stage")
    else:
        spec_envelope = {
            "goal": str(stage.get("title") or stage.get("id") or "stage"),
            "acceptance": raw_spec,
        }
        for opt_key in (
            "scope",
            "dependencies",
            "artifacts",
            "time_budget",
            "budget",
            "permissions",
        ):
            v = stage.get(opt_key)
            if v is not None:
                spec_envelope[opt_key] = v
    from lhgp.goals.stage import StageSpec

    spec = StageSpec.from_dict(spec_envelope)
    title = str(stage.get("title") or spec.goal or str(stage.get("id", "stage")))
    objective = spec.goal or str(goal.get("objective") or title)
    if spec.deadline_at:
        deadline_iso = str(spec.deadline_at)
    else:
        deadline_iso = (now + timedelta(hours=24)).isoformat()
    boolean_body: Any = spec_envelope.get("acceptance", raw_spec)
    acceptance_checks: list[Any] = []
    _collect_machine_checks(boolean_body, acceptance_checks)
    if not acceptance_checks:
        acceptance_checks = [
            {
                "kind": "artifact-present",
                "target": f"stage:{stage.get('id', 'unknown')}",
                "mandatory": True,
            }
        ]
    context: dict[str, Any] = {
        "stage_spec": boolean_body,
        "stage_id": stage.get("id"),
    }
    if previous_evidence:
        context["previous_evidence"] = previous_evidence
    if spec.dependencies:
        context["dependencies"] = list(spec.dependencies)
    if spec.artifacts:
        context["expected_artifacts"] = list(spec.artifacts)
    if spec.modifiable_scope:
        context["modifiable_scope"] = list(spec.modifiable_scope)
    if spec.functional or spec.interfaces or spec.constraints or spec.out_of_scope:
        context["scope"] = {
            "functional": list(spec.functional),
            "interfaces": list(spec.interfaces),
            "constraints": list(spec.constraints),
            "out_of_scope": list(spec.out_of_scope),
        }
    hard_constraints: dict[str, Any] = {}
    if spec.modifiable_scope:
        hard_constraints["modifiable_scope"] = list(spec.modifiable_scope)
    draft: dict[str, Any] = {
        "title": title,
        "objective": objective,
        "deadline_at": deadline_iso,
        "hard_constraints": hard_constraints,
        "acceptance": {
            "standard": objective,
            "checks": acceptance_checks,
            "verifier": "cross_check",
            "spec": boolean_body,
            "spec_hash": spec.spec_hash() or None,
        },
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": max(1, spec.max_dispatches),
            "max_escalations": 2,
            "max_concurrent_attempts": max(1, spec.max_concurrent_attempts),
            "max_attempt_minutes": max(1, spec.max_attempt_minutes),
            "max_output_bytes": 1_048_576,
        },
        "context": context,
    }
    prev_auto_approve = _resolve_previous_auto_approve(goal, stage, conn)
    if prev_auto_approve is not None:
        draft["auto_approve"] = AutoApprove().inherit_from(prev_auto_approve).to_dict()
    return draft


def _resolve_previous_auto_approve(
    goal: dict[str, Any],
    stage: dict[str, Any],
    conn: sqlite3.Connection | None,
) -> AutoApprove | None:
    """Return the previous stage's contract ``auto_approve``,
    or ``None`` for any silent-degrade case (no conn, no plan,
    no prev stage, no contract_id, contract missing).
    """
    if conn is None:
        return None
    plan_raw = goal.get("plan")
    if not isinstance(plan_raw, dict):
        return None
    stages_raw = plan_raw.get("stages")
    if not isinstance(stages_raw, list):
        return None
    target_id = stage.get("id") if isinstance(stage, dict) else None
    if target_id is None:
        return None
    idx = next(
        (i for i, s in enumerate(stages_raw) if isinstance(s, dict) and s.get("id") == target_id),
        None,
    )
    if idx is None or idx <= 0:
        return None
    prev = stages_raw[idx - 1]
    if not isinstance(prev, dict):
        return None
    prev_cid = prev.get("contract_id")
    if not prev_cid:
        return None
    prev_contract = get_contract(conn, str(prev_cid))
    if prev_contract is None:
        return None
    return prev_contract.draft.auto_approve


def _collect_machine_checks(node: Any, out: list[dict[str, Any]]) -> None:
    """Walk a boolean spec and append every leaf machine criterion.

    Handles ``{"all": [...]}``, ``{"any": [...]}``, and bare
    criterion dicts. Stops at non-machine leaves (user/agent judges)
    since the plan gate only requires coverage of typed checks.
    """
    if not isinstance(node, dict):
        return
    for comb in ("all", "any"):
        children = node.get(comb)
        if isinstance(children, list):
            for child in children:
                _collect_machine_checks(child, out)
            return
    judge = node.get("judge")
    if judge != "machine":
        return
    kind = node.get("kind")
    target = node.get("target")
    if isinstance(kind, str) and kind.strip() and isinstance(target, str) and target.strip():
        out.append({"kind": kind, "target": target, "mandatory": True})


__all__ = [
    "synthesize_stage_draft",
]
