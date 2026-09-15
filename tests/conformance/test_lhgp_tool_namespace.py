"""规范 lhgp_* 工具命名空间的暴露完整性（P6 双轨期）。

旧命名 `longtask_*` 与规范命名 `lhgp_*` 并存，但此前合同「读取」与「列出」
两项能力只挂在旧命名上：只见 lhgp_* 工具集的 AI 无法查看自己立下的合同，
规范工具集存在死角。本文件把这条暴露缺口钉住。

覆盖：
- lhgp_get_contract / lhgp_list_contracts 可被发现
- 只用 lhgp_* 工具即可完成「立合同 → 读合同 → 列出合同」
- 按状态过滤列出
- 结构性守卫：合同读取/列出能力在规范命名空间有对应项（防未来漂移）
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.mcp_server import TOOLS
from longtask.persistence.store import StoreConfig, connect, ensure_schema

pytestmark = pytest.mark.conformance

NOW = datetime(2026, 9, 3, 22, 0, 0, tzinfo=UTC)
DEADLINE = (NOW + timedelta(hours=6)).isoformat()

# 合同读取/列出能力：规范名 -> 它复用的旧工具（同处理链）
CONTRACT_READ_CAPABILITIES = {
    "lhgp_get_contract": "longtask_get_contract",
    "lhgp_list_contracts": "longtask_list_contracts",
}


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    return conn


def _ctx(tmp_path: Path, conn: sqlite3.Connection) -> dict:
    """Mirror the shape the MCP server hands to a tool handler."""
    return {"root": tmp_path, "conn": conn, "registry": None}


def _call(tool_name: str, args: dict, ctx: dict) -> dict:
    """Invoke a tool exactly as the MCP dispatch path does."""
    handler, _schema = TOOLS[tool_name]
    return handler(args, ctx)


@pytest.fixture()
def prepared(tmp_path):
    """A contract created through the canonical lhgp_* namespace only."""
    conn = _conn(tmp_path)
    ctx = _ctx(tmp_path, conn)
    result = _call(
        "lhgp_prepare_goal",
        {
            "contract_id": "lt-20260903-namespace-1",
            "request_id": "req-namespace-prepare",
            "title": "规范命名空间暴露验证",
            "objective": "只用 lhgp_* 工具立合同并读回",
            "deadline_at": DEADLINE,
            "acceptance_standard": "能通过 lhgp_get_contract 读到同一份合同",
            "acceptance_checks": ["result.txt 存在"],
            "workload_initial_hours": 1.0,
            "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        },
        ctx,
    )
    return conn, ctx, result


def test_canonical_namespace_exposes_contract_read_and_list() -> None:
    """两项能力必须以规范名可发现，而不是只挂在 longtask_* 上。"""
    for canonical in CONTRACT_READ_CAPABILITIES:
        assert canonical in TOOLS, f"规范工具缺失: {canonical}"
        _handler, schema = TOOLS[canonical]
        assert schema.get("description"), f"{canonical} 缺描述"


def test_contract_read_schema_exposes_decision_history_limit() -> None:
    """模型工具描述必须保留风险历史查询的可控上限。"""
    _handler, schema = TOOLS["lhgp_get_contract"]
    properties = schema["inputSchema"]["properties"]
    assert properties["decision_limit"]["type"] == "integer"
    assert properties["decision_limit"]["minimum"] == 1
    assert properties["decision_limit"]["maximum"] == 200
    assert properties["attempt_limit"]["type"] == "integer"
    assert properties["attempt_limit"]["minimum"] == 1
    assert properties["attempt_limit"]["maximum"] == 100


def test_canonical_aliases_reuse_the_same_handlers() -> None:
    """规范别名必须复用旧处理链，避免两套语义分叉。"""
    for canonical, legacy in CONTRACT_READ_CAPABILITIES.items():
        assert TOOLS[canonical][0] is TOOLS[legacy][0]


def test_only_canonical_tools_can_read_and_list_a_contract(prepared) -> None:
    """只用 lhgp_* 工具即可完成立合同 → 读合同 → 列出合同。"""
    conn, ctx, prepare_result = prepared
    assert prepare_result["ok"] is True

    got = _call(
        "lhgp_get_contract",
        {"contract_id": "lt-20260903-namespace-1", "request_id": "req-namespace-get"},
        ctx,
    )
    assert got["ok"] is True
    # contract/get 直接返回权威合同视图（§11.6 字段表），不额外包一层
    contract = got["result"]
    assert contract["contract_id"] == "lt-20260903-namespace-1"
    assert contract["objective"] == "只用 lhgp_* 工具立合同并读回"
    assert contract["state"] == "drafted"

    listed = _call("lhgp_list_contracts", {"request_id": "req-namespace-list"}, ctx)
    assert listed["ok"] is True
    ids = [item["contract_id"] for item in listed["result"]["contracts"]]
    assert "lt-20260903-namespace-1" in ids
    conn.close()


def test_canonical_list_filters_by_state(prepared) -> None:
    """规范列表工具按状态过滤可用。"""
    conn, ctx, _ = prepared
    listed = _call(
        "lhgp_list_contracts",
        {"state": "drafted", "request_id": "req-namespace-filter"},
        ctx,
    )
    assert listed["ok"] is True
    ids = [item["contract_id"] for item in listed["result"]["contracts"]]
    assert "lt-20260903-namespace-1" in ids

    empty = _call(
        "lhgp_list_contracts",
        {"state": "cancelled", "request_id": "req-namespace-filter-empty"},
        ctx,
    )
    assert empty["result"]["contracts"] == []
    conn.close()


def test_contract_read_capabilities_do_not_drift_to_legacy_only() -> None:
    """结构性守卫：不得再出现「只有旧命名暴露」的合同读取/列出能力。

    只看 lhgp_* 工具集的 AI 必须能覆盖这些能力；若某个旧工具的处理链没有
    任何规范名暴露，该能力对规范调用方不可见。
    """
    canonical_handlers = {
        handler for name, (handler, _) in TOOLS.items() if name.startswith("lhgp_")
    }
    for legacy in CONTRACT_READ_CAPABILITIES.values():
        assert TOOLS[legacy][0] in canonical_handlers, (
            f"合同能力 {legacy} 只以旧命名暴露；规范工具集的 AI 看不到它"
        )
