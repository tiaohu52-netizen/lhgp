"""Top-level immutable contract draft (SPEC §4 and §6.1)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lhgp.acceptance.checks import parse_check
from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.attention import Attention
from lhgp.contracts.attention import from_dict as attention_from_dict
from lhgp.contracts.authority import Authority
from lhgp.contracts.authority import from_dict as authority_from_dict
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.auto_approve import from_dict as auto_approve_from_dict
from lhgp.contracts.budget import DEFAULT_VERIFICATION_RESERVED, Budget
from lhgp.contracts.continuity import Continuity
from lhgp.contracts.continuity import from_dict as continuity_from_dict

SCHEMA_VERSION = 2


def _strict_int(value: Any, field: str) -> int:
    """Parse a budget integer without treating booleans as 0/1."""
    if isinstance(value, (bool, float)):
        raise TypeError(f"budget.{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"budget.{field} must be an integer") from exc


def _strict_float(value: Any, field: str) -> float:
    """Parse a finite workload estimate without bool coercion."""
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise TypeError(f"{field} must be a finite number")
    return parsed


@dataclass(frozen=True, slots=True)
class ContractDraft:
    """Contract draft composed from the five protocol field groups."""

    title: str
    objective: str
    deadline_at: datetime
    hard_constraints: dict[str, Any]
    acceptance: Acceptance
    workload_initial_hours: float
    budget: Budget
    authority: Authority = field(default_factory=Authority)
    attention: Attention = field(default_factory=Attention)
    continuity: Continuity = field(default_factory=Continuity)
    auto_approve: AutoApprove = field(default_factory=AutoApprove)
    soft_guidance: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    client_meta: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not 0 < len(self.title) <= 200:
            errors.append("title must be 1..200 chars")
        if not self.objective.strip():
            errors.append("objective must not be empty")
        if self.deadline_at.tzinfo is None:
            errors.append("deadline_at must carry an explicit timezone")
        if self.workload_initial_hours <= 0:
            errors.append("workload_estimate.initial_hours must be positive")
        errors.extend(self.acceptance.validate())
        errors.extend(self.budget.validate())
        errors.extend(self.authority.validate())
        errors.extend(self.attention.validate())
        errors.extend(self.continuity.validate())
        return errors

    def to_dict(self) -> dict[str, Any]:
        from lhgp.contracts.attention import to_dict as attention_to_dict
        from lhgp.contracts.authority import to_dict as authority_to_dict
        from lhgp.contracts.continuity import to_dict as continuity_to_dict

        return {
            "schema_version": SCHEMA_VERSION,
            "title": self.title,
            "objective": self.objective,
            "deadline_at": self.deadline_at.isoformat(),
            "hard_constraints": self.hard_constraints,
            "acceptance": {
                "standard": self.acceptance.standard,
                "checks": [
                    check.to_dict() if hasattr(check, "to_dict") else check
                    for check in self.acceptance.checks
                ],
                "verifier": self.acceptance.verifier,
                "spec": self.acceptance.spec,
                "spec_hash": self.acceptance.spec_hash,
            },
            "workload_estimate": {"initial_hours": self.workload_initial_hours},
            "workload_initial_hours": self.workload_initial_hours,
            "budget": {
                "max_dispatches": self.budget.max_dispatches,
                "max_escalations": self.budget.max_escalations,
                "max_concurrent_attempts": self.budget.max_concurrent_attempts,
                "max_attempt_minutes": self.budget.max_attempt_minutes,
                "max_output_bytes": self.budget.max_output_bytes,
                "verification_attempts_reserved": self.budget.verification_attempts_reserved,
            },
            "authority": authority_to_dict(self.authority),
            "attention": attention_to_dict(self.attention),
            "continuity": continuity_to_dict(self.continuity),
            "auto_approve": self.auto_approve.to_dict(),
            "soft_guidance": self.soft_guidance,
            "context": self.context,
            "execution": self.execution,
            "client_meta": self.client_meta,
        }


def from_dict(data: dict[str, Any]) -> ContractDraft:
    raw_deadline = data["deadline_at"]
    deadline_at = (
        raw_deadline
        if isinstance(raw_deadline, datetime)
        else datetime.fromisoformat(str(raw_deadline))
    )
    acceptance_raw = data["acceptance"]
    acceptance = Acceptance(
        standard=str(acceptance_raw["standard"]),
        checks=tuple(parse_check(c) for c in acceptance_raw.get("checks") or ()),
        verifier=str(acceptance_raw.get("verifier") or "cross_check"),
        spec=acceptance_raw.get("spec"),
        spec_hash=acceptance_raw.get("spec_hash"),
    )
    workload_raw = data.get("workload_estimate") or {}
    if "initial_hours" in workload_raw:
        workload = _strict_float(workload_raw["initial_hours"], "workload_estimate.initial_hours")
    elif "workload_initial_hours" in data:
        workload = _strict_float(data["workload_initial_hours"], "workload_estimate.initial_hours")
    else:
        raise KeyError("workload_estimate.initial_hours")
    budget_raw = data["budget"]
    budget = Budget(
        max_dispatches=_strict_int(budget_raw["max_dispatches"], "max_dispatches"),
        max_escalations=_strict_int(budget_raw["max_escalations"], "max_escalations"),
        max_concurrent_attempts=_strict_int(
            budget_raw["max_concurrent_attempts"], "max_concurrent_attempts"
        ),
        max_attempt_minutes=_strict_int(budget_raw["max_attempt_minutes"], "max_attempt_minutes"),
        max_output_bytes=_strict_int(budget_raw["max_output_bytes"], "max_output_bytes"),
        verification_attempts_reserved=_strict_int(
            budget_raw.get("verification_attempts_reserved", DEFAULT_VERIFICATION_RESERVED),
            "verification_attempts_reserved",
        ),
    )
    return ContractDraft(
        title=str(data["title"]),
        objective=str(data["objective"]),
        deadline_at=deadline_at,
        hard_constraints=dict(data.get("hard_constraints") or {}),
        acceptance=acceptance,
        workload_initial_hours=workload,
        budget=budget,
        authority=authority_from_dict(data.get("authority")),
        attention=attention_from_dict(data.get("attention")),
        continuity=continuity_from_dict(data.get("continuity")),
        auto_approve=auto_approve_from_dict(data.get("auto_approve")),
        soft_guidance=dict(data.get("soft_guidance") or {}),
        context=dict(data.get("context") or {}),
        execution=dict(data.get("execution") or {}),
        client_meta=dict(data.get("client_meta") or {}),
    )


__all__ = ["SCHEMA_VERSION", "ContractDraft", "from_dict"]
