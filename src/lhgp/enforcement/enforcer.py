"""P6: apply level effects to a contract.

The enforcer is intentionally a *pure* decision: given a contract view
and a :class:`LevelDecision`, it returns a list of
:class:`EnforcementAction` items the daemon tick should apply (status
change, lock, re-publish, push notification). It does not perform
the side effects — that keeps the rule logic testable and the tick
loop the single I/O boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lhgp.contracts.contract_view import DeadlineStatus
from lhgp.contracts.contract_view_entity import ContractView
from lhgp.enforcement.levels import DeadlineLevel, LevelDecision
from lhgp.persistence.events import EventType


@dataclass(frozen=True, slots=True)
class EnforcementAction:
    """One side-effect the daemon should apply for a contract at this level."""

    contract_id: str
    level: DeadlineLevel
    actions: tuple[str, ...]
    new_deadline_status: DeadlineStatus | None = None
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


_LEVEL_ACTIONS: dict[DeadlineLevel, dict[str, Any]] = {
    DeadlineLevel.NORMAL: {"actions": (), "deadline_status": None},
    DeadlineLevel.WARNING: {
        "actions": ("republish", "notify_user"),
        "deadline_status": DeadlineStatus.AT_RISK,
        "event_type": EventType.DEADLINE_LEVEL_ESCALATED,
    },
    DeadlineLevel.URGENT: {
        "actions": ("republish", "notify_user", "request_verification"),
        "deadline_status": DeadlineStatus.AT_RISK,
        "event_type": EventType.DEADLINE_LEVEL_ESCALATED,
    },
    DeadlineLevel.BREACHED: {
        "actions": ("lock_new_attempts", "notify_user", "request_verification"),
        "deadline_status": DeadlineStatus.MISSED,
        "event_type": EventType.DEADLINE_BREACH_LOCKED,
    },
}


class DeadlineEnforcer:
    """Stateless: every call derives the level then returns actions."""

    def enforce(
        self,
        contract: ContractView,
        decision: LevelDecision,
        *,
        now: datetime | None = None,
    ) -> EnforcementAction:
        spec = _LEVEL_ACTIONS.get(decision.level, _LEVEL_ACTIONS[DeadlineLevel.NORMAL])
        return EnforcementAction(
            contract_id=contract.contract_id,
            level=decision.level,
            actions=spec["actions"],
            new_deadline_status=spec.get("deadline_status"),
            rationale=decision.rationale,
            metadata={
                "remaining_seconds": decision.remaining_seconds,
                "elapsed_ratio": decision.elapsed_ratio,
                "event_type": spec.get("event_type", "").value
                if hasattr(spec.get("event_type", ""), "value")
                else spec.get("event_type", ""),
            },
        )

    def enforce_all(
        self,
        contracts: list[tuple[ContractView, LevelDecision]],
    ) -> list[EnforcementAction]:
        out: list[EnforcementAction] = []
        for contract, decision in contracts:
            out.append(self.enforce(contract, decision))
        return out
