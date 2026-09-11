"""E2 多合同公平性策略：饥饿检测 + 容量记账（纯函数，可测试）。

公平性的反例不是「按紧迫排」本身，而是「低紧迫合同永远排不上」。
这里定义两个可判定的规则：

1. **饥饿（starvation）**：合同持续处于某档位 ≥ N 个 tick 却从未被
   派工——即使低紧迫，也应当在第 N 次后获得一次优先处理。
2. **容量记账（capacity ledger）**：per-tick 已派工的合同数达到
   soft cap（如 max_dispatches_per_tick）时，同 tick 内不再继续
   派工（避免一 tick 内把预算全部烧给排序靠前的合同）。

两者都是确定性规则，不依赖时钟以外的外部状态。

**接线状态（审计 B8，2026-09-11 实测）**：

- 规则 2 已接线：``tick.py`` 的 ``run_daemon_tick`` 持有 ``TickCapacityLedger``，
  每次派工前查 ``can_dispatch``。注意 soft cap 默认 0（不限），所以它只在运营
  显式配置 ``max_dispatches_per_tick`` 后才有约束力。
- 规则 1 **未接线**：``apply_fairness_order`` 需要每合同的跨 tick 状态，而 tick
  传给它的 ``fairness_states`` 恒为空 dict——``ContractFairnessState.observe_tick``
  （唯一推进状态的方法）在生产代码里没有任何调用点，只在测试里被调用。因此
  「连续 N tick 未派工自动提前」这条分支在生产里从不触发（``apply_fairness_order``
  里的 ``starved`` 恒为空列表）。
- DESIGN §8.3「多合同公平性」描述的饥饿保护是**另一套语义**（连续 N 轮因**额度
  不足**未获分发且 u ≥ 1.0 → 升级 ``blocked(need-user)``，让用户决定加执行器还是
  砍任务），本模块没有实现它。

结论：饥饿保护属于**未交付**能力，不是「已实现但没测到」。要接线先得有 E2 的独立
SPEC 与测试（``docs/RELEASE-PLAN.md`` §5 E2 的依赖列就是这么写的）。在那之前
CHANGELOG 不再宣称它已交付。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class FairnessConfig:
    """公平性参数（E2 SPEC 落地值）。"""

    # 同一合同在同一紧迫档位连续等这么多个 tick 仍未派工 → 视为饥饿。
    # 注意：这条阈值目前只在单元测试里起作用（饥饿规则未接线，见模块 docstring），
    # 而 DESIGN §8.3 的饥饿保护写的是「默认 10 轮」——数字与语义都待 E2 SPEC 统一。
    starvation_ticks: int = 5
    # 单个 tick 内最多派工多少个合同（0 = 不限制）
    max_dispatches_per_tick: int = 0


DEFAULT_FAIRNESS = FairnessConfig()


@dataclass(slots=True)
class ContractFairnessState:
    """单个合同的公平性跟踪状态。"""

    contract_id: str
    last_tier: int | None = None
    tier_consecutive_ticks: int = 0
    ticks_since_last_dispatch: int = 0

    def observe_tick(self, tier: int, dispatched: bool) -> bool:
        """每个 tick 调用一次：更新跟踪状态，返回是否处于饥饿状态。

        Args:
            tier: 本 tick 的紧迫档位（0=QUEUED, 3=RESPAWN 等）。
            dispatched: 本 tick 是否成功派工（True=有新 attempt 启动）。

        Returns:
            True 表示该合同已连续 starvation_ticks 个 tick 未被派工，
            下一 tick 应该获得优先处理（不与同档位其他合同竞争）。
        """
        if dispatched:
            self.tier_consecutive_ticks = 0
            self.ticks_since_last_dispatch = 0
        else:
            self.ticks_since_last_dispatch += 1

        if self.last_tier == tier:
            self.tier_consecutive_ticks += 1
        else:
            self.tier_consecutive_ticks = 1
            self.last_tier = tier

        return self.ticks_since_last_dispatch >= DEFAULT_FAIRNESS.starvation_ticks


@dataclass(slots=True)
class TickCapacityLedger:
    """单个 tick 的派工容量记账。"""

    _config: FairnessConfig = field(default_factory=FairnessConfig)
    _dispatched: set[str] = field(default_factory=set)

    def can_dispatch(self, contract_id: str) -> bool:
        """检查本 tick 是否还能派工该合同（未被派过 + 未超 cap）。"""
        if contract_id in self._dispatched:
            return False
        if self._config.max_dispatches_per_tick <= 0:
            return True
        return len(self._dispatched) < self._config.max_dispatches_per_tick

    def record_dispatch(self, contract_id: str) -> None:
        """记录一次成功派工。"""
        self._dispatched.add(contract_id)

    @property
    def dispatched_count(self) -> int:
        return len(self._dispatched)


def apply_fairness_order(
    contract_ids: list[str],
    urgency_by_contract: dict[str, int],
    fairness_states: dict[str, ContractFairnessState],
) -> list[str]:
    """对已按紧迫降序排好的合同列表应用公平性调整。

    规则：
    1. 饥饿合同（连续 ≥ starvation_ticks 个 tick 未派工）提到最前面
    2. 其余按紧迫档位降序（原顺序不变）

    Args:
        contract_ids: 已按紧迫降序排列的合同 ID 列表。
        urgency_by_contract: 每个合同的紧迫档位（用于同档位排序）。
        fairness_states: 每个合同的公平性跟踪状态。

    Returns:
        调整后的合同 ID 列表。
    """
    starved = [
        cid
        for cid in contract_ids
        if fairness_states.get(cid) is not None
        and fairness_states[cid].ticks_since_last_dispatch >= DEFAULT_FAIRNESS.starvation_ticks
    ]
    starved_set = set(starved)
    rest = [cid for cid in contract_ids if cid not in starved_set]
    return starved + rest


__all__ = [
    "DEFAULT_FAIRNESS",
    "ContractFairnessState",
    "FairnessConfig",
    "TickCapacityLedger",
    "apply_fairness_order",
]
