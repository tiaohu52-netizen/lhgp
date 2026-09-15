"""P5 修复闭环 + 验证预算独立记账（SPEC §12.4）测试。

verifier fail → repair → reverify 链路：
1. 验证预算：verification_attempts_reserved 独立于 max_dispatches 记账；
   触顶后不再派 verifier，如实记 ESCALATION_HANDED_TO_USER；
2. RepairBrief：verifier 失败的结构化证据（failed_checks/notes）写进
   handover.md 的 remaining/next_action/open_risks——下一轮 attempt 的
   task_prompt 附言与 active.md 快照自动携带（§4.1 通道）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.events import EventType
from longtask.persistence.events_query import append_event, get_events
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def make_draft(*, verification_reserved: int = 1) -> ContractDraft:
    return ContractDraft(
        title="P5 测试合同",
        objective="验证修复闭环",
        deadline_at=NOW + timedelta(hours=4),
        hard_constraints={},
        acceptance=Acceptance(standard="测试", checks=("检查项甲",)),
        workload_initial_hours=2.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
            verification_attempts_reserved=verification_reserved,
        ),
    )


def setup_contract(data_dir: Path, cid: str, *, verification_reserved: int = 1) -> None:
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        ensure_schema(conn)
        save_contract(
            conn, make_draft(verification_reserved=verification_reserved), contract_id=cid, now=NOW
        )
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    finally:
        conn.close()


class TestVerificationBudget:
    def test_budget_field_serializes_and_defaults(self) -> None:
        """字段序列化 + 未声明兜底 2（老库存兼容）。"""
        draft = make_draft(verification_reserved=3)
        d = draft.to_dict()
        assert d["budget"]["verification_attempts_reserved"] == 3
        # 未声明 → 兜底 2
        raw = dict(d)
        raw["budget"] = {
            k: v for k, v in d["budget"].items() if k != "verification_attempts_reserved"
        }
        from longtask.contracts.contract_draft import from_dict

        restored = from_dict(raw)
        assert restored.budget.verification_attempts_reserved == 2

    def test_zero_reserved_is_legal_but_blocks_dispatch(self) -> None:
        """reserved=0 合法（用户明确不要自动验证）——但不能是负数。"""
        assert make_draft(verification_reserved=0).budget.validate() == []

    def test_db_roundtrip_preserves_reserved(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p5a", verification_reserved=2)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            view = get_contract(conn, "lt-p5a")
            assert view.draft.budget.verification_attempts_reserved == 2
        finally:
            conn.close()

    def test_verifier_dispatch_gated_by_reserved_budget(self, tmp_path: Path) -> None:
        """预算触顶 → 不派 verifier，如实记 ESCALATION_HANDED_TO_USER。"""
        from longtask.adapters.fake_executor import FakeExecutor
        from longtask.adapters.registry import ExecutorRegistry
        from longtask.cli.runner import AttemptRunner
        from longtask.promoter.records import _count_verifier_attempts

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # reserved=1：先人工种一条已终态 verifier attempt（=预算用尽）
        setup_contract(data_dir, "lt-p5b", verification_reserved=1)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id, goal_id, contract_id, contract_revision, role, executor_id,
                    state, admitted_at, terminal_at, payload_json, updated_at
                ) VALUES ('ver-used1', 'lt-p5b', 'lt-p5b', 2, 'verifier', 'exec-2',
                          'failed', ?, ?, '{}', ?)
                """,
                (
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
            conn.commit()
            assert _count_verifier_attempts(conn, "lt-p5b") == 1

            registry = ExecutorRegistry()
            runner = AttemptRunner(data_dir, conn, registry)
            runner._adapters["exec-1"] = FakeExecutor()
            dispatched = runner._dispatch_verifier(NOW, contract_id="lt-p5b", executor_id="exec-1")
            assert dispatched is False
            types = [str(e.event_type) for e in get_events(conn, contract_id="lt-p5b")]
            assert EventType.ESCALATION_HANDED_TO_USER.value in types
            # 事件 payload 里写明预算口径（审计可读）
            handover = [
                e
                for e in get_events(conn, contract_id="lt-p5b")
                if str(e.event_type) == EventType.ESCALATION_HANDED_TO_USER.value
            ]
            import json as _json

            payload = _json.loads(handover[-1].payload_json or "{}")
            assert "verification budget exhausted" in payload["reason"]
        finally:
            conn.close()

    def test_verification_budget_is_scoped_to_contract_not_goal(self, tmp_path: Path) -> None:
        """不同阶段合同不得消耗彼此的 verifier 配额。"""
        from longtask.promoter.records import _count_verifier_attempts

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p5-budget-a", verification_reserved=1)
        setup_contract(data_dir, "lt-p5-budget-b", verification_reserved=1)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            conn.execute(
                "UPDATE contracts SET goal_id = 'shared-goal' WHERE contract_id IN (?, ?)",
                ("lt-p5-budget-a", "lt-p5-budget-b"),
            )
            conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id, goal_id, contract_id, contract_revision, role,
                    executor_id, state, admitted_at, terminal_at, payload_json, updated_at
                ) VALUES ('ver-a', 'shared-goal', 'lt-p5-budget-a', 2, 'verifier',
                          'exec-2', 'failed', ?, ?, '{}', ?)
                """,
                (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
            conn.commit()
            assert _count_verifier_attempts(conn, "lt-p5-budget-a") == 1
            assert _count_verifier_attempts(conn, "lt-p5-budget-b") == 0
        finally:
            conn.close()


class TestRepairBrief:
    """verifier 失败 → RepairBrief → handover.md 修复上下文。"""

    def _seed_verifier_failure(self, conn: Any, cid: str, evidence: dict[str, Any]) -> None:
        """种一条 verifier failed 事件（裁决输入）。"""
        append_event(
            conn,
            contract_id=cid,
            attempt_id="ver-fail1",
            event_type=EventType.ATTEMPT_FAILED,
            payload={"role": "verifier", **evidence},
            now=NOW + timedelta(minutes=1),
            actor="verifier",
            goal_id=cid,
            contract_revision=2,
            role="verifier",
        )

    def test_judge_failure_writes_repair_brief_to_handover(self, tmp_path: Path) -> None:
        """裁决器看到 verifier 失败 → handover.md 换上修复上下文。"""
        from longtask.cli.tick import _judge_verifier_outcomes

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-p5c"
        setup_contract(data_dir, cid)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            self._seed_verifier_failure(
                conn,
                cid,
                {
                    "failed_checks": ["检查项甲", "quality-gate"],
                    "stderr": "exit 1: 2 tests failed",
                },
            )
            _judge_verifier_outcomes(data_dir, conn, NOW + timedelta(minutes=2))
            handover_path = data_dir / "contracts" / cid / "handover.md"
            assert handover_path.is_file()
            text = handover_path.read_text(encoding="utf-8")
            # 修复项成为交接的 remaining
            assert "修复验收失败项：检查项甲" in text
            assert "修复验收失败项：quality-gate" in text
            # 失败证据进 open_risks
            assert "exit 1" in text
            # source_attempt_id 指向失败 verifier（追溯链）
            assert "ver-fail1" in text
        finally:
            conn.close()

    def test_next_attempt_prompt_carries_repair_context(self, tmp_path: Path) -> None:
        """闭环端到端：repair 后新 attempt 的 task_prompt 带修复指引。"""
        from longtask.cli.runner import build_attempt_input
        from longtask.cli.tick import _judge_verifier_outcomes

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-p5d"
        setup_contract(data_dir, cid)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            self._seed_verifier_failure(
                conn, cid, {"failed_checks": ["检查项甲"], "reason": "quality gate 红"}
            )
            _judge_verifier_outcomes(data_dir, conn, NOW + timedelta(minutes=2))
            view = get_contract(conn, cid)
            assert view.state == ContractState.ACTIVE  # 退回 active 等再派
            input_, _, _ = build_attempt_input(
                data_dir, conn, view, "att-repair1", NOW + timedelta(minutes=3)
            )
            # 交接附言通道把修复指引带进 task_prompt（§4.1，无需新机制）
            assert "检查项甲" in input_.task_prompt
            assert "quality gate 红" in input_.task_prompt
        finally:
            conn.close()

    def test_block_event_carries_structured_repair_brief(self, tmp_path: Path) -> None:
        """contract/blocked 事件 payload 带 RepairBrief 结构（可审计）。"""
        import json as _json

        from longtask.cli.tick import _judge_verifier_outcomes

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-p5e"
        setup_contract(data_dir, cid)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            self._seed_verifier_failure(conn, cid, {"failed_checks": ["检查项甲"]})
            _judge_verifier_outcomes(data_dir, conn, NOW + timedelta(minutes=2))
            blocked = [
                e
                for e in get_events(conn, contract_id=cid)
                if str(e.event_type) == EventType.CONTRACT_BLOCKED.value
            ]
            assert blocked
            payload = _json.loads(blocked[-1].payload_json or "{}")
            assert payload["repair_brief"]["failed_checks"] == ["检查项甲"]
            assert payload["repair_brief"]["retry_strategy"] == "respawn"
        finally:
            conn.close()

    def test_brief_from_evidence_fallback_shapes(self) -> None:
        """不同 verifier 写回形态都能提炼（failed_checks/fail_reasons/reason）。"""
        from longtask.cli.tick import _repair_brief_from

        b1 = _repair_brief_from("v1", {"failed_checks": ["c1"]})
        assert b1.failed_checks == ("c1",)
        b2 = _repair_brief_from("v2", {"fail_reasons": ["r1", "r2"]})
        assert b2.failed_checks == ("r1", "r2")
        b3 = _repair_brief_from("v3", {"reason": "gate red"})
        assert b3.failed_checks == ()
        assert "gate red" in b3.notes
        b4 = _repair_brief_from("v4", {})
        assert b4.failed_checks == ()  # 没写过失败项就不发明
