"""Principal 权限门禁回归（安全审查 RPC-C1，SPEC §4.2）。

模型客户端（client_id="mcp" → actor="model"）不得自批准合同或代行
用户专属状态变更；这些决定权属于 Principal，必须经 CLI（actor="user"）。
MCP 的 destructiveHint 注解只是提示，host 可忽略，强制点在服务端。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lhgp import PROTOCOL_VERSION
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import save_contract
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.handlers.contract import (
    handle_contract_approve,
    handle_contract_cancel,
)
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _make_contract(conn: sqlite3.Connection, contract_id: str, *, state: str = "drafted") -> None:
    if state == "drafted":
        draft = ContractDraft(
            title="t",
            objective="o",
            deadline_at=datetime(2030, 1, 1, tzinfo=UTC),
            workload_initial_hours=1.0,
            hard_constraints=[],
            acceptance=Acceptance(standard="产物存在", checks=("file-exists:x",)),
            budget=Budget(
                max_dispatches=2,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=10,
                max_output_bytes=10000,
            ),
        )
        save_contract(conn, contract_id=contract_id, draft=draft, now=NOW, actor="user")
    else:
        raise NotImplementedError(state)


def _envelope(method: Method, params: dict, client_id: str) -> RequestEnvelope:
    return RequestEnvelope(
        method=method,
        request_id=f"req-{method.value}-{client_id}",
        client_id=client_id,
        protocol_version=PROTOCOL_VERSION,
        params=params,
    )


@pytest.mark.parametrize(
    ("handler", "method"),
    [
        (handle_contract_approve, Method.CONTRACT_APPROVE),
        (handle_contract_cancel, Method.CONTRACT_CANCEL),
    ],
)
def test_model_client_cannot_approve_or_cancel(tmp_path: Path, handler, method) -> None:
    conn = _make_conn(tmp_path)
    try:
        _make_contract(conn, "lt-20260905-sec")
        env = _envelope(method, {"contract_id": "lt-20260905-sec"}, client_id="mcp")
        with pytest.raises(RpcError) as excinfo:
            handler(env, conn=conn, now=NOW)
        assert excinfo.value.code == ErrorCode.AUTH_FAILED
        state = conn.execute(
            "SELECT state FROM contracts WHERE contract_id = ?", ("lt-20260905-sec",)
        ).fetchone()[0]
        assert state == "drafted"
    finally:
        conn.close()


def test_user_cli_can_still_approve(tmp_path: Path) -> None:
    """门禁不能误伤真实用户路径（longtask-cli → actor=user）。"""
    conn = _make_conn(tmp_path)
    try:
        _make_contract(conn, "lt-20260905-ok")
        env = _envelope(
            Method.CONTRACT_APPROVE, {"contract_id": "lt-20260905-ok"}, client_id="longtask-cli"
        )
        resp = handle_contract_approve(env, conn=conn, now=NOW)
        assert resp["ok"] is True
        assert resp["result"]["state"] == "active"
    finally:
        conn.close()
