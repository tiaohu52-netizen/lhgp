"""Single runtime validator for LHGP contract drafts."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from lhgp.contracts.acceptance import VALID_VERIFIER_KINDS
from lhgp.contracts.contract_draft import ContractDraft, from_dict

__all__ = ["validate_draft", "validate_raw"]


def validate_raw(data: object) -> list[str]:
    """Validate raw input before constructing a :class:`ContractDraft`."""

    if not isinstance(data, dict):
        return ["draft must be a dict"]
    errors: list[str] = []
    title = data.get("title")
    if not isinstance(title, str) or not 0 < len(title) <= 200:
        errors.append(f"title must be 1..200 chars, got {type(title).__name__}")
    objective = data.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        errors.append("objective must be a non-empty string")
    raw_deadline = data.get("deadline_at")
    if not _looks_like_datetime_with_tz(raw_deadline):
        errors.append(
            f"deadline_at must be ISO-8601 with timezone, got {type(raw_deadline).__name__}"
        )
    if not isinstance(data.get("hard_constraints"), dict):
        errors.append("hard_constraints must be a dict")
    acc = data.get("acceptance")
    if not isinstance(acc, dict):
        errors.append("acceptance must be a dict")
    else:
        if not isinstance(acc.get("standard"), str) or not acc["standard"].strip():
            errors.append("acceptance.standard must be a non-empty string")
        if not isinstance(acc.get("checks"), (list, tuple)) or not acc["checks"]:
            errors.append("acceptance.checks must be a non-empty list")
        if acc.get("verifier") not in VALID_VERIFIER_KINDS:
            errors.append(
                f"acceptance.verifier must be one of {sorted(VALID_VERIFIER_KINDS)}, "
                f"got {acc.get('verifier')!r}"
            )
    workload = data.get("workload_estimate") or {}
    init_hours = workload.get("initial_hours") if isinstance(workload, dict) else None
    if init_hours is None:
        init_hours = data.get("workload_initial_hours")
    if (
        isinstance(init_hours, bool)
        or not isinstance(init_hours, (int, float))
        or not math.isfinite(float(init_hours))
        or init_hours <= 0
    ):
        errors.append("workload_estimate.initial_hours must be a positive number")
    budget = data.get("budget")
    if not isinstance(budget, dict):
        errors.append("budget must be a dict")
    else:
        for name in (
            "max_dispatches",
            "max_escalations",
            "max_concurrent_attempts",
            "max_attempt_minutes",
            "max_output_bytes",
        ):
            value = budget.get(name)
            if not isinstance(value, int) or value <= 0:
                errors.append(f"budget.{name} must be a positive int, got {value!r}")
        max_cost = budget.get("max_cost")
        if max_cost is not None and (
            isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)) or max_cost <= 0
        ):
            errors.append(f"budget.max_cost must be a positive number, got {max_cost!r}")
    return errors


def validate_draft(draft: ContractDraft | dict[str, Any]) -> list[str]:
    """Validate either raw input or an already constructed draft."""

    if isinstance(draft, dict):
        errors = validate_raw(draft)
        if errors:
            return errors
        try:
            draft = from_dict(draft)
        except (KeyError, TypeError, ValueError) as exc:
            return [f"draft.to_dataclass failed: {exc}"]
    return draft.validate()


def _looks_like_datetime_with_tz(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None
