"""Pure urgency formula and escalation tier classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class UrgencyTier(IntEnum):
    """紧迫度档位（DESIGN §6.2）。

    ``PARALLEL``（档 4）**当前不会被 decide() 产出**：它对应的分区租约机制
    尚未接线（见 promoter/escalation.py 的说明与 ADR-005）。枚举值保留是因为
    档位序（``min(tier, REMIND)`` 等比较）与历史数据都依赖连续编号。
    """

    QUEUED = 0
    REMIND = 1
    STEER = 2
    RESPAWN = 3
    PARALLEL = 4
    HAND_TO_USER = 5


@dataclass(frozen=True, slots=True)
class UrgencyThresholds:
    remind: float = 0.25
    steer: float = 0.5
    respawn: float = 1.0
    hand_to_user: float = 1.5

    def validate(self) -> list[str]:
        if not 0 < self.remind < self.steer < self.respawn < self.hand_to_user:
            return [
                "thresholds must satisfy 0 < remind < steer < respawn < hand_to_user, "
                f"got {self.remind}/{self.steer}/{self.respawn}/{self.hand_to_user}"
            ]
        return []


DEFAULT_THRESHOLDS = UrgencyThresholds()


def urgency(remaining_hours: float, hours_left: float) -> float | None:
    if hours_left <= 0:
        return None
    if remaining_hours < 0:
        raise ValueError(f"remaining_hours must be >= 0, got {remaining_hours}")
    return remaining_hours / hours_left


def classify(
    u: float | None, thresholds: UrgencyThresholds = DEFAULT_THRESHOLDS
) -> UrgencyTier | None:
    if u is None:
        return None
    if u < thresholds.remind:
        return UrgencyTier.QUEUED
    if u < thresholds.steer:
        return UrgencyTier.REMIND
    if u < thresholds.respawn:
        return UrgencyTier.STEER
    return UrgencyTier.RESPAWN


__all__ = ["DEFAULT_THRESHOLDS", "UrgencyThresholds", "UrgencyTier", "classify", "urgency"]
