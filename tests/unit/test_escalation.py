"""升级阶梯决策（DESIGN §6.2 阈值表、§6.3 硬边界、§7 租约、§7.1 分区）。

对应 claim: escalation-ladder-decision（quality/claims.json）。
"""

from __future__ import annotations

import pytest

from longtask.promoter.escalation import decide
from longtask.promoter.urgency import UrgencyTier

pytestmark = pytest.mark.unit

# 基准盘上事实：无租约、预算充足、未停滞
BASE = {
    "lease_alive": False,
    "budget_dispatches_left": 3,
    "budget_escalations_left": 2,
    "estimate_stalled": False,
}


def decide_with(tier: UrgencyTier | None, **overrides: object) -> object:
    kwargs = {**BASE, **overrides}
    return decide(tier, **kwargs)  # type: ignore[arg-type]


class TestDeadlineArbitration:
    def test_none_tier_takes_no_ladder_action(self) -> None:
        # 越 Deadline 走仲裁，不走阶梯（DESIGN §6.2 表下注）
        d = decide_with(None)
        assert d.tier is None
        assert not d.consumes_dispatch
        assert not d.consumes_escalation
        assert "arbitration" in d.reason

    def test_none_tier_ignores_everything_else(self) -> None:
        # 即使预算触顶/租约活着，仲裁路径优先于一切阶梯判定
        d = decide_with(None, lease_alive=True, budget_dispatches_left=0)
        assert d.tier is None


class TestLeaseCap:
    @pytest.mark.parametrize(
        "tier",
        [UrgencyTier.RESPAWN, UrgencyTier.PARALLEL, UrgencyTier.HAND_TO_USER],
    )
    def test_live_lease_caps_at_remind(self, tier: UrgencyTier) -> None:
        # 租约活着只能提醒，不能接管不能加派（DESIGN §7）
        d = decide_with(tier, lease_alive=True)
        assert d.tier == UrgencyTier.REMIND
        assert not d.consumes_dispatch
        assert not d.consumes_escalation

    def test_live_lease_caps_even_when_budget_exhausted(self) -> None:
        # 预算耗尽不构成打断健康执行者的理由；提醒免费照发（§7 + §6.3）
        d = decide_with(UrgencyTier.RESPAWN, lease_alive=True, budget_dispatches_left=0)
        assert d.tier == UrgencyTier.REMIND

    def test_queued_tier_with_live_lease_stays_queued(self) -> None:
        # 档 0 本就不打扰任何人，租约活着不改变它
        d = decide_with(UrgencyTier.QUEUED, lease_alive=True)
        assert d.tier == UrgencyTier.QUEUED


class TestFreeTiers:
    @pytest.mark.parametrize(
        ("tier", "expected"),
        [
            (UrgencyTier.QUEUED, UrgencyTier.QUEUED),
            (UrgencyTier.REMIND, UrgencyTier.REMIND),
        ],
    )
    def test_free_tiers_consume_nothing(self, tier: UrgencyTier, expected: UrgencyTier) -> None:
        d = decide_with(tier)
        assert d.tier == expected
        assert not d.consumes_dispatch
        assert not d.consumes_escalation

    def test_free_tiers_survive_exhausted_budget(self) -> None:
        # 档 0/1 免费动作：预算耗尽不阻止它们，也不升档 5
        d = decide_with(UrgencyTier.REMIND, budget_dispatches_left=0, budget_escalations_left=0)
        assert d.tier == UrgencyTier.REMIND
        assert not d.consumes_dispatch


class TestSteerNoLease:
    def test_steer_with_dead_lease_respawns(self) -> None:
        # DESIGN §6.2 档 3：无活跃租约且 u >= 0.5 → 重派，不浪费 deadline 窗口
        d = decide_with(UrgencyTier.STEER, lease_alive=False)
        assert d.tier == UrgencyTier.RESPAWN
        assert d.consumes_dispatch

    def test_steer_with_dead_lease_and_no_budget_hands_to_user(self) -> None:
        d = decide_with(UrgencyTier.STEER, lease_alive=False, budget_dispatches_left=0)
        assert d.tier == UrgencyTier.HAND_TO_USER
        assert not d.consumes_dispatch


class TestRespawn:
    def test_respawn_consumes_dispatch(self) -> None:
        # 档 3：另起会话，消耗 1 次 max_dispatches（DESIGN §6.2）
        d = decide_with(UrgencyTier.RESPAWN)
        assert d.tier == UrgencyTier.RESPAWN
        assert d.consumes_dispatch
        assert not d.consumes_escalation

    def test_respawn_with_last_dispatch(self) -> None:
        # 预算恰好剩 1：仍可档 3
        d = decide_with(UrgencyTier.RESPAWN, budget_dispatches_left=1)
        assert d.tier == UrgencyTier.RESPAWN
        assert d.consumes_dispatch

    def test_dispatch_budget_exhausted_hands_to_user(self) -> None:
        # 预算硬边界：无钱可花 → 档 5 交还用户，不无限加码（DESIGN §6.3）
        d = decide_with(UrgencyTier.RESPAWN, budget_dispatches_left=0)
        assert d.tier == UrgencyTier.HAND_TO_USER
        assert not d.consumes_dispatch
        assert not d.consumes_escalation
        assert "budget" in d.reason

    def test_escalations_exhausted_does_not_block_respawn(self) -> None:
        # 档 3 不消耗 escalations：escalations=0 不影响另起会话
        d = decide_with(UrgencyTier.RESPAWN, budget_escalations_left=0)
        assert d.tier == UrgencyTier.RESPAWN


