"""执行者 write-back 禁止直推合同完成态（安全审查 RPC-C3）。

fencing 三元组（contract_id/attempt_id/write_generation）全部可从只读
工具读到，被提示注入的模型可冒充在跑 attempt 自报 succeeded 并把合同
直推 complete——绕过 verifier 交叉验收与全部验证预算。完成态只能由
verifier 裁决路径（tick _judge_verifier_outcomes）写入。
"""

from __future__ import annotations

import hashlib as _hl
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp import PROTOCOL_VERSION
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import (
    acquire_lease,
    save_contract,
)
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.executor_api import handle_attempt_write_back
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
TEST_TOKEN = "test-session-token-0123456789abcdef"  # noqa: S105 - test fixture
TEST_TOKEN_HASH = _hl.sha256(TEST_TOKEN.encode()).hexdigest()
CID = "lt-wbsec-01"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _contract(conn: sqlite3.Connection) -> None:
    save_contract(
        conn,
        contract_id=CID,
        draft=ContractDraft(
            title="sec",
            objective="o",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=3,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
            ),
        ),
        now=NOW,
        actor="user",
    )


def _attempt_with_lease(conn: sqlite3.Connection, *, role: str = "executor") -> str:
    attempt_id = f"att-{role}-wb"
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
        " admitted_at, contract_revision, updated_at, session_token_hash)"
        " VALUES (?, ?, ?, ?, 'running', ?, 1, ?, ?)",
        (attempt_id, CID, CID, role, NOW.isoformat(), NOW.isoformat(), TEST_TOKEN_HASH),
    )
    acquire_lease(
        conn,
        contract_id=CID,
        holder_attempt_id=attempt_id,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=30),
        actor="daemon",
        payload={},
        role=role,
        contract_revision=1,
        expected_generation=0,
    )
    conn.commit()
    return attempt_id


def _envelope(params: dict[str, Any], request_id: str) -> RequestEnvelope:
    return RequestEnvelope(
        method=Method.ATTEMPT_WRITE_BACK,
        request_id=request_id,
        client_id="executor",
        protocol_version=PROTOCOL_VERSION,
        params=params,
    )


@pytest.mark.parametrize("state", ["complete", "satisfied"])
def test_executor_cannot_set_completion_state(tmp_path: Path, state: str) -> None:
    conn = _conn(tmp_path)
    try:
        _contract(conn)
        attempt_id = _attempt_with_lease(conn, role="executor")
        env = _envelope(
            {
                "contract_id": CID,
                "attempt_id": attempt_id,
                "write_generation": 1,
                "session_token": TEST_TOKEN,
                "attempt_state": "succeeded",
                "contract_state": state,
            },
            "req-wb-c3-exec",
        )
        with pytest.raises(RpcError) as excinfo:
            handle_attempt_write_back(env, conn=conn, now=NOW)
        assert excinfo.value.code == ErrorCode.STATE_FORBIDDEN
        row = conn.execute("SELECT state FROM contracts WHERE contract_id = ?", (CID,)).fetchone()
        assert row[0] == "drafted"
    finally:
        conn.close()


def test_verifier_write_back_still_allowed_to_complete(tmp_path: Path) -> None:
    """门禁不得误伤 verifier 的合法裁决写回。"""
    conn = _conn(tmp_path)
    try:
        _contract(conn)
        conn.execute("UPDATE contracts SET state='active' WHERE contract_id = ?", (CID,))
        conn.commit()
        attempt_id = _attempt_with_lease(conn, role="verifier")
        env = _envelope(
            {
                "contract_id": CID,
                "attempt_id": attempt_id,
                "write_generation": 1,
                "session_token": TEST_TOKEN,
                "attempt_state": "succeeded",
                "contract_state": "complete",
                "evidence": [{"check_id": "c1", "outcome": "pass", "source": "verifier-run"}],
            },
            "req-wb-c3-ver",
        )
        resp = handle_attempt_write_back(env, conn=conn, now=NOW)
        assert resp["ok"] is True
        row = conn.execute("SELECT state FROM contracts WHERE contract_id = ?", (CID,)).fetchone()
        assert row[0] == "complete"
    finally:
        conn.close()


def test_executor_progress_note_unaffected(tmp_path: Path) -> None:
    """普通进度写回（不带 contract_state）不受门禁影响。"""
    conn = _conn(tmp_path)
    try:
        _contract(conn)
        attempt_id = _attempt_with_lease(conn, role="executor")
        env = _envelope(
            {
                "contract_id": CID,
                "attempt_id": attempt_id,
                "write_generation": 1,
                "session_token": TEST_TOKEN,
                "progress_note": "half done",
            },
            "req-wb-c3-note",
        )
        resp = handle_attempt_write_back(env, conn=conn, now=NOW)
        assert resp["ok"] is True
    finally:
        conn.close()
