"""E2 公平性 + E3 提案校验回归（分支 feature/e2-e3-complete）。"""

from __future__ import annotations

from lhgp.promoter.fairness import (
    ContractFairnessState,
    FairnessConfig,
    TickCapacityLedger,
    apply_fairness_order,
)
from lhgp.promoter.proposals import validate_proposed_plan


class TestFairness:
    def test_starvation_after_n_ticks(self) -> None:
        """连续 5 个 tick 未派工 → 饥饿状态。"""
        state = ContractFairnessState("c1")
        starved = False
        for _ in range(5):
            starved = state.observe_tick(tier=0, dispatched=False)
        assert starved, "should be starved after 5 undispatched ticks"
        # 派工后重置
        state.observe_tick(tier=0, dispatched=True)
        assert state.ticks_since_last_dispatch == 0

    def test_no_starvation_when_dispatched(self) -> None:
        state = ContractFairnessState("c1")
        for _ in range(10):
            starved = state.observe_tick(tier=0, dispatched=True)
            assert not starved

    def test_capacity_ledger_respects_cap(self) -> None:
        config = FairnessConfig(max_dispatches_per_tick=2)
        ledger = TickCapacityLedger(config)
        assert ledger.can_dispatch("a")
        assert ledger.can_dispatch("b")
        ledger.record_dispatch("a")
        ledger.record_dispatch("b")
        assert not ledger.can_dispatch("c"), "cap=2 already hit"
        assert ledger.dispatched_count == 2

    def test_capacity_ledger_no_double_dispatch(self) -> None:
        ledger = TickCapacityLedger(FairnessConfig(max_dispatches_per_tick=0))
        assert ledger.can_dispatch("a")
        ledger.record_dispatch("a")
        assert not ledger.can_dispatch("a"), "same contract twice in one tick"

    def test_fairness_order_promotes_starved(self) -> None:
        """饥饿合同提到非饥饿合同前面。"""
        states = {
            "low-urgency": ContractFairnessState("low-urgency"),
            "high-urgency": ContractFairnessState("high-urgency"),
        }
        # low-urgency 饿了 5 tick，high-urgency 刚派工
        for _ in range(5):
            states["low-urgency"].observe_tick(tier=0, dispatched=False)
        states["high-urgency"].observe_tick(tier=3, dispatched=True)

        ordered = apply_fairness_order(
            ["high-urgency", "low-urgency"],  # 原序按紧迫降序
            {"high-urgency": 3, "low-urgency": 0},
            states,
        )
        assert ordered[0] == "low-urgency", "starved contract should come first"


class TestProposalValidation:
    def test_valid_plan_passes(self) -> None:
        plan = {"stages": [{"id": "s1", "contract_id": "lt-001"}, {"id": "s2"}]}
        result = validate_proposed_plan(plan)
        assert result.ok, f"errors: {result.errors}"

    def test_non_dict_rejected(self) -> None:
        assert not validate_proposed_plan("not a dict").ok
        assert not validate_proposed_plan(None).ok

    def test_stages_must_be_list(self) -> None:
        assert not validate_proposed_plan({"stages": "not-a-list"}).ok
        assert not validate_proposed_plan({}).ok  # missing stages

    def test_stage_id_required(self) -> None:
        plan = {"stages": [{"contract_id": "lt-001"}]}
        result = validate_proposed_plan(plan)
        assert not result.ok
        assert any("id" in e for e in result.errors)

    def test_contract_id_path_rejected(self) -> None:
        plan = {"stages": [{"id": "s1", "contract_id": "../escape"}]}
        result = validate_proposed_plan(plan)
        assert not result.ok
        assert any("path" in e for e in result.errors)

    def test_frozen_zone_fields_rejected(self) -> None:
        """提案不得携带执行权限字段（ADR-004 规则 6 核心）。"""
        for forbidden in ("executor", "model", "budget", "authority"):
            plan = {"stages": [{"id": "s1", forbidden: {}}]}
            result = validate_proposed_plan(plan)
            assert not result.ok, f"{forbidden} should be rejected"
            assert any(forbidden in e for e in result.errors)

    def test_max_stages_enforced(self) -> None:
        plan = {"stages": [{"id": f"s{i}"} for i in range(25)]}
        result = validate_proposed_plan(plan)
        assert not result.ok
        assert any("max" in e for e in result.errors)
