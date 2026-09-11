"""失败的验证者 attempt 必须被识别为验证者（审计 B9）。

缺陷链：
1. `_fail_attempt`（以及 `_mark_stale`、取消路径）写 attempt 生命周期事件时**不带
   role`，于是 `events.role` 为 NULL；
2. attempt/started 与 attempt/orphaned 早就带 role，所以这不是「没这个字段」，
   而是部分写入方漏了；
3. 读取方对 NULL 的兜底是「在 payload 里找 role」的旧版兼容分支
   （`tick.py:733`、`contract.py:1036`），而这条路径的 payload 是
   ``{"reason": ...}``——找不到。

后果不是审计缺字段，而是**行为错**：`_judge_verifier_outcomes` 里专门处理
「验证者失败」的分支（写 RepairBrief、置 acceptance_status=FAILED、记
CONTRACT_BLOCKED，SPEC §12.4 的修复闭环）对失败的验证者 attempt 不可见，
每轮 tick 都静默空转——合同停在 active，修复上下文永远不产生。

修法遵循仓库既有原则：**改写入方，不改读取方**（读取方的 payload 兜底留给旧版
事件，不动）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.cli.runner import AttemptRunner
from longtask.cli.tick import _judge_verifier_outcomes
from longtask.contracts.schema import (
    Acceptance,
    AcceptanceStatus,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.events import EventType
from longtask.persistence.events_query import get_events
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
CID = "lt-verifier-role"


def _setup(tmp_path: Path) -> tuple[Path, Any, ExecutorRegistry]:
    root = tmp_path / "data"
    ws = root / "ws"
    ws.mkdir(parents=True)
    (ws / "result.txt").write_text("deliverable\n", encoding="utf-8")
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="验证者失败可见性",
            objective="失败的 verifier attempt 必须能被识别",
            deadline_at=NOW + timedelta(hours=1),
            hard_constraints={
                "file_effects": {"mode": "workspace-write", "workspace_root": str(ws)}
            },
            acceptance=Acceptance(
                standard="全部通过", checks=({"kind": "file-exists", "target": "result.txt"},)
            ),
            workload_initial_hours=1.5,
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
                verification_attempts_reserved=3,
            ),
        ),
        contract_id=CID,
        now=NOW,
    )
    update_contract_state(conn, contract_id=CID, new_state=ContractState.ACTIVE, now=NOW)
    reg = ExecutorRegistry()
    for exec_id in ("exec-a", "exec-b"):
        reg.register(
            RegistryEntry(
                id=exec_id,
                kind="fake",
                launch=LaunchSpec(),
                capabilities=FAKE_MANIFEST.capabilities,
                limits={"max_concurrent_attempts": 2},
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
    reg.save_to_file(root / "registry.json")
    return root, conn, reg


def _failed_verifier_dispatch(tmp_path: Path) -> tuple[Path, Any]:
    """驱动真实派发路径，让 verifier 因「无适配器」失败。

    ``adapter_factory`` 一律返回 None → 命中 ``_dispatch_verifier`` 里
    ``adapter is None`` 那条分支（真实代码路径，不是直接调私有方法伪造事件）。
    """
    root, conn, reg = _setup(tmp_path)
    runner = AttemptRunner(root, conn, reg, adapter_factory=lambda _entry: None)
    dispatched = runner._dispatch_verifier(NOW, contract_id=CID, executor_id="exec-a")
    assert dispatched is False
    events = list(get_events(conn, contract_id=CID))
    assert any(str(e.event_type) == EventType.ATTEMPT_FAILED.value for e in events), (
        "verifier 派发没有走到 adapter 缺失分支——事件="
        f"{[(str(e.event_type), e.role) for e in events]}"
    )
    return root, conn


def _attempt_events(conn: Any) -> list[Any]:
    return [e for e in get_events(conn, contract_id=CID) if e.attempt_id is not None]


def test_failed_verifier_dispatch_records_the_verifier_role(tmp_path: Path) -> None:
    _root, conn = _failed_verifier_dispatch(tmp_path)
    failed = [
        e for e in _attempt_events(conn) if str(e.event_type) == EventType.ATTEMPT_FAILED.value
    ]
    assert failed, "必须有 attempt/failed 事件"
    assert failed[-1].role == "verifier", (
        f"失败的 verifier attempt 事件 role={failed[-1].role!r}，读取方会把它当成执行者 attempt"
    )


def test_every_attempt_scoped_event_carries_a_role(tmp_path: Path) -> None:
    """不变量：带 attempt_id 的事件都必须有 role（本次流程内逐条核对）。"""
    _root, conn = _failed_verifier_dispatch(tmp_path)
    missing = [(str(e.event_type), e.attempt_id) for e in _attempt_events(conn) if e.role is None]
    assert missing == [], f"这些 attempt 事件没有 role：{missing}"


def test_failed_verifier_triggers_the_repair_loop(tmp_path: Path) -> None:
    """行为级后果：修复闭环必须真的动起来（SPEC §12.4）。

    修复前：失败的 verifier 事件对 `_judge_verifier_outcomes` 不可见，
    acceptance_status 停在原值、不写 RepairBrief、不记 CONTRACT_BLOCKED。
    """
    root, conn = _failed_verifier_dispatch(tmp_path)
    _judge_verifier_outcomes(root, conn, NOW)

    contract = get_contract(conn, CID)
    assert contract is not None
    assert contract.acceptance_status == AcceptanceStatus.FAILED, (
        f"验证者失败后 acceptance_status={contract.acceptance_status}"
    )
    blocked = [
        e
        for e in get_events(conn, contract_id=CID)
        if str(e.event_type) == EventType.CONTRACT_BLOCKED.value
    ]
    assert blocked, "验证者失败必须记 contract/blocked（带 RepairBrief）"
    handover = root / "contracts" / CID / "handover.md"
    assert handover.is_file(), "修复上下文必须落到 handover.md"
    # 不断言某一分支的文案（failed_checks 为空时会走另一条兜底文案），
    # 只断言契约：这份 brief 必须绑定到那个失败的 verifier attempt。
    failed_id = [
        e.attempt_id
        for e in _attempt_events(conn)
        if str(e.event_type) == EventType.ATTEMPT_FAILED.value
    ][-1]
    text = handover.read_text(encoding="utf-8")
    assert failed_id in text, f"RepairBrief 未指向失败的 verifier attempt {failed_id}"
    assert "repair" in text
