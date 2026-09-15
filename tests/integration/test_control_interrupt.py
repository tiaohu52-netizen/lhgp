"""control/interrupt 集成测试（DESIGN §10 用户可干涉）。

两段：
1. RPC handler 写盘上 interrupt 请求事件（无 daemon 在线也持久化）；
2. AttemptRunner.cancel_attempt 兑现：adapter.cancel + attempt/cancelled
   + 租约释放 + 停追。daemon 层 _consume_interrupt_requests 在下一轮
   tick 消费。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask import PROTOCOL_VERSION
from longtask.adapters.fake_executor import FakeExecutor
from longtask.cli.daemon import _cancel_terminal_contract_attempts, _consume_interrupt_requests
from longtask.cli.runner import AttemptRunner
from longtask.contracts.schema import Acceptance, Budget, ContractDraft, ContractState
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    acquire_lease,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    get_events,
    get_lease,
    save_contract,
    update_contract_state,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope, route

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
CID = "lt-20260901-interr"


def _make_conn(tmp_path: Path) -> Any:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="interrupt 测试",
            objective="验证打断",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c",)),
            workload_initial_hours=2.0,
            budget=Budget(
                max_dispatches=3,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
            ),
        ),
        contract_id=CID,
        now=NOW,
    )
    update_contract_state(conn, contract_id=CID, new_state=ContractState.ACTIVE, now=NOW)
    return conn


def _envelope(params: dict[str, Any]) -> RequestEnvelope:
    return RequestEnvelope(
        method=Method.CONTROL_INTERRUPT,
        request_id="req-int-1",
        client_id="cli",
        protocol_version=PROTOCOL_VERSION,
        params=params,
    )


class TestInterruptHandler:
    def test_handler_writes_cancelled_event(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            resp = route(
                _envelope(
                    {
                        "contract_id": CID,
                        "attempt_id": "att-x",
                        "reason": "user wants stop",
                    }
                ),
                conn=conn,
                now=NOW,
            )
            assert resp["ok"] is True
            events = get_events(conn, contract_id=CID)
            cancelled = [e for e in events if e.event_type == EventType.ATTEMPT_CANCELLED]
            assert len(cancelled) == 1
            assert '"via": "control/interrupt"' in cancelled[0].payload_json
            assert '"user wants stop"' in cancelled[0].payload_json
        finally:
            conn.close()

    def test_handler_validates_required_fields(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            with pytest.raises(RpcError) as exc_info:
                route(
                    _envelope({"contract_id": CID}),  # 缺 attempt_id
                    conn=conn,
                    now=NOW,
                )
            assert exc_info.value.code == ErrorCode.VALIDATION_FAILED
        finally:
            conn.close()


class TestInterruptConsume:
    def test_terminal_contract_cancels_running_attempt(self, tmp_path: Path) -> None:
        """contract/cancel 后 daemon 必须终止本进程 attempt 并释放租约。"""
        conn = _make_conn(tmp_path)
        try:
            runner = AttemptRunner(
                tmp_path,
                conn,
                __import__(
                    "longtask.adapters.registry", fromlist=["ExecutorRegistry"]
                ).ExecutorRegistry(),
            )
            acquire_lease(
                conn,
                contract_id=CID,
                holder_attempt_id="att-terminal",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=30),
            )
            runner._running["att-terminal"] = {
                "contract_id": CID,
                "executor_id": "missing",
                "session_ref": "subprocess:att-terminal",
                "generation": 1,
            }
            update_contract_state(
                conn,
                contract_id=CID,
                new_state=ContractState.CANCELLED,
                now=NOW + timedelta(seconds=1),
            )

            _cancel_terminal_contract_attempts(conn, runner, NOW + timedelta(seconds=2))

            assert runner.is_idle()
            assert get_lease(conn, CID) is None
            cancelled = [
                event
                for event in get_events(conn, contract_id=CID)
                if event.event_type == EventType.ATTEMPT_CANCELLED
            ]
            assert len(cancelled) == 1
            assert cancelled[0].actor == "daemon"
        finally:
            conn.close()

    def test_daemon_consume_cancels_running_attempt(self, tmp_path: Path) -> None:
        """interrupt 事件 → _consume_interrupt_requests → runner.cancel_attempt
        → attempt/cancelled + 租约释放 + 停追。"""
        conn = _make_conn(tmp_path)
        try:
            # 模拟 runner 正在跑一个 fake attempt
            reg = __import__(
                "longtask.adapters.registry", fromlist=["ExecutorRegistry"]
            ).ExecutorRegistry()
            reg.register(
                __import__("longtask.adapters.registry", fromlist=["RegistryEntry"]).RegistryEntry(
                    id="fake-1",
                    kind="fake",
                    launch=__import__(
                        "longtask.adapters.registry", fromlist=["LaunchSpec"]
                    ).LaunchSpec(),
                    capabilities=FakeExecutor().describe().capabilities,
                    limits={"max_concurrent_attempts": 1},
                    cost_hint=__import__(
                        "longtask.adapters.registry", fromlist=["CostHint"]
                    ).CostHint.LOW,
                    enabled=True,
                )
            )
            runner = AttemptRunner(tmp_path, conn, reg)
            acquire_lease(
                conn,
                contract_id=CID,
                holder_attempt_id="att-live",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=30),
            )
            runner._running["att-live"] = {
                "contract_id": CID,
                "executor_id": "fake-1",
                "session_ref": "fake:att-live",
                "generation": 1,
            }

            # 写 interrupt 请求事件
            append_event(
                conn,
                contract_id=CID,
                attempt_id="att-live",
                event_type=EventType.ATTEMPT_CANCELLED,
                payload={"reason": "stop please", "via": "control/interrupt"},
                now=NOW,
                actor="user",
            )

            _consume_interrupt_requests(tmp_path, conn, runner, NOW)

            # attempt 从 running 移除
            assert "att-live" not in runner._running
            # 租约释放
            assert get_lease(conn, CID) is None
            # 合同状态保持 active
            view = get_contract(conn, CID)
            assert view is not None
            assert view.state == ContractState.ACTIVE
        finally:
            conn.close()

    def test_consume_is_idempotent_for_gone_attempt(self, tmp_path: Path) -> None:
        """interrupt 事件针对已不在 running 的 attempt：no-op，不重复记事件。"""
        conn = _make_conn(tmp_path)
        try:
            reg = __import__(
                "longtask.adapters.registry", fromlist=["ExecutorRegistry"]
            ).ExecutorRegistry()
            runner = AttemptRunner(tmp_path, conn, reg)
            append_event(
                conn,
                contract_id=CID,
                attempt_id="att-gone",
                event_type=EventType.ATTEMPT_CANCELLED,
                payload={"reason": "already done", "via": "control/interrupt"},
                now=NOW,
                actor="user",
            )
            _consume_interrupt_requests(tmp_path, conn, runner, NOW)
            # 不应重复：_running 仍空；取消事件还是那一条
            events = get_events(conn, contract_id=CID)
            cancelled = [e for e in events if e.event_type == EventType.ATTEMPT_CANCELLED]
            assert len(cancelled) == 1
        finally:
            conn.close()
