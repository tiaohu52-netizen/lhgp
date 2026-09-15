"""骨架模块的词汇表与小逻辑：事件类型、方法表、时钟、投影路径、适配器杂项。

这些测试把「骨架期已有定义」钉住，防止后续实现悄悄改语义。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.base import PrepareRefusedError
from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.manifest import MANIFEST_PROTOCOL_VERSION
from longtask.adapters.subprocess_adapter import SubprocessAdapter, refuse
from longtask.persistence import projections
from longtask.persistence.events import EventType
from longtask.promoter.escalation import EscalationDecision
from longtask.promoter.urgency import UrgencyTier
from longtask.rpc.methods import IDEMPOTENT_METHODS, Method
from longtask.scheduler.ticker import ContractClock, is_overdue, next_wakeup

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)


class TestEventVocabulary:
    def test_values_lower_kebab_with_namespace(self) -> None:
        pattern = re.compile(r"^[a-z][a-z0-9-]*/[a-z][a-z0-9-]*$")
        for event in EventType:
            assert pattern.match(event.value), f"bad event type shape: {event.value}"

    def test_values_unique(self) -> None:
        values = [e.value for e in EventType]
        assert len(values) == len(set(values))


class TestMethodVocabulary:
    def test_idempotent_subset_of_methods(self) -> None:
        assert frozenset(Method) >= IDEMPOTENT_METHODS

    def test_side_effect_methods_are_idempotent_keyed(self) -> None:
        # DESIGN §11.3：有副作用的方法按幂等键处理
        for name in (
            Method.CONTRACT_PREPARE,
            Method.CONTRACT_APPROVE,
            Method.CONTROL_SPAWN,
            Method.LEASE_RENEW,
        ):
            assert name in IDEMPOTENT_METHODS

    def test_readonly_methods_not_idempotent_keyed(self) -> None:
        assert Method.CONTRACT_GET not in IDEMPOTENT_METHODS
        assert Method.PROTOCOL_EVENTS not in IDEMPOTENT_METHODS


class TestContractClock:
    def make_clock(self) -> ContractClock:
        return ContractClock(deadline_at=LATER, next_wakeup_at=LATER, arbitrated_at=None)

    def test_overdue_boundary(self) -> None:
        clock = self.make_clock()
        assert not is_overdue(clock, NOW)
        assert is_overdue(clock, NOW + timedelta(days=6))

    def test_next_wakeup_passthrough(self) -> None:
        clock = self.make_clock()
        assert next_wakeup(clock, NOW) == LATER


class TestProjectionPaths:
    def test_contract_dir_layout(self) -> None:
        root = Path("/home/user/.longtask")
        result = projections.contract_dir(root, "lt-20260831-001")
        assert result == root / "contracts" / "lt-20260831-001"


class TestSubprocessAdapterSkeleton:
    def test_refuse_carries_reason(self) -> None:
        err = refuse("network deny untranslatable")
        assert isinstance(err, PrepareRefusedError)
        assert "network deny untranslatable" in str(err.args[0] if err.args else err)

    def test_adapter_id_and_describe(self) -> None:
        adapter = SubprocessAdapter(FAKE_MANIFEST)
        assert adapter.id == "fake-executor"
        assert adapter.describe() is FAKE_MANIFEST
        assert adapter.describe().protocol_version == MANIFEST_PROTOCOL_VERSION


class TestEscalationDecision:
    def test_decision_shape(self) -> None:
        decision = EscalationDecision(
            tier=UrgencyTier.RESPAWN,
            reason="u=1.2 且无活跃租约",
            consumes_dispatch=True,
            consumes_escalation=False,
        )
        assert decision.tier == UrgencyTier.RESPAWN
        assert decision.consumes_dispatch
        assert not decision.consumes_escalation
