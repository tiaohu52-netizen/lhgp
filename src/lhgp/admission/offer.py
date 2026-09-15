"""Admission offers (SPEC §10.4), owned by the canonical namespace."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ExecutorCandidateView:
    """One eligible or rejected executor candidate in an offer."""

    executor_id: str
    models: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "models": list(self.models),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Offer:
    """Admission offer returned by ``goal/prepare``."""

    eligible_executors: tuple[ExecutorCandidateView, ...] = field(default_factory=tuple)
    rejected_executors: tuple[ExecutorCandidateView, ...] = field(default_factory=tuple)
    acceptance_executable: bool = False
    forecast_p50_minutes: float | None = None
    forecast_p90_minutes: float | None = None
    forecast_confidence: float | None = None
    verification_reserve_sufficient: bool = False
    safe_start_by: datetime | None = None
    uncontrolled_risks: tuple[str, ...] = field(default_factory=tuple)
    declared_guarantees: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible_executors": [c.to_dict() for c in self.eligible_executors],
            "rejected_executors": [c.to_dict() for c in self.rejected_executors],
            "acceptance_executable": self.acceptance_executable,
            "forecast_p50_minutes": self.forecast_p50_minutes,
            "forecast_p90_minutes": self.forecast_p90_minutes,
            "forecast_confidence": self.forecast_confidence,
            "verification_reserve_sufficient": self.verification_reserve_sufficient,
            "safe_start_by": self.safe_start_by.isoformat() if self.safe_start_by else None,
            "uncontrolled_risks": list(self.uncontrolled_risks),
            "declared_guarantees": list(self.declared_guarantees),
        }

    @property
    def eligible(self) -> bool:
        """Whether the offer proves all locally checkable admission conditions."""
        deadline_feasible = self.forecast_p90_minutes is None or self.safe_start_by is not None
        return (
            bool(self.eligible_executors)
            and self.acceptance_executable
            and self.verification_reserve_sufficient
            and deadline_feasible
        )


__all__ = ["ExecutorCandidateView", "Offer"]
