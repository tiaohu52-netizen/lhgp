"""Structured stage spec for staged Goal execution.

Each stage of a Goal plan declares five required contents (外部 review 3rd-round
建议) so the executor and verifier share one explicit contract for what the
stage is, what it covers, how to verify completion, and what resources the
executor is allowed to consume.

Required fields
---------------
- goal:        what this stage delivers
- scope:       functional / interfaces / constraints / out_of_scope
- acceptance:  verifiable conditions (forwarded to acceptance/spec.py)
- dependencies + artifacts: what the stage needs and what it produces
- time_budget: deadline (deadline_at)
- budget:      retries / concurrent attempts / dispatch ceiling
- permissions:  declared modifiable scope (a stage may not change anything
                outside this set without re-approval)

The spec is bound to the contract that implements the stage: any change to the
spec invalidates the contract's plan approval (revision CAS).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StageSpec:
    goal: str
    functional: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    acceptance: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    deadline_at: str | None = None
    max_retries: int = 3
    max_dispatches: int = 10
    max_concurrent_attempts: int = 2
    max_attempt_minutes: int = 30
    modifiable_scope: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "scope": {
                "functional": list(self.functional),
                "interfaces": list(self.interfaces),
                "constraints": list(self.constraints),
                "out_of_scope": list(self.out_of_scope),
            },
            "acceptance": self.acceptance,
            "dependencies": list(self.dependencies),
            "artifacts": list(self.artifacts),
            "time_budget": {"deadline_at": self.deadline_at},
            "budget": {
                "max_retries": self.max_retries,
                "max_dispatches": self.max_dispatches,
                "max_concurrent_attempts": self.max_concurrent_attempts,
                "max_attempt_minutes": self.max_attempt_minutes,
            },
            "permissions": {"modifiable_scope": list(self.modifiable_scope)},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | Any) -> StageSpec:
        if not isinstance(raw, dict):
            raise ValueError("stage spec must be a dict")
        scope_raw = raw.get("scope")
        scope: dict[str, Any] = scope_raw if isinstance(scope_raw, dict) else {}
        time_budget_raw = raw.get("time_budget")
        time_budget: dict[str, Any] = time_budget_raw if isinstance(time_budget_raw, dict) else {}
        budget_raw = raw.get("budget")
        budget: dict[str, Any] = budget_raw if isinstance(budget_raw, dict) else {}
        permissions_raw = raw.get("permissions")
        permissions: dict[str, Any] = permissions_raw if isinstance(permissions_raw, dict) else {}
        acceptance_raw = raw.get("acceptance")
        acceptance: dict[str, Any] = acceptance_raw if isinstance(acceptance_raw, dict) else {}

        def _tuple(key: str) -> tuple[str, ...]:
            v = scope.get(key)
            if not isinstance(v, list):
                return ()
            return tuple(str(x) for x in v if isinstance(x, str) and x.strip())

        return cls(
            goal=str(raw.get("goal", "")).strip(),
            functional=_tuple("functional"),
            interfaces=_tuple("interfaces"),
            constraints=_tuple("constraints"),
            out_of_scope=_tuple("out_of_scope"),
            acceptance=acceptance,
            dependencies=tuple(
                str(x) for x in (raw.get("dependencies") or []) if isinstance(x, str) and x.strip()
            ),
            artifacts=tuple(
                str(x) for x in (raw.get("artifacts") or []) if isinstance(x, str) and x.strip()
            ),
            deadline_at=str(time_budget.get("deadline_at"))
            if time_budget.get("deadline_at") is not None
            else None,
            max_retries=int(budget.get("max_retries", 3)),
            max_dispatches=int(budget.get("max_dispatches", 10)),
            max_concurrent_attempts=int(budget.get("max_concurrent_attempts", 2)),
            max_attempt_minutes=int(budget.get("max_attempt_minutes", 30)),
            modifiable_scope=tuple(
                str(x)
                for x in (permissions.get("modifiable_scope") or [])
                if isinstance(x, str) and x.strip()
            ),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.goal:
            errors.append("stage spec.goal is required")
        if not isinstance(self.acceptance, dict) or not self.acceptance:
            errors.append("stage spec.acceptance is required and must be a non-empty dict")
        else:
            from lhgp.acceptance.spec import validate_spec

            errors.extend(f"acceptance: {e}" for e in validate_spec(self.acceptance))
        if self.max_retries < 0:
            errors.append("stage spec.budget.max_retries must be >= 0")
        if self.max_dispatches < 1:
            errors.append("stage spec.budget.max_dispatches must be >= 1")
        if self.max_concurrent_attempts < 1:
            errors.append("stage spec.budget.max_concurrent_attempts must be >= 1")
        if self.max_attempt_minutes < 1:
            errors.append("stage spec.budget.max_attempt_minutes must be >= 1")
        return errors

    def spec_hash(self) -> str:
        """Deterministic hash of the spec — bound to contract approval."""
        canonical = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_stage_entry(stage: Any) -> list[str]:
    """Validate a single stage entry from a Goal plan.

    Returns the list of validation errors. An empty list means the
    stage declares a usable spec. Stages without a ``spec`` field fail
    with a single, structured error so the daemon can surface it
    before binding a contract.
    """
    if not isinstance(stage, dict):
        return [f"stage must be a dict, got {type(stage).__name__}"]
    raw_spec = stage.get("spec")
    if not isinstance(raw_spec, dict):
        return [
            "stage.spec is required (StageSpec needs goal / scope / acceptance / "
            "dependencies / time_budget / budget / permissions)"
        ]
    try:
        spec = StageSpec.from_dict(raw_spec)
    except (ValueError, TypeError) as exc:
        return [f"stage.spec invalid: {exc}"]
    return spec.validate()


__all__ = ["StageSpec", "validate_stage_entry"]
