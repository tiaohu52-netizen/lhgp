"""本机计划任务唤醒 RPC 的边界测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_events,
    save_contract,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.handlers.protocol import handle_daemon_wake
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _make_conn(tmp_path: Path):
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    draft = ContractDraft(
        title="唤醒 RPC 合同",
        objective="验证计划任务唤醒可审计",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="测试通过", checks=("通过",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=2,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1024,
        ),
    )
    save_contract(conn, draft, contract_id="lt-wake-rpc", now=NOW)
    return conn


def _envelope(*, client_id: str = "daemon-wakeup", task_id: str = "longtask-wakeup-lt-wake-rpc"):
    return RequestEnvelope(
        method=Method.DAEMON_WAKE,
        request_id="wake-request-1",
        client_id=client_id,
        protocol_version=1,
        params={"task_id": task_id},
    )


def test_daemon_wake_records_fired_event(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)

    result = handle_daemon_wake(_envelope(), conn=conn, now=NOW)

    assert result["result"] == {
        "task_id": "longtask-wakeup-lt-wake-rpc",
        "contract_id": "lt-wake-rpc",
        "queued_for_daemon": True,
    }
    events = get_events(conn, contract_id="lt-wake-rpc")
    assert events[-1].event_type == EventType.WAKEUP_RTC_FIRED.value
    assert events[-1].actor == "daemon"
    conn.close()


def test_daemon_wake_rejects_non_daemon_client(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        handle_daemon_wake(_envelope(client_id="longtask-cli"), conn=conn, now=NOW)

    assert exc_info.value.code is ErrorCode.AUTH_FAILED
    conn.close()


def test_daemon_wake_rejects_unknown_contract(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)

    with pytest.raises(RpcError) as exc_info:
        handle_daemon_wake(_envelope(task_id="longtask-wakeup-lt-missing"), conn=conn, now=NOW)

    assert exc_info.value.code is ErrorCode.UNKNOWN_CONTRACT
    conn.close()
