"""P6: discrete deadline-escalation levels.

The user chose "harder_warn" — escalate before breach, freeze new
attempts at breach. We express that as four levels keyed off the
ratio ``elapsed / total`` (i.e. ``1 - remaining / total``):

  - NORMAL:    less than 70% of the window elapsed
  - WARNING:   70% <= elapsed < 95%   (i.e. remaining <= 30% of window)
  - URGENT:    elapsed >= 95%         (i.e. remaining <= 5% of window)
  - BREACHED:  now > deadline_at      (frozen for new attempts)

Note the direction: thresholds are stated on the **remaining** fraction in
``WARNING_THRESHOLD`` / ``URGENT_THRESHOLD`` (0.30 / 0.05) and the code
compares ``elapsed_ratio >= 1 - threshold``. An earlier version of this
docstring wrote "WARNING: 30% <= remaining < 50%", which is the inverse of
what the code does — reading it would make you expect a 1h contract with
10 minutes left to be URGENT, while the code (correctly, per policy)
returns WARNING.

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


def contract_window_seconds(deadline_at: datetime, created_at: datetime) -> float | None:
    """The contract's full window in seconds (``deadline_at - created_at``).

    Single source of truth for the ``total_seconds`` argument. Callers that
    omit it make ``compute_deadline_level`` fall back to ``remaining * 2``,
    which pins ``elapsed_ratio`` at 0.5 for every contract — so a contract
    is never reported above NORMAL. The daemon passed the real window while
    the CLI and MCP deadline reports did not, giving the same function two
    different answers for the same contract.

    Returns ``None`` when the window is non-positive (malformed data), which
    makes the caller fall back to the documented inference.
    """
    try:
        window = (deadline_at - created_at).total_seconds()
    except TypeError:
        # naive vs aware datetime mix — treat as unavailable rather than raise
        return None
    return window if window > 0 else None


def compute_deadline_level(
    deadline_at: datetime,
    *,
    now: datetime | None = None,
    total_seconds: float | None = None,
) -> LevelDecision:
    """Pick the appropriate :class:`DeadlineLevel` for a deadline.

    ``total_seconds`` is the time window from the contract's start to
    ``deadline_at``; callers should pass it via
    :func:`contract_window_seconds`. When it is omitted (or non-positive)
    we fall back to ``max(remaining * 2, MIN_ESCALATION_WINDOW)``, which
    treats the contract as if it were exactly half over — a coarse default
    that can only ever report NORMAL, never a risk level.
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