class TestStalledRespawnIsHonest:
    """档 4 未实现：估算停滞一律如实记为串行重派，绝不产出 PARALLEL 档。

    旧实现产出 UrgencyTier.PARALLEL 并写下 "parallel dispatch" 的决策理由，
    实际执行与档 3 完全相同——调度器在决策历史里说谎。本类钉住修正后的
    契约：档位、理由、预算三项都必须如实。
    """

    def test_stalled_never_claims_parallel(self) -> None:
        d = decide_with(UrgencyTier.RESPAWN, estimate_stalled=True, partitions_allowed=True)
        assert d.tier == UrgencyTier.RESPAWN
        assert d.consumes_dispatch
        # 没做额外的事，就不收额外的预算
        assert not d.consumes_escalation

    def test_reason_discloses_that_parallel_is_unimplemented(self) -> None:
        d = decide_with(UrgencyTier.RESPAWN, estimate_stalled=True, partitions_allowed=True)
        assert "serial respawn" in d.reason
        assert "not implemented" in d.reason
        # 被请求的并行意图仍可在审计里看到（不隐藏）
        assert "partitions_requested=True" in d.reason

    def test_last_of_both_budgets_still_serial(self) -> None:
        d = decide_with(
            UrgencyTier.RESPAWN,
            estimate_stalled=True,
            budget_dispatches_left=1,
            budget_escalations_left=1,
        )
        assert d.tier == UrgencyTier.RESPAWN
        assert d.consumes_dispatch

    def test_escalations_exhausted_changes_nothing(self) -> None:
        # 档 4 已不消耗 escalations：该项为 0 与为满值的结果必须一致
        zero = decide_with(UrgencyTier.RESPAWN, estimate_stalled=True, budget_escalations_left=0)
        rich = decide_with(UrgencyTier.RESPAWN, estimate_stalled=True, budget_escalations_left=9)
        assert zero.tier == rich.tier == UrgencyTier.RESPAWN
        assert not zero.consumes_escalation and not rich.consumes_escalation

    def test_unpartitionable_also_serial(self) -> None:
        d = decide_with(UrgencyTier.RESPAWN, estimate_stalled=True, partitions_allowed=False)
        assert d.tier == UrgencyTier.RESPAWN
        assert d.consumes_dispatch
        assert not d.consumes_escalation
        assert "serial" in d.reason

    def test_stalled_with_no_dispatch_budget_hands_to_user(self) -> None:
        # 停滞 + 无 dispatch 预算：连串行换人都做不了 → 档 5
        d = decide_with(UrgencyTier.RESPAWN, estimate_stalled=True, budget_dispatches_left=0)
        assert d.tier == UrgencyTier.HAND_TO_USER

    def test_not_stalled_stays_respawn(self) -> None:
        # 未停滞：不升档 4（停滞判定只信交接估算，§6.2）
        d = decide_with(UrgencyTier.RESPAWN, estimate_stalled=False)
        assert d.tier == UrgencyTier.RESPAWN
        assert not d.consumes_escalation


class TestParallelTierIsUnreachable:
    """穷举决策输入空间：没有任何组合能产出 PARALLEL 档。

    比逐个手挑用例更硬——它同时锁住「未来有人无意中恢复那条分支」的情形。
    档 4 的实现（分区租约）尚未接线，见 ADR-005；在它真正落地前，
    决策历史里不得出现一个名义上是并行、实际是串行的档位。
    """

    def test_no_input_combination_yields_parallel(self) -> None:
        combos = 0
        for tier in list(UrgencyTier):
            for lease_alive in (False, True):
                for stalled in (False, True):
                    for partitions in (False, True):
                        for dispatches_left in (0, 1, 3):
                            for escalations_left in (0, 1, 3):
                                decision = decide(
                                    tier,
                                    lease_alive=lease_alive,
                                    budget_dispatches_left=dispatches_left,
                                    budget_escalations_left=escalations_left,
                                    estimate_stalled=stalled,
                                    partitions_allowed=partitions,
                                )
                                combos += 1
                                assert decision.tier is not UrgencyTier.PARALLEL, (
                                    f"输入组合产出了未实现的并行档: tier={tier} "
                                    f"lease_alive={lease_alive} stalled={stalled} "
                                    f"partitions={partitions} dispatches={dispatches_left} "
                                    f"escalations={escalations_left}"
                                )
        assert combos > 100, f"输入空间只覆盖了 {combos} 组，断言强度不足"

    def test_honest_reason_when_stall_is_observed(self) -> None:
        """停滞且可加派时的理由必须自陈「并行未实现」，不假装并行。"""
        decision = decide(
            UrgencyTier.RESPAWN,
            lease_alive=False,
            budget_dispatches_left=3,
            budget_escalations_left=3,
            estimate_stalled=True,
            partitions_allowed=True,
        )
        assert decision.tier == UrgencyTier.RESPAWN
        assert "not implemented" in decision.reason
