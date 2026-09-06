"""P6 portfolio view types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ContractSummary:
    """One row per contract in the portfolio summary."""

    contract_id: str
    title: str
    state: str
    deadline_status: str
    acceptance_status: str
    deadline_at: str | None
    next_decision_at: str | None
    revision: int
    goal_id: str | None = None
    last_user_rating: int | None = None
    last_user_verdict: str | None = None


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Aggregated view over all known contracts.

    ``by_state`` / ``by_deadline`` / ``by_acceptance`` are counter dicts
    to drive the CLI dashboard without N+1 queries.
    """

    contracts: tuple[ContractSummary, ...]
    by_state: dict[str, int] = field(default_factory=dict)
    by_deadline: dict[str, int] = field(default_factory=dict)
    by_acceptance: dict[str, int] = field(default_factory=dict)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "count": len(self.contracts),
            "by_state": dict(self.by_state),
            "by_deadline": dict(self.by_deadline),
            "by_acceptance": dict(self.by_acceptance),
            "contracts": [c.__dict__ for c in self.contracts],
        }
