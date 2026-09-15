"""Deterministic Goal stage progression (single-machine LHGP scope)."""

from __future__ import annotations

from typing import Any


def normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize an ordered ``stages`` plan."""
    stages = plan.get("stages")
    if stages is None:
        return dict(plan)
    if not isinstance(stages, list) or not stages:
        raise ValueError("plan.stages must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(stages):
        if isinstance(raw, str):
            stage = {"id": raw, "title": raw}
        elif isinstance(raw, dict):
            stage = dict(raw)
        else:
            raise ValueError(f"plan.stages[{index}] must be a string or object")
        stage_id = str(stage.get("id", "")).strip()
        if not stage_id:
            raise ValueError(f"plan.stages[{index}].id is required")
        if stage_id in seen:
            raise ValueError(f"duplicate stage id: {stage_id}")
        seen.add(stage_id)
        stage["id"] = stage_id
        stage.setdefault("title", stage_id)
        normalized.append(stage)
    result = dict(plan)
    result["stages"] = normalized
    return result


def advance_progress(
    plan: dict[str, Any],
    progress: dict[str, Any],
    *,
    complete_stage: str,
) -> dict[str, Any]:
    """Complete the current stage and select the next stage deterministically."""
    normalized = normalize_plan(plan)
    stages = normalized.get("stages")
    if not stages:
        raise ValueError("a staged plan is required before advancing progress")
    stage_ids = [str(stage["id"]) for stage in stages]
    completed = [str(item) for item in progress.get("completed", [])]
    if complete_stage not in stage_ids:
        raise ValueError(f"unknown stage: {complete_stage}")
    current = progress.get("current")
    if current is None:
        current = next(
            (stage_id for stage_id in stage_ids if stage_id not in completed), stage_ids[0]
        )
    if str(current) != complete_stage:
        raise ValueError(f"stage {complete_stage} is not current; expected {current}")
    if complete_stage not in completed:
        completed.append(complete_stage)
    next_stage = next((stage_id for stage_id in stage_ids if stage_id not in completed), None)
    result = dict(progress)
    result["completed"] = completed
    result["current"] = next_stage
    result["status"] = "satisfied" if next_stage is None else "active"
    return result


__all__ = ["advance_progress", "normalize_plan"]
