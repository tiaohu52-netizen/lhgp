"""P6: discrete deadline-escalation levels.

The user chose "harder_warn" — escalate before breach, freeze new
attempts at breach. We express that as four levels keyed off the
ratio ``remaining / total``:

  - NORMAL:    >= 50% time remaining
  - WARNING:   30% <= time remaining < 50%   (T-30 min on a 1h deadline)
  - URGENT:     5% <= time remaining < 30%   (T-15 / T-10 / T-5)
  - BREACHED:   now > deadline_at            (frozen for new attempts)

The thresholds are conservative defaults; per-contract overrides are
read from ``contracts.attention`` (QuietHours) when present.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class DeadlineLevel(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    URGENT = "urgent"
    BREACHED = "breached"


_LEVEL_ORDER = {
    DeadlineLevel.NORMAL: 0,
    DeadlineLevel.WARNING: 1,
    DeadlineLevel.URGENT: 2,
    DeadlineLevel.BREACHED: 3,
}

# Warning window starts when 30% of the total deadline has elapsed.
# Urgent window starts when 5% remains (≈ T-5 on a 1.5h deadline).
WARNING_THRESHOLD = 0.30
URGENT_THRESHOLD = 0.05
# Minimum wall-clock time before any escalation kicks in, to avoid
# spamming notices on minute-scale contracts.
MIN_ESCALATION_WINDOW = 60.0  # seconds


@dataclass(frozen=True, slots=True)
class LevelDecision:
    level: DeadlineLevel
    remaining_seconds: float
    elapsed_ratio: float  # 0..1 of total consumed
    rationale: str

    @property
    def ordinal(self) -> int:
        return _LEVEL_ORDER[self.level]


def compute_deadline_level(
    deadline_at: datetime,
    *,
    now: datetime | None = None,
    total_seconds: float | None = None,
) -> LevelDecision:
    """Pick the appropriate :class:`DeadlineLevel` for a deadline.

    ``total_seconds`` is the time window from the contract's start to
    ``deadline_at``. If not provided we infer it as the difference
    between ``deadline_at`` and the most recent of (now - 1h, contract
    create) — but in practice callers should pass it explicitly from
    the contract view.
    """
    now = now or datetime.now(UTC)
    remaining = (deadline_at - now).total_seconds()
    if remaining <= 0:
        elapsed_ratio = 1.0
        return LevelDecision(
            level=DeadlineLevel.BREACHED,
            remaining_seconds=remaining,
            elapsed_ratio=elapsed_ratio,
            rationale=f"now > deadline_at (delta={-remaining:.0f}s)",
        )
    if total_seconds is None or total_seconds <= 0:
        total_seconds = max(remaining * 2.0, MIN_ESCALATION_WINDOW)
    elapsed_ratio = max(0.0, min(1.0, 1.0 - (remaining / total_seconds)))
    if remaining < MIN_ESCALATION_WINDOW and elapsed_ratio < URGENT_THRESHOLD:
        return LevelDecision(
            level=DeadlineLevel.URGENT,
            remaining_seconds=remaining,
            elapsed_ratio=elapsed_ratio,
            rationale=(
                f"small absolute window ({remaining:.0f}s) below {MIN_ESCALATION_WINDOW:.0f}s floor"
            ),
        )
    if elapsed_ratio >= 1.0 - URGENT_THRESHOLD:
        return LevelDecision(
            level=DeadlineLevel.URGENT,
            remaining_seconds=remaining,
            elapsed_ratio=elapsed_ratio,
            rationale=f"elapsed_ratio={elapsed_ratio:.2f} >= {1 - URGENT_THRESHOLD:.2f}",
        )
    if elapsed_ratio >= 1.0 - WARNING_THRESHOLD:
        return LevelDecision(
            level=DeadlineLevel.WARNING,
            remaining_seconds=remaining,
            elapsed_ratio=elapsed_ratio,
            rationale=f"elapsed_ratio={elapsed_ratio:.2f} >= {1 - WARNING_THRESHOLD:.2f}",
        )
    return LevelDecision(
        level=DeadlineLevel.NORMAL,
        remaining_seconds=remaining,
        elapsed_ratio=elapsed_ratio,
        rationale="elapsed_ratio below warning threshold",
    )
