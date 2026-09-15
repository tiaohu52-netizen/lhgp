"""执行器 RPC 控制面方法集成测试（DESIGN §8、§11.2、§11.7）。

测试覆盖：
1. executor/list 列表查询与 enabled_only 过滤；
2. executor/enable 与 executor/disable 用户框定开关；
3. executor/health 健康与能力摘要；
4. UNKNOWN_EXECUTOR 错误码；
5. VALIDATION_FAILED 缺少 executor_id 错误码。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

import pytest

from longtask import PROTOCOL_VERSION
from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.contracts.schema import Enforcement
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope, route

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def make_test_registry() -> ExecutorRegistry:
    reg = ExecutorRegistry()
    caps = Capabilities(
        spawn=True,
        observe=True,
        cancel=True,
        notify=False,
        followup=False,
        steer=False,
        interrupt=False,
        context="optional",
        sandbox=SandboxCapability(
            file_effects="workspace-write",
            network="unsupported",
            process="unsupported",
            enforcement=Enforcement.PARTIAL,
        ),
        acceptance_evidence=True,
    )
    reg.register(
        RegistryEntry(
            id="codex-cli",
            kind="subprocess",
            # health() probes shutil.which(argv[0]); use the interpreter so the
            # fixture stays hermetic on machines without the codex CLI.
            launch=LaunchSpec(argv=(sys.executable, "-c", "pass"), cwd=None, env_allowlist=()),
            capabilities=caps,
            limits={"max_concurrent_attempts": 2},
            cost_hint=CostHint.LOW,
            enabled=False,
        )
    )
    reg.register(
        RegistryEntry(
            id="cli-bridge",
            kind="bridge",
            launch=LaunchSpec(
                argv=("cli-bridge", "--headless"), cwd=None, env_allowlist=("cli-bridge_CONFIG",)
            ),
            capabilities=caps,
            limits={"max_concurrent_attempts": 1},
            cost_hint=CostHint.MEDIUM,
            enabled=True,
        )
    )
    return reg


def make_envelope(
    method: Method,
    params: dict[str, Any] | None = None,
    request_id: str = "req-test-001",
    client_id: str = "test-client",
) -> RequestEnvelope:
    return RequestEnvelope(
        method=method,
        request_id=request_id,
        client_id=client_id,
        protocol_version=PROTOCOL_VERSION,
        params=params or {},
    )


class TestExecutorRpc:
    def test_executor_list_all_and_enabled_only(self) -> None:
        reg = make_test_registry()
        # 查全量
        env = make_envelope(Method.EXECUTOR_LIST)
        resp = route(env, registry=reg, now=NOW)
        assert resp["ok"] is True
        result = resp["result"]
        assert result["total"] == 2
        assert [e["id"] for e in result["executors"]] == ["cli-bridge", "codex-cli"]

        # 仅查已启用
        env_enabled = make_envelope(Method.EXECUTOR_LIST, params={"enabled_only": True})
        resp_enabled = route(env_enabled, registry=reg, now=NOW)
        assert resp_enabled["ok"] is True
        assert resp_enabled["result"]["total"] == 1
        assert resp_enabled["result"]["executors"][0]["id"] == "cli-bridge"

        # JSON 字符串不能被宽松地真值化，否则 "false" 会错误过滤掉执行器。
        env_invalid = make_envelope(Method.EXECUTOR_LIST, params={"enabled_only": "false"})
        with pytest.raises(RpcError) as exc_info:
            route(env_invalid, registry=reg, now=NOW)
        assert exc_info.value.code == ErrorCode.VALIDATION_FAILED
        assert str(exc_info.value.message) == "enabled_only must be a boolean"

    def test_executor_enable_and_disable(self) -> None:
        reg = make_test_registry()
        assert not reg.get("codex-cli").enabled  # type: ignore[union-attr]

        # 启用 codex-cli（CLI = Principal，SPEC §16.1）
        env_en = make_envelope(
            Method.EXECUTOR_ENABLE, params={"executor_id": "codex-cli"}, client_id="cli-test"
        )
        resp_en = route(env_en, registry=reg, now=NOW)
        assert resp_en["ok"] is True
        assert resp_en["result"]["enabled"] is True
        assert reg.get("codex-cli").enabled  # type: ignore[union-attr]

        # 禁用 codex-cli
        env_dis = make_envelope(
            Method.EXECUTOR_DISABLE, params={"executor_id": "codex-cli"}, client_id="cli-test"
        )
        resp_dis = route(env_dis, registry=reg, now=NOW)
        assert resp_dis["ok"] is True
        assert resp_dis["result"]["enabled"] is False
        assert not reg.get("codex-cli").enabled  # type: ignore[union-attr]

    def test_model_client_cannot_toggle_executor(self) -> None:
        """模型客户端不得改授权池（SPEC §16.1：扩大授权 MUST 由 Principal 批准）。

        回归：enable/disable 此前没有任何 actor 校验，任何持 token 的客户端
        都能开关执行器——MCP 的 destructiveHint 只是提示，强制点必须在服务端。
        """
        reg = make_test_registry()
        for method in (Method.EXECUTOR_ENABLE, Method.EXECUTOR_DISABLE):
            env = make_envelope(method, params={"executor_id": "codex-cli"}, client_id="mcp")
            with pytest.raises(RpcError) as exc_info:
                route(env, registry=reg, now=NOW)
            assert exc_info.value.code == ErrorCode.AUTH_FAILED
        # 授权池未被改动
        assert not reg.get("codex-cli").enabled  # type: ignore[union-attr]

    def test_executor_health(self) -> None:
        reg = make_test_registry()
        env = make_envelope(Method.EXECUTOR_HEALTH, params={"executor_id": "codex-cli"})
        resp = route(env, registry=reg, now=NOW)
        assert resp["ok"] is True
        res = resp["result"]
        assert res["executor_id"] == "codex-cli"
        assert res["healthy"] is True
        assert res["cost_hint"] == "low"
        assert res["capabilities"]["spawn"] is True
        assert "health_reason" in res

        # A registry entry with an unsupported transport is not healthy merely
        # because it is present in the configuration.
        bridge_env = make_envelope(
            Method.EXECUTOR_HEALTH,
            params={"executor_id": "cli-bridge"},
            request_id="req-health-bridge",
        )
        bridge_resp = route(bridge_env, registry=reg, now=NOW)
        assert bridge_resp["result"]["healthy"] is False
        assert "no adapter available" in bridge_resp["result"]["health_reason"]

    def test_unknown_executor_error(self) -> None:
        reg = make_test_registry()
        for method in (Method.EXECUTOR_ENABLE, Method.EXECUTOR_DISABLE, Method.EXECUTOR_HEALTH):
            # enable/disable 需 Principal；用 CLI 客户端以越过门禁、测到
            # 目标错误码（模型客户端的拒绝另由 test_model_client_* 覆盖）。
            env = make_envelope(
                method, params={"executor_id": "non-existent"}, client_id="cli-test"
            )
            with pytest.raises(RpcError) as exc_info:
                route(env, registry=reg, now=NOW)
            assert exc_info.value.code == ErrorCode.UNKNOWN_EXECUTOR

    def test_missing_executor_id_validation_error(self) -> None:
        reg = make_test_registry()
        for method in (Method.EXECUTOR_ENABLE, Method.EXECUTOR_DISABLE, Method.EXECUTOR_HEALTH):
            env = make_envelope(method, params={}, client_id="cli-test")
            with pytest.raises(RpcError) as exc_info:
                route(env, registry=reg, now=NOW)
            assert exc_info.value.code == ErrorCode.VALIDATION_FAILED
