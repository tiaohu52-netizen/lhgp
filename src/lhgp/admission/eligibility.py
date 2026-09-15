"""Seven-condition executor admission evaluation (SPEC §6.3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from lhgp.contracts.authority import (
    Authority,
    AuthorityBinding,
    binding_for_executor,
    models_allow,
    roles_allow,
)


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    executor_id: str
    executor_enabled_globally: bool
    executor_concurrency_available: bool
    capability_satisfied: bool
    constraint_enforcement_proven: bool
    budget_available: bool
    verifier_independent: bool


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    candidate_executor_id: str
    requested_model: str
    requested_role: str
    conditions: dict[str, bool] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return all(self.conditions.values())

    @property
    def failed(self) -> list[str]:
        return [name for name, ok in self.conditions.items() if not ok]


def evaluate(
    *, authority: Authority, facts: CandidateFacts, requested_model: str, requested_role: str
) -> EligibilityVerdict:
    conditions: dict[str, bool] = {"globally_enabled": facts.executor_enabled_globally}
    binding = binding_for_executor(authority, facts.executor_id)
    if binding is None and authority.executor_policy == "closed" and not authority.executors:
        explicit_allowed = True
    else:
        explicit_allowed = (
            binding is not None
            and models_allow(authority, binding=binding, model=requested_model)
            and roles_allow(authority, binding=binding, role=requested_role)
        )
    conditions["contract_explicitly_allows"] = explicit_allowed
    conditions["capability_satisfies"] = facts.capability_satisfied
    conditions["constraint_enforcement_proven"] = facts.constraint_enforcement_proven
    conditions["budget_available"] = facts.budget_available
    conditions["concurrency_available"] = facts.executor_concurrency_available
    conditions["verifier_independence_satisfies"] = (
        facts.verifier_independent if requested_role == "verifier" else True
    )
    return EligibilityVerdict(
        candidate_executor_id=facts.executor_id,
        requested_model=requested_model,
        requested_role=requested_role,
        conditions=conditions,
    )


__all__ = ["AuthorityBinding", "CandidateFacts", "EligibilityVerdict", "evaluate"]
