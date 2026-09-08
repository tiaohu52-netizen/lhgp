"""临时上下文单元测试（DESIGN §4.1）。

覆盖：policy 解析（合同 context 字段/默认值）、快照编译
（合同锚点+交接+attempt 终态摘要，事件 context/snapshot-built）、
容量 fail-closed（context/capacity-refused + CapacityRefusedError）、
交接附言进任务文本（修复「再派 attempt 缺验收上下文」缺口）、
probe 路径不物化快照。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.context import (
    CapacityRefusedError,
    ContextPolicy,
    compile_context_snapshot,
    handover_prompt_addendum,
)
from longtask.persistence.events import EventType
from longtask.persistence.projections import HandoverData
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    get_events,
    save_contract,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def make_draft(*, context: dict | None = None) -> ContractDraft:
    return ContractDraft(
        title="上下文测试合同",
        objective="验证临时上下文",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="s", checks=("c1", "c2")),
        workload_initial_hours=1.5,
        budget=Budget(
            max_dispatches=3,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=60,
            max_output_bytes=1048576,
        ),
        context=context if context is not None else {},
    )


def make_store(tmp_path: Path, cid: str = "lt-ctx01") -> tuple[object, Path]:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(conn, make_draft(), contract_id=cid, now=NOW)
    return conn, root


class TestContextPolicy:
    def test_defaults_without_context_field(self) -> None:
        policy = ContextPolicy.from_contract(make_draft())
        assert policy.required is False
        assert policy.max_bytes == 24000
        assert policy.expires_after_minutes == 240

    def test_parses_contract_context(self) -> None:
        draft = make_draft(
            context={
                "required": True,
                "limits": {"max_bytes": 500, "expires_after_minutes": 30},
            }
        )
        policy = ContextPolicy.from_contract(draft)
        assert policy.required is True
        assert policy.max_bytes == 500
        assert policy.expires_after_minutes == 30

    def test_rejects_non_boolean_required_flag(self) -> None:
        draft = make_draft(context={"required": "false"})
        with pytest.raises(TypeError, match=r"context\.required must be a boolean"):
            ContextPolicy.from_contract(draft)

    def test_rejects_boolean_context_limits(self) -> None:
        draft = make_draft(context={"limits": {"max_bytes": True}})
        with pytest.raises(TypeError, match=r"context\.limits\.max_bytes must be an integer"):
            ContextPolicy.from_contract(draft)

    def test_malformed_limits_fall_back_to_defaults(self) -> None:
        draft = make_draft(context={"required": True, "limits": {"max_bytes": "abc"}})
        policy = ContextPolicy.from_contract(draft)
        assert policy.required is True
        assert policy.max_bytes == 24000  # 形状不对按默认值（fail 只对可选字段）


class TestCompileSnapshot:
    def test_builds_active_and_scratch_with_events(self, tmp_path: Path) -> None:
        conn, root = make_store(tmp_path)
        from longtask.persistence.store import get_contract

        contract = get_contract(conn, "lt-ctx01")
        active, scratch, _consumed, _consumed_ids = compile_context_snapshot(
            root, conn, contract, "att-1", NOW
        )

        body = active.read_text(encoding="utf-8")
        assert "合同锚点" in body
        assert "objective: 验证临时上下文" in body
        assert "c1" in body  # acceptance.checks
        assert "expires_at" in body
        assert scratch.read_text(encoding="utf-8").startswith("# Scratch: att-1")
        assert "current_focus" in scratch.read_text(encoding="utf-8")

        types = [str(e.event_type) for e in get_events(conn, contract_id="lt-ctx01")]
        assert EventType.CONTEXT_SNAPSHOT_BUILT.value in types
        conn.close()

    def test_includes_handover_and_attempt_digest(self, tmp_path: Path) -> None:
        conn, root = make_store(tmp_path)
        from longtask.persistence.store import get_contract

        cid = "lt-ctx01"
        # 写交接 + 一条失败 attempt 终态
        hdir = root / "contracts" / cid
        hdir.mkdir(parents=True, exist_ok=True)
        (hdir / "handover.md").write_text(
            HandoverData(
                current_stage="fix",
                completed_evidence=("模块已交付",),
                remaining=("修复 tests.py 断言",),
                estimate_remaining_hours=0.2,
                next_action="按 FIX-NOTES 修正断言",
                constraints_digest="{}",
                source_attempt_id="att-0",
            ).format_markdown(),
            encoding="utf-8",
        )
        append_event(
            conn,
            contract_id=cid,
            attempt_id="att-0",
            event_type=EventType.ATTEMPT_FAILED,
            payload={"reason": "check 6 failed"},
            now=NOW,
            actor="daemon",
        )
        append_event(
            conn,
            contract_id=cid,
            event_type=EventType.FORECAST_UPDATED,
            payload={"risk": "orange", "next_decision_at": NOW.isoformat()},
            now=NOW,
            actor="promoter",
        )

        contract = get_contract(conn, cid)
        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            root, conn, contract, "att-2", NOW
        )
        body = active.read_text(encoding="utf-8")
        assert "交接" in body
        assert "按 FIX-NOTES 修正断言" in body
        assert "修复 tests.py 断言" in body
        assert "att-0" in body  # 失败摘要进快照
        assert "Deadline 风险快照" in body
        assert '"risk": "orange"' in body
        assert "不是按时完成保证" in body
        conn.close()

    def test_includes_recent_progress_checkpoint_as_untrusted_data(self, tmp_path: Path) -> None:
        """中途 write-back 的进度也能跨 attempt 恢复，但不升级为指令。"""
        conn, root = make_store(tmp_path)
        cid = "lt-ctx01"
        append_event(
            conn,
            contract_id=cid,
            attempt_id="att-0",
            event_type=EventType.CONTEXT_SCRATCH_UPDATED,
            payload={"attempt_id": "att-0", "note": "已完成数据层，下一步补 API"},
            now=NOW,
            actor="model",
        )
        from longtask.persistence.store import get_contract

        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            root, conn, get_contract(conn, cid), "att-1", NOW
        )
        body = active.read_text(encoding="utf-8")
        assert "progress data (untrusted)" in body
        assert "已完成数据层，下一步补 API" in body
        conn.close()

    def test_capacity_refused_is_fail_closed(self, tmp_path: Path) -> None:
        conn, root = make_store(tmp_path)
        from longtask.persistence.store import get_contract

        # 重立一份超小容量的合同
        save_contract(
            conn,
            make_draft(context={"required": True, "limits": {"max_bytes": 50}}),
            contract_id="lt-ctx02",
            now=NOW,
        )
        contract = get_contract(conn, "lt-ctx02")
        with pytest.raises(CapacityRefusedError, match="exceeds policy max_bytes"):
            compile_context_snapshot(root, conn, contract, "att-1", NOW)[0:2]
        types = [str(e.event_type) for e in get_events(conn, contract_id="lt-ctx02")]
        assert EventType.CONTEXT_CAPACITY_REFUSED.value in types
        conn.close()


class TestHandoverPromptAddendum:
    def test_empty_without_handover(self, tmp_path: Path) -> None:
        conn, root = make_store(tmp_path)
        assert handover_prompt_addendum(root, "lt-ctx01") == ""
        conn.close()

    def test_returns_next_action_and_remaining(self, tmp_path: Path) -> None:
        conn, root = make_store(tmp_path)
        cid = "lt-ctx01"
        hdir = root / "contracts" / cid
        hdir.mkdir(parents=True, exist_ok=True)
        (hdir / "handover.md").write_text(
            HandoverData(
                current_stage="fix",
                completed_evidence=(),
                remaining=("修复断言",),
                estimate_remaining_hours=0.2,
                next_action="改 assertFalse",
                constraints_digest="{}",
                source_attempt_id="att-0",
            ).format_markdown(),
            encoding="utf-8",
        )
        addendum = handover_prompt_addendum(root, cid)
        assert "改 assertFalse" in addendum
        assert "修复断言" in addendum
        assert "不可信" in addendum
        conn.close()


def test_attempt_input_carries_context_and_prompt_addendum(tmp_path: Path) -> None:
    """runner 的 build_attempt_input：快照路径 + 交接附言融入任务文本。"""
    from longtask.cli.runner import build_attempt_input
    from longtask.persistence.store import get_contract

    conn, root = make_store(tmp_path)
    cid = "lt-ctx01"
    hdir = root / "contracts" / cid
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / "handover.md").write_text(
        HandoverData(
            current_stage="fix",
            completed_evidence=(),
            remaining=("修复断言",),
            estimate_remaining_hours=0.2,
            next_action="改 assertFalse",
            constraints_digest="{}",
            source_attempt_id="att-0",
        ).format_markdown(),
        encoding="utf-8",
    )
    contract = get_contract(conn, cid)

    input_, _consumed, _consumed_ids = build_attempt_input(root, conn, contract, "att-3", NOW)
    assert input_.context_snapshot_path is not None
    assert "active.md" in input_.context_snapshot_path
    assert "验证临时上下文" in input_.task_prompt  # objective 仍在
    assert "改 assertFalse" in input_.task_prompt  # 交接附言已融入
    assert "不可信" in input_.task_prompt  # 历史模型文本不得伪装成当前指令

    # probe 路径：不物化快照、无附言（§10 时序：探针先于租约）
    probe, _consumed, _consumed_ids = build_attempt_input(
        root, conn, contract, "att-3", NOW, with_context=False
    )
    assert probe.context_snapshot_path is None
    # probe 仍带冻结区摘要（§11.2 合同可见性），但不带交接附言
    assert "验证临时上下文" in probe.task_prompt
    assert "acceptance.checks" in probe.task_prompt
    assert "hard_constraints" in probe.task_prompt
    assert "改 assertFalse" not in probe.task_prompt
    conn.close()
