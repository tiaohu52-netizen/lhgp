"""executor/* handler：执行器注册表控制面。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lhgp.adapters.factory import build_adapter
from lhgp.adapters.registry import ExecutorRegistry, RegistryEntry
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.handlers._common import require_principal

if TYPE_CHECKING:
    from lhgp.rpc.server import RequestEnvelope


def handle_executor_list(
    envelope: RequestEnvelope,
    *,
    registry: ExecutorRegistry | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """查询执行器注册表列表。"""
    reg = registry or ExecutorRegistry()
    raw_enabled_only = envelope.params.get("enabled_only", False)
    if not isinstance(raw_enabled_only, bool):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="enabled_only must be a boolean",
        )
    enabled_only = raw_enabled_only
    entries = reg.list_entries(enabled_only=enabled_only)
    return {
        "ok": True,
        "result": {"executors": [entry.to_dict() for entry in entries], "total": len(entries)},
    }


def _require_executor_id(params: dict[str, Any]) -> str:
    executor_id = str(params.get("executor_id", "")).strip()
    if not executor_id:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="executor_id is required")
    return executor_id


def _lookup(reg: ExecutorRegistry, executor_id: str) -> RegistryEntry:
    entry = reg.get(executor_id)
    if entry is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_EXECUTOR,
            message=f"executor '{executor_id}' not found",
        )
    return entry


def handle_executor_enable(
    envelope: RequestEnvelope,
    *,
    registry: ExecutorRegistry | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """开启指定执行器。"""
    # 开关执行器 = 改合同可用的授权池（SPEC §8.2「用户框定池子」），
    # 属 Principal 决定权。此前无任何 actor 校验，任何持 token 的客户端
    # （含 MCP 模型客户端）都能改，等于把 §16.1 权限矩阵里
    # 「扩大授权 MUST 由 Principal 批准」这条直接绕开。
    require_principal(envelope, envelope.params, action="executor/enable")
    reg = registry or ExecutorRegistry()
    executor_id = _require_executor_id(envelope.params)
    _lookup(reg, executor_id)
    reg.set_enabled(executor_id, True)
    updated = reg.get(executor_id)
    return {
        "ok": True,
        "result": {"executor": updated.to_dict() if updated else None, "enabled": True},
    }


def handle_executor_disable(
    envelope: RequestEnvelope,
    *,
    registry: ExecutorRegistry | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """关闭指定执行器。"""
    require_principal(envelope, envelope.params, action="executor/disable")
    reg = registry or ExecutorRegistry()
    executor_id = _require_executor_id(envelope.params)
    _lookup(reg, executor_id)
    reg.set_enabled(executor_id, False)
    updated = reg.get(executor_id)
    return {
        "ok": True,
        "result": {"executor": updated.to_dict() if updated else None, "enabled": False},
    }


def handle_executor_health(
    envelope: RequestEnvelope,
    *,
    registry: ExecutorRegistry | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """检查指定执行器健康与配置状态。"""
    reg = registry or ExecutorRegistry()
    executor_id = _require_executor_id(envelope.params)
    entry = _lookup(reg, executor_id)
    # Registration is not proof of liveness.  Probe the concrete adapter so
    # callers can distinguish a configured executor from one that can be
    # launched now; unknown transports fail closed instead of reporting a
    # misleading healthy=True.
    adapter = build_adapter(entry)
    if adapter is None:
        healthy = False
        health_reason = f"no adapter available for kind '{entry.kind}'"
    else:
        try:
            healthy = bool(adapter.health())
            health_reason = (
                "adapter health probe passed" if healthy else "adapter health probe failed"
            )
        except Exception as exc:
            healthy = False
            health_reason = f"adapter health probe error: {type(exc).__name__}"
    return {
        "ok": True,
        "result": {
            "executor_id": entry.id,
            "healthy": healthy,
            "health_reason": health_reason,
            "enabled": entry.enabled,
            "kind": entry.kind,
            "cost_hint": entry.cost_hint.value,
            "capabilities": entry.to_dict()["capabilities"],
        },
    }


__all__ = [
    "handle_executor_disable",
    "handle_executor_enable",
    "handle_executor_health",
    "handle_executor_list",
]
