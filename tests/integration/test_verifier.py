"""§5.2 verifier 派出与裁决集成测试。

分层：直接调 AttemptRunner 验证派发（tick 只派首轮，verifier 派生在
收尾钩子里，不经 tick）；写入 verifier 终态事件后让下一轮 tick 走
_judge_verifier_outcomes 钩子裁决合同状态。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon import run_daemon_tick
from longtask.cli.runner import AttemptRunner
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    get_events,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _registry_two(tmp_path: Path) -> tuple[Path, ExecutorRegistry]:
    """两个 fake-kind 执行器：exec-a（执行者）+ exec-b（独立 verifier 候选）。"""
    root = tmp_path / "data"
    root.mkdir()
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
    return root, reg


def _registry_single(tmp_path: Path, exec_id: str = "only-exec") -> tuple[Path, ExecutorRegistry]:
    """单 fake-kind 执行器池（无独立 verifier 候选）。"""
    root = tmp_path / "data"
    root.mkdir()
    reg = ExecutorRegistry()
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
    return root, reg


def _active_contract(tmp_path: Path) -> tuple[Any, str]:
    cid = "lt-ver01"
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="verifier 派遣测试",
            objective="验证 verifier 派生与裁决",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(tmp_path / "ws"),
                }
            },
            acceptance=Acceptance(standard="标准", checks=("check1", "check2")),
            workload_initial_hours=2.5,  # u=1.25 -> RESPAWN 档
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
            ),
        ),
        contract_id=cid,
        now=NOW,
    )
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    return conn, cid


class TestVerifierDispatch:
    def test_two_candidates_spawned_in_order(self, tmp_path: Path) -> None:
        """执行者 succeeded 后派生独立 verifier（候选 ≠ 执行者）。"""
        root, reg = _registry_two(tmp_path)
        conn, cid = _active_contract(root)
        try:
            runner = AttemptRunner(root, conn, reg)
            # 模拟执行者成功收尾：租约已释放（真实路径 _finish_attempt 释放
            # 后才派 verifier）；attempts 行标 succeeded，不挡活租约守卫。
            conn.execute(
                "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id,"
                " state, admitted_at, contract_revision, updated_at, session_token_hash)"
                " VALUES ('att-1', ?, ?, 'executor', 'exec-a', 'succeeded', ?, 1, ?, NULL)",
                (cid, cid, NOW.isoformat(), NOW.isoformat()),
            )
            conn.commit()
            ok = runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-a")
            assert ok is True
            starts = [
                e
                for e in get_events(conn, contract_id=cid)
                if str(e.event_type) == "attempt/started"
            ]
            payload_starts: list[dict[str, Any]] = []
            for e in starts:
                payload = json.loads(e.payload_json or "{}")
                if payload.get("role") == "verifier":
                    payload_starts.append(payload)
            # 至少一条 attempt/started，role=verifier，executor_id=exec-b
            assert payload_starts, "verifier started event not found"
            assert payload_starts[0]["executor_id"] == "exec-b"
            assert payload_starts[0]["verifier_for"] == "exec-a"
        finally:
            conn.close()

    def test_no_independent_candidate_records_handed_to_user(self, tmp_path: Path) -> None:
        """单候选池：执行者 succeeded 后派生失败，无独立候选记 handed-to-user。"""
        root, reg = _registry_single(tmp_path)
        conn, cid = _active_contract(root)
        try:
            runner = AttemptRunner(root, conn, reg)
            # 同上：executor 已 succeeded、租约已释放的收尾后路径。
            conn.execute(
                "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id,"
                " state, admitted_at, contract_revision, updated_at, session_token_hash)"
                " VALUES ('att-1', ?, ?, 'executor', 'only-exec', 'succeeded', ?, 1, ?, NULL)",
                (cid, cid, NOW.isoformat(), NOW.isoformat()),
            )
            conn.commit()
            ok = runner._dispatch_verifier(NOW, contract_id=cid, executor_id="only-exec")
            assert ok is False
            types = [str(e.event_type) for e in get_events(conn, contract_id=cid)]
            assert EventType.ESCALATION_HANDED_TO_USER.value in types
        finally:
            conn.close()


class TestVerifierOutcome:
    """verifier 报告 succeeded/failed → daemon tick 末钩子裁决合同状态。"""

    def _record_verifier(self, conn: Any, cid: str, state: str, **extra: Any) -> None:
        body = {"reported_by": "model", "role": "verifier", "checks": {"check1": "pass"}, **extra}
        append_event(
            conn,
            contract_id=cid,
            attempt_id="ver-test",
            event_type=(
                EventType.ATTEMPT_SUCCEEDED if state == "succeeded" else EventType.ATTEMPT_FAILED
            ),
            payload=body,
            now=NOW,
            actor="model",
        )

    def test_verifier_succeeded_completes_contract(self, tmp_path: Path) -> None:
        root, _reg = _registry_two(tmp_path)
        conn, cid = _active_contract(root)
        try:
            self._record_verifier(conn, cid, "succeeded")
            result = run_daemon_tick(
                root, conn, ExecutorRegistry.load_from_file(root / "registry.json"), now=NOW
            )
            assert result["dispatched"] == 0
            c_view = get_contract(conn, cid)
            assert c_view is not None
            assert c_view.state == ContractState.COMPLETE
            types = [str(e.event_type) for e in get_events(conn, contract_id=cid)]
            assert EventType.CONTRACT_COMPLETED.value in types
        finally:
            conn.close()

    def test_verifier_failed_sends_contract_back_to_active(self, tmp_path: Path) -> None:
        root, _reg = _registry_two(tmp_path)
        conn, cid = _active_contract(root)
        try:
            self._record_verifier(conn, cid, "failed", reason="check1 不通过")
            run_daemon_tick(
                root, conn, ExecutorRegistry.load_from_file(root / "registry.json"), now=NOW
            )
            c_view = get_contract(conn, cid)
            assert c_view is not None
            assert c_view.state == ContractState.ACTIVE
            types = [str(e.event_type) for e in get_events(conn, contract_id=cid)]
            assert EventType.CONTRACT_BLOCKED.value in types
        finally:
            conn.close()
