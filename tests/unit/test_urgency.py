"""紧迫度分档（DESIGN §6.1、§6.2 默认阈值表）。

对应 claim: urgency-tier-thresholds（quality/claims.json）。
"""

from __future__ import annotations

import pytest

from longtask.promoter.urgency import (
    DEFAULT_THRESHOLDS,
    UrgencyThresholds,
    UrgencyTier,
    classify,
    urgency,
)

pytestmark = pytest.mark.unit


class TestUrgencyFormula:
    def test_basic_ratio(self) -> None:
        # 剩 3 小时工作 / 剩 12 小时 = 0.25
        assert urgency(3.0, 12.0) == pytest.approx(0.25)

    def test_zero_work_is_zero(self) -> None:
        assert urgency(0.0, 5.0) == 0.0

    def test_deadline_passed_returns_none(self) -> None:
        # 剩余时间 ≤ 0：走 Deadline 仲裁，不走阶梯（DESIGN §6.2）
        assert urgency(1.0, 0.0) is None
        assert urgency(1.0, -3.0) is None

    def test_negative_work_rejected(self) -> None:
        with pytest.raises(ValueError, match="remaining_hours"):
            urgency(-1.0, 5.0)


class TestClassify:
    @pytest.mark.parametrize(
        ("u", "expected"),
        [
            (0.0, UrgencyTier.QUEUED),
            (0.249, UrgencyTier.QUEUED),
            (0.25, UrgencyTier.REMIND),  # 边界：含下界
            (0.499, UrgencyTier.REMIND),
            (0.5, UrgencyTier.STEER),
            (0.999, UrgencyTier.STEER),
            (1.0, UrgencyTier.RESPAWN),
            (7.5, UrgencyTier.RESPAWN),
        ],
    )
    def test_tier_boundaries(self, u: float, expected: UrgencyTier) -> None:
        assert classify(u) == expected

    def test_none_stays_none(self) -> None:
        assert classify(None) is None

    def test_custom_thresholds(self) -> None:
        custom = UrgencyThresholds(remind=0.1, steer=0.2, respawn=0.5, hand_to_user=2.0)
        assert classify(0.15, custom) == UrgencyTier.REMIND
        assert classify(0.6, custom) == UrgencyTier.RESPAWN


class TestThresholds:
    def test_defaults_match_design(self) -> None:
        # DESIGN §6.2 默认阈值表：0.25 / 0.5 / 1.0 / 1.5
        t = DEFAULT_THRESHOLDS
        assert (t.remind, t.steer, t.respawn, t.hand_to_user) == (0.25, 0.5, 1.0, 1.5)
        assert t.validate() == []

    def test_disordered_thresholds_rejected(self) -> None:
        bad = UrgencyThresholds(remind=0.9, steer=0.5, respawn=1.0, hand_to_user=1.5)
        assert bad.validate() != []
