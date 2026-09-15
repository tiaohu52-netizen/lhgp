"""Forecast model (SPEC §10.2).

The canonical ``lhgp`` namespace owns this implementation.  The historical
``longtask.forecast.model`` path remains a compatibility facade.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class Forecast:
    """Six-component forecast with p50/p90 and finish probability."""

    queue_minutes: float | None = None
    startup_minutes: float | None = None
    remaining_minutes: float | None = None
    verification_minutes: float | None = None
    retry_reserve_minutes: float | None = None
    safety_margin_minutes: float | None = None
    forecast_p50_minutes: float | None = None
    forecast_p90_minutes: float | None = None
    p_finish: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_minutes": self.queue_minutes,
            "startup_minutes": self.startup_minutes,
            "remaining_minutes": self.remaining_minutes,
            "verification_minutes": self.verification_minutes,
            "retry_reserve_minutes": self.retry_reserve_minutes,
            "safety_margin_minutes": self.safety_margin_minutes,
            "forecast_p50_minutes": self.forecast_p50_minutes,
            "forecast_p90_minutes": self.forecast_p90_minutes,
            "p_finish": self.p_finish,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Forecast:
        return cls(
            queue_minutes=_opt_float(data.get("queue_minutes")),
            startup_minutes=_opt_float(data.get("startup_minutes")),
            remaining_minutes=_opt_float(data.get("remaining_minutes")),
            verification_minutes=_opt_float(data.get("verification_minutes")),
            retry_reserve_minutes=_opt_float(data.get("retry_reserve_minutes")),
            safety_margin_minutes=_opt_float(data.get("safety_margin_minutes")),
            forecast_p50_minutes=_opt_float(data.get("forecast_p50_minutes")),
            forecast_p90_minutes=_opt_float(data.get("forecast_p90_minutes")),
            p_finish=_opt_float(data.get("p_finish")),
        )


@dataclass(frozen=True, slots=True)
class DeadlineSnapshot:
    """Immutable, explainable Deadline decision snapshot (SPEC §10.6)."""

    computed_at: datetime
    due_at: datetime
    forecast: Forecast
    slack_p50_minutes: float | None
    slack_p90_minutes: float | None
    confidence: str
    forecast_level: str
    risk: str
    reason: str
    next_decision_at: datetime | None = None
    sample_count: int = 0
    p_finish_basis: str = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "computed_at": self.computed_at.isoformat(),
            "due_at": self.due_at.isoformat(),
            **self.forecast.to_dict(),
            "slack_p50_minutes": self.slack_p50_minutes,
            "slack_p90_minutes": self.slack_p90_minutes,
            "confidence": self.confidence,
            "forecast_level": self.forecast_level,
            "risk": self.risk,
            "reason": self.reason,
            "next_decision_at": (
                self.next_decision_at.isoformat() if self.next_decision_at else None
            ),
            "sample_count": self.sample_count,
            "p_finish_basis": self.p_finish_basis,
        }


def build_deadline_snapshot(
    forecast: Forecast,
    *,
    computed_at: datetime,
    due_at: datetime,
    next_decision_at: datetime | None = None,
    sample_count: int = 0,
    stale_after: timedelta = timedelta(hours=1),
    forecast_updated_at: datetime | None = None,
    sample_durations_minutes: Sequence[float] | None = None,
) -> DeadlineSnapshot:
    """Derive conservative Deadline risk without promising completion.

    Missing/low-sample/stale inputs deliberately produce ``low/coarse``
    confidence.  The caller may still use the snapshot for scheduling, but
    must not present it as a precise probability.
    """
    p50 = forecast.forecast_p50_minutes
    p90 = forecast.forecast_p90_minutes
    remaining = (due_at - computed_at).total_seconds() / 60.0
    slack_p50 = remaining - p50 if p50 is not None else None
    slack_p90 = remaining - p90 if p90 is not None else None
    observed = tuple(
        duration
        for duration in (sample_durations_minutes or ())
        if isinstance(duration, (int, float)) and duration >= 0
    )
    fixed_overhead = sum(
        value or 0.0
        for value in (
            forecast.queue_minutes,
            forecast.startup_minutes,
            forecast.verification_minutes,
            forecast.safety_margin_minutes,
        )
    )
    available_for_executor = max(remaining - fixed_overhead, 0.0)
    p_finish = (
        sum(duration <= available_for_executor for duration in observed) / len(observed)
        if observed
        else forecast.p_finish
    )
    p_finish_basis = (
        "empirical-success-cdf"
        if observed
        else ("coarse-heuristic" if forecast.p_finish is not None else "unavailable")
    )
    stale = forecast_updated_at is not None and computed_at - forecast_updated_at > stale_after
    complete_components = all(
        value is not None
        for value in (
            forecast.queue_minutes,
            forecast.startup_minutes,
            forecast.remaining_minutes,
            forecast.verification_minutes,
            forecast.retry_reserve_minutes,
            forecast.safety_margin_minutes,
            p50,
            p90,
            p_finish,
        )
    )
    low_confidence = sample_count < 3 or not complete_components or stale
    confidence = "low" if low_confidence else "high"
    # 历史样本只能说明估计来自真实运行记录；在尚未做回放校准和
    # 置信区间校验前，不得把它标成 calibrated，避免模型误读精度。
    forecast_level = "coarse" if low_confidence else "historical"
    if p_finish is not None:
        p_finish = max(0.0, min(1.0, p_finish))
    if computed_at > due_at:
        risk = "missed"
        reason = "deadline passed before acceptance"
    elif slack_p90 is not None and slack_p90 < 0:
        risk = "red"
        reason = "p90 forecast exceeds remaining time"
    elif p_finish is not None and p_finish < 0.40:
        risk = "red"
        reason = "finish probability below 0.40"
    elif slack_p90 is not None and slack_p90 < 0.25 * max(remaining, 1.0):
        risk = "orange"
        reason = "p90 safety slack is narrow"
    elif p_finish is not None and p_finish < 0.65:
        risk = "yellow"
        reason = "finish probability below 0.65"
    elif p_finish is not None and p_finish >= 0.85 and (slack_p90 or 0) >= 0:
        risk = "green"
        reason = "p90 slack and finish probability are healthy"
    else:
        risk = "unknown"
        reason = "insufficient forecast evidence"
    return DeadlineSnapshot(
        computed_at=computed_at,
        due_at=due_at,
        forecast=Forecast(**{**forecast.to_dict(), "p_finish": p_finish}),
        slack_p50_minutes=slack_p50,
        slack_p90_minutes=slack_p90,
        confidence=confidence,
        forecast_level=forecast_level,
        risk=risk,
        reason=reason,
        next_decision_at=next_decision_at,
        sample_count=len(observed) if observed else max(sample_count, 0),
        p_finish_basis=p_finish_basis,
    )


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


RISK_TIER_THRESHOLDS: tuple[float, ...] = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0)


def risk_tier(u: float | None) -> int | None:
    """Map urgency ratio ``u`` to tier 0..5; None means past deadline."""
    if u is None:
        return None
    for idx, threshold in enumerate(RISK_TIER_THRESHOLDS):
        if u < threshold:
            return idx
    return len(RISK_TIER_THRESHOLDS)


__all__ = [
    "RISK_TIER_THRESHOLDS",
    "DeadlineSnapshot",
    "Forecast",
    "build_deadline_snapshot",
    "risk_tier",
]
