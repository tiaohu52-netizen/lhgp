"""执行者侧会话 RPC 集成测试（DESIGN §11.2、§7、§14.1）。

attempt/status、lease/renew、attempt/write-back 三方法（被拉起会话的
协作通道）。核心保证：fencing（旧代次/非持有人写回 LEASE_FENCED、
事件不落库）、request_id 幂等（重试不重复落事件）、词汇表内事件。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask import PROTOCOL_VERSION
from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    acquire_lease,
    connect,
    ensure_schema,
    get_events,
    get_lease,
    reclaim_lease,
    save_contract,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope, route

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
CID = "lt-execsess01"
# 安全加固（2026-09-12 审查整改）：签发过凭据的 attempt 写回必须出示
# session_token（与 attempts.session_token_hash 的 sha256 对应）。
TEST_SESSION_TOKEN = "test-session-token"  # noqa: S105 —— 测试夹具常量，非真实凭据


def make_envelope(method: Method, params: dict[str, Any], request_id: str) -> RequestEnvelope:
    if method == Method.ATTEMPT_WRITE_BACK and "session_token" not in params:
        params = {**params, "session_token": TEST_SESSION_TOKEN}
    return RequestEnvelope(
        method=method,
        request_id=request_id,
        client_id="executor-session",
        protocol_version=PROTOCOL_VERSION,
        params=params,
    )


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="执行者侧会话合同",
            objective="验证执行者协作通道",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=3,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=60,
                max_output_bytes=1048576,
            ),
        ),
        contract_id=CID,
        now=NOW,
    )
    try:
        yield conn
    finally:
        conn.close()


def acquired(conn: Any) -> None:
    token_hash = hashlib.sha256(TEST_SESSION_TOKEN.encode()).hexdigest()
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
        " admitted_at, contract_revision, updated_at, session_token_hash)"
        " VALUES ('att-1', ?, ?, 'executor', 'running', ?, 1, ?, ?)",
        (CID, CID, NOW.isoformat(), NOW.isoformat(), token_hash),
    )
    conn.commit()
    acquire_lease(
        conn,
        contract_id=CID,
        holder_attempt_id="att-1",
        expected_generation=0,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=30),
    )


class TestAttemptStatus:
    def test_status_returns_events_and_lease(self, store: Any) -> None:
        acquired(store)
        resp = route(
            make_envelope(
                Method.ATTEMPT_STATUS,
                {"contract_id": CID, "attempt_id": "att-1"},
                "req-status-1",
            ),
            conn=store,
            now=NOW,
        )
        result = resp["result"]
        assert result["lease"]["generation"] == 1
        assert result["lease"]["holder_attempt_id"] == "att-1"
        assert result["lease"]["is_alive"] is True
        types = [e["event_type"] for e in result["events"]]
        assert "lease/acquired" in types

    def test_status_rejects_unknown_contract(self, store: Any) -> None:
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.ATTEMPT_STATUS,
                    {"contract_id": "lt-none", "attempt_id": "att-1"},
                    "req-status-2",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.UNKNOWN_CONTRACT


class TestLeaseRenew:
    def test_holder_renews_heartbeat(self, store: Any) -> None:
        acquired(store)
        later = NOW + timedelta(minutes=10)
        resp = route(
            make_envelope(
                Method.LEASE_RENEW,
                {"contract_id": CID, "attempt_id": "att-1", "timeout_seconds": 1200},
                "req-renew-1",
            ),
            conn=store,
            now=later,
        )
        assert resp["result"]["generation"] == 1
        assert resp["result"]["heartbeat_at"] == later.isoformat()
        lease = get_lease(store, CID)
        assert lease is not None
        assert lease.is_alive(later)

    def test_non_holder_renew_is_fenced(self, store: Any) -> None:
        acquired(store)
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.LEASE_RENEW,
                    {"contract_id": CID, "attempt_id": "att-imposter"},
                    "req-renew-2",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.LEASE_FENCED

    def test_invalid_timeout_rejected(self, store: Any) -> None:
        acquired(store)
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.LEASE_RENEW,
                    {"contract_id": CID, "attempt_id": "att-1", "timeout_seconds": -5},
                    "req-renew-3",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_FAILED

    def test_boolean_timeout_rejected(self, store: Any) -> None:
        acquired(store)
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.LEASE_RENEW,
                    {"contract_id": CID, "attempt_id": "att-1", "timeout_seconds": True},
                    "req-renew-bool",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_FAILED


class TestAttemptWriteBack:
    def test_boolean_write_generation_rejected(self, store: Any) -> None:
        acquired(store)
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.ATTEMPT_WRITE_BACK,
                    {
                        "contract_id": CID,
                        "attempt_id": "att-1",
                        "write_generation": True,
                        "progress_note": "invalid generation type",
                    },
                    "req-wb-bool-generation",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_FAILED

    def test_verifier_terminal_write_back_requires_structured_evidence(self, store: Any) -> None:
        acquired(store)
        # acquired() 已插入 att-1（executor）；本测试需要 verifier 语义，
        # 原地改 role 而不是重复 INSERT（UNIQUE 冲突）。
        store.execute("UPDATE attempts SET role = 'verifier' WHERE attempt_id = 'att-1'")
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.ATTEMPT_WRITE_BACK,
                    {
                        "contract_id": CID,
                        "attempt_id": "att-1",
                        "write_generation": 1,
                        "attempt_state": "succeeded",
                    },
                    "req-verifier-evidence-required",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_FAILED

    def test_progress_and_terminal_events(self, store: Any) -> None:
        acquired(store)
        resp = route(
            make_envelope(
                Method.ATTEMPT_WRITE_BACK,
                {
                    "contract_id": CID,
                    "attempt_id": "att-1",
                    "write_generation": 1,
                    "progress_note": "模块完成一半，测试还差两个用例",
                    "attempt_state": "succeeded",
                    "model_id": "gpt-test-1",
                },
                "req-wb-1",
            ),
            conn=store,
            now=NOW + timedelta(minutes=5),
        )
        assert resp["ok"] is True
        assert resp["result"]["lease_generation"] == 1
        types = [str(e.event_type) for e in get_events(store, contract_id=CID)]
        assert EventType.CONTEXT_SCRATCH_UPDATED.value in types
        assert EventType.ATTEMPT_SUCCEEDED.value in types
        succeeded = [
            e
            for e in get_events(store, contract_id=CID)
            if e.event_type == EventType.ATTEMPT_SUCCEEDED
        ]
        assert '"model_id": "gpt-test-1"' in succeeded[-1].payload_json

    def test_stale_generation_write_is_fenced(self, store: Any) -> None:
        """§7/§14.1：旧代次写回被 fenced，事件不落库。"""
        acquired(store)
        reclaim_lease(
            store,
            contract_id=CID,
            expected_generation=1,
            heartbeat_at=NOW + timedelta(minutes=1),
            timeout=timedelta(minutes=30),
            new_holder_attempt_id="att-2",
        )
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.ATTEMPT_WRITE_BACK,
                    {
                        "contract_id": CID,
                        "attempt_id": "att-1",
                        "write_generation": 1,  # 旧代次
                        "progress_note": "迟到的写回",
                    },
                    "req-wb-2",
                ),
                conn=store,
                now=NOW + timedelta(minutes=2),
            )
        assert exc_info.value.code == ErrorCode.LEASE_FENCED
        types = [str(e.event_type) for e in get_events(store, contract_id=CID)]
        assert EventType.CONTEXT_SCRATCH_UPDATED.value not in types

    def test_request_id_idempotent(self, store: Any) -> None:
        """同 request_id 重试：返回原结果，不产生第二组事件（§11.3）。"""
        acquired(store)
        params = {
            "contract_id": CID,
            "attempt_id": "att-1",
            "write_generation": 1,
            "progress_note": "幂等测试",
        }
        before = len(get_events(store, contract_id=CID))
        resp1 = route(
            make_envelope(Method.ATTEMPT_WRITE_BACK, params, "req-wb-idem"),
            conn=store,
            now=NOW,
        )
        resp2 = route(
            make_envelope(Method.ATTEMPT_WRITE_BACK, params, "req-wb-idem"),
            conn=store,
            now=NOW + timedelta(seconds=1),
        )
        after = len(get_events(store, contract_id=CID))
        assert resp1["result"]["event_ids"] == resp2["result"]["event_ids"]
        assert after - before == 1  # 只落一条 scratch-updated

    def test_validates_missing_generation(self, store: Any) -> None:
        acquired(store)
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.ATTEMPT_WRITE_BACK,
                    {"contract_id": CID, "attempt_id": "att-1"},
                    "req-wb-3",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_FAILED

    def test_validates_bad_attempt_state(self, store: Any) -> None:
        acquired(store)
        with pytest.raises(RpcError) as exc_info:
            route(
                make_envelope(
                    Method.ATTEMPT_WRITE_BACK,
                    {
                        "contract_id": CID,
                        "attempt_id": "att-1",
                        "write_generation": 1,
                        "attempt_state": "exploded",
                    },
                    "req-wb-4",
                ),
                conn=store,
                now=NOW,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_FAILED
