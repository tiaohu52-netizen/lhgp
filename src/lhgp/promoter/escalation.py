"""Pure escalation ladder decision logic."""

from __future__ import annotations

from dataclasses import dataclass

from lhgp.promoter.urgency import UrgencyTier


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    tier: UrgencyTier | None
    reason: str
    consumes_dispatch: bool
    consumes_escalation: bool


def _free(tier: UrgencyTier, reason: str) -> EscalationDecision:
    return EscalationDecision(tier, reason, False, False)


def decide(
    tier: UrgencyTier | None,
    *,
    lease_alive: bool,
    budget_dispatches_left: int,
    budget_escalations_left: int,
    estimate_stalled: bool,
    partitions_allowed: bool = True,
    budget_cost_left: float | None = None,
) -> EscalationDecision:
    if tier is None:
        return EscalationDecision(
            None, "past deadline: contract goes to arbitration, not the ladder (§6.2)", False, False
        )
    if lease_alive:
        capped = min(tier, UrgencyTier.REMIND)
        return _free(
            capped, f"lease alive: cap at remind (§7), u-tier {int(tier)} -> {int(capped)}"
        )
    if budget_cost_left is not None and budget_cost_left <= 0:
        # 成本预算（§6.3）：唯一按钱画的线。放在 STEER 换挡之前——
        # 「无租约的 STEER 会转 RESPAWN 拉新会话」，那同样要花钱；
        # 成本耗尽后任何形式的派工都必须停，只能交人。
        return EscalationDecision(
            UrgencyTier.HAND_TO_USER,
            f"cost budget exhausted ({budget_cost_left:.2f} left): hand to user (§6.3)",
            False,
            False,
        )
    if tier is UrgencyTier.STEER:
        # DESIGN §6.2 档 3：u >= 1.0，或「无活跃租约且 u >= 0.5」→ 重派。
        # 没有活会话可干预时，steer 是空动作，deadline 剩余窗口不能浪费在
        # 等待上——立即重派新 attempt（灵活性），预算不足则如实交用户。
        if budget_dispatches_left < 1:
            return EscalationDecision(
                UrgencyTier.HAND_TO_USER,
                "steer with no live lease but dispatch budget exhausted: hand to user (§6.2/§6.3)",
                False,
                False,
            )
        return EscalationDecision(
            UrgencyTier.RESPAWN,
            "steer tier with no live lease: nothing to steer, respawn now (§6.2)",
            True,
            False,
        )
    if tier in (UrgencyTier.QUEUED, UrgencyTier.REMIND):
        return _free(tier, f"u-tier {int(tier)}: free action, no budget consumed")
    if budget_dispatches_left < 1:
        return EscalationDecision(
            UrgencyTier.HAND_TO_USER, "dispatch budget exhausted: hand to user (§6.3)", False, False
        )
    if estimate_stalled and partitions_allowed and budget_escalations_left >= 1:
        return EscalationDecision(
            UrgencyTier.PARALLEL,
            "estimate stalled after tier 3 and partitionable: parallel dispatch (§6.2/§7.1)",
            True,
            True,
        )
    if estimate_stalled:
        return EscalationDecision(
            UrgencyTier.RESPAWN,
            "estimate stalled but tier 4 unavailable "
            f"(partitions_allowed={partitions_allowed}, "
            f"escalations_left={budget_escalations_left}): serial respawn (§7.1)",
            True,
            False,
        )
    return EscalationDecision(
        UrgencyTier.RESPAWN,
        "u >= respawn threshold and no live lease: dispatch new attempt (§6.2)",
        True,
        False,
    )


__all__ = ["EscalationDecision", "decide"]
