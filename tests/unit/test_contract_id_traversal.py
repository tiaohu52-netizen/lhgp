"""contract_id / goal 路径穿越校验回归（安全审查 RPC-C2）。

projections 以 contract_id 拼接 `root/contracts/<contract_id>` 目录；
goal/prepare 此前只有裸 strip() 校验，带 ../ 或绝对路径的 ID 会把
合同投影写到数据根之外，且 DRAFTED 合同的 deadline 过期扫描会
自动触发投影重建——时间炸弹。所有写入入口必须共享同一格式约束。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lhgp import PROTOCOL_VERSION
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.handlers._common import require_contract_id
from lhgp.rpc.handlers.contract import handle_contract_prepare
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)

_BAD_IDS = [
    "lt-x/../../../../escaped_dir",
    "lt-20260905-../escaped",
    "lt-20260905-a\\..\\escaped",
    "/abs/path",
    "C:\\abs",
    "..",
    "lt-20260905-a:b",
]


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_require_contract_id_rejects_path_traversal(bad: str) -> None:
    with pytest.raises(RpcError) as excinfo:
        require_contract_id({"contract_id": bad})
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED


def test_require_contract_id_accepts_canonical() -> None:
    assert require_contract_id({"contract_id": "lt-20260905-abc123"}) == "lt-20260905-abc123"


def test_contract_prepare_rejects_path_traversal(tmp_path: Path) -> None:
    """contract/prepare 原有格式校验保持不回归。"""
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        env = RequestEnvelope(
            method=Method.CONTRACT_PREPARE,
            request_id="req-sec-c2-a",
            client_id="longtask-cli",
            protocol_version=PROTOCOL_VERSION,
            params={
                "contract_id": "lt-x/../../escaped",
                "draft": {
                    "title": "t",
                    "objective": "o",
                    "deadline_at": "2030-01-01T00:00:00+00:00",
                    "workload_estimate": {"initial_hours": 1.0},
                },
            },
        )
        with pytest.raises(RpcError) as excinfo:
            handle_contract_prepare(env, conn=conn, now=NOW)
        assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    finally:
        conn.close()


def test_goal_prepare_rejects_path_traversal(tmp_path: Path) -> None:
    """goal/prepare 用户自报 contract_id 走同一格式约束（本次修复点）。"""
    from lhgp.persistence.schema import ensure_schema
    from lhgp.rpc.handlers.goal import handle_goal_prepare

    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        ensure_schema(conn)
        env = RequestEnvelope(
            method=Method.GOAL_PREPARE,
            request_id="req-sec-c2-b",
            client_id="mcp",
            protocol_version=PROTOCOL_VERSION,
            params={
                "contract_id": "lt-x/../../../../escaped_dir",
                "goal_id": "g1",
                "draft": {
                    "title": "t",
                    "objective": "o",
                    "deadline_at": "2030-01-01T00:00:00+00:00",
                    "workload_estimate": {"initial_hours": 1.0},
                },
            },
        )
        with pytest.raises(RpcError) as excinfo:
            handle_goal_prepare(env, conn=conn, now=NOW)
        assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
        # 数据根外不得出现任何文件
        escaped = Path.home() / "escaped_dir"
        assert not escaped.exists()
    finally:
        conn.close()
