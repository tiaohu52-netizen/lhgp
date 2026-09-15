"""MCP 工具面 schema↔handler 一致性回归（工具面审计建议 3）。

approve 工具曾发 "revision" 而 handler 读 "expected_revision"，CAS 在
MCP 路径静默失效。本回归遍历 TOOLS 全部工具：
1. handler 源码里读取的每个 args[...] / args.get(...) 键必须被 schema
   声明（防止「模型传了也白传」类静默丢弃）；
2. 注册表里两个命名空间的名字都能解析到同一 handler。
"""

from __future__ import annotations

import ast
import inspect

import pytest

from longtask.mcp_server import TOOLS

# 导入 TOOLS/_dispatch 即真实入口（conftest 的 real_entry 棘轮口径）；
# test_schema_covers_handler_reads 按 TOOLS 全量 parametrize，工具面
# 增减会直接改变展开后的 item 数——2026-09-10 新增 lhgp_user_confirm_spec_verdict
# 使欠款 114→115，正是棘轮要拦的「改工具面必须过这里」。
pytestmark = pytest.mark.real_entry

# handler 源码里合法的透传参数名：这些通过 **kwargs / _mcp_route 整包
# 转发，不逐键读取；schema 缺它们不算不一致。
PASSTHROUGH_TOOLS = {
    "longtask_prepare_contract",
    "longtask_prepare_goal",
    "longtask_approve_contract",
}


def _handler_reads(handler) -> set[str]:
    """AST 解析 handler 源码，收集 args['x'] / args.get('x') 读取的键。"""
    try:
        tree = ast.parse(inspect.getsource(handler))
    except (OSError, TypeError, SyntaxError):
        return set()
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == "args" and isinstance(node.slice, ast.Constant):
                keys.add(str(node.slice.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "args"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys.add(str(node.args[0].value))
    return keys


def _schema_declares(schema: dict) -> set[str]:
    return set(schema.get("inputSchema", {}).get("properties", {}).keys())


@pytest.mark.parametrize("tool_name", sorted(TOOLS.keys()))
def test_schema_covers_handler_reads(tool_name: str) -> None:
    handler, schema = TOOLS[tool_name]
    if tool_name in PASSTHROUGH_TOOLS:
        pytest.skip("whole-payload passthrough tool; reads forward the full dict")
    # 别名共享 handler：去重后 handler 名不同才解析
    reads = _handler_reads(handler)
    declared = _schema_declares(schema)
    missing = {k for k in reads if k not in declared and k != "args"}
    assert not missing, (
        f"{tool_name}: handler reads {sorted(missing)} but schema does not "
        "declare them - model passes them and they are silently dropped"
    )


def test_alias_pairs_share_handler() -> None:
    """每个 lhgp_* 别名必须解析到对应 longtask_* 的同一 handler。"""
    for name, (handler, _meta) in TOOLS.items():
        if name.startswith("lhgp_"):
            legacy = "longtask_" + name[len("lhgp_") :]
            # 改名映射的例外：goal/attach 语义对齐由 _RENAMED_TOOLS 显式管理
            if legacy in TOOLS:
                assert TOOLS[legacy][0] is handler, f"alias {name} diverges from {legacy}"


def test_prepare_goal_registered_and_distinct() -> None:
    """goal/prepare 工具激活 advance_goal/goal_contract_draft 的前提。"""
    assert "longtask_prepare_goal" in TOOLS
    assert "lhgp_prepare_goal" in TOOLS
    handler, schema = TOOLS["longtask_prepare_goal"]
    assert handler.__name__ == "tool_prepare_goal"
    assert "goal_id" in schema["inputSchema"]["properties"]
    assert "stage_id" in schema["inputSchema"]["properties"]


def test_dispatch_rejects_unknown_and_typed_arguments(tmp_path) -> None:
    """R1：dispatch 层 schema 强制（required/类型/未知键）。

    ctx 按服务端真实形态构造（profile_tools = legacy 全量）：直呼 _dispatch
    的旧空 ctx 自 profile 轮起被 fail-closed 拒绝，见
    test_tool_profiles.py::test_unprofiled_context_refuses_to_serve。
    """
    from longtask.mcp_server import TOOLS, _dispatch

    ctx: dict = {
        "profile_name": "legacy",
        "profile_tools": tuple(sorted(TOOLS)),
    }
    # 未知键
    resp = _dispatch(ctx, "tools/call", {"name": "longtask_health", "arguments": {"bogus": 1}}, 1)
    assert "unknown parameter" in resp["error"]["message"]
    # 类型错误
    resp = _dispatch(
        ctx,
        "tools/call",
        {"name": "longtask_get_contract", "arguments": {"contract_id": 123}},
        2,
    )
    assert "must be of type string" in resp["error"]["message"]
    # required 缺失
    resp = _dispatch(ctx, "tools/call", {"name": "longtask_get_contract", "arguments": {}}, 3)
    assert "missing required parameter: contract_id" in resp["error"]["message"]
