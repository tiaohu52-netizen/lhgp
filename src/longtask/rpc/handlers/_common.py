"""JSON-RPC 控制面 handler 共享小工具（DESIGN §11.2、§11.3、§11.7）。

集中以下重复模式，避免每个 handler 各写一份：

- 服务端可信 actor 派生（C4 修复）：actor 由 envelope.client_id 映射，
  params["actor"] 仅作审计标签，不再覆盖派生值（不变式 #2）。
- 合同 ID 入参校验：空白抛 VALIDATION_FAILED。
- request_id 幂等重放快速返回：找到已有事件则直接返回当前视图，
  不重复执行副作用（DESIGN §11.3）。
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from longtask.rpc.server import RequestEnvelope

from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.auto_approve import from_dict as auto_approve_from_dict
from lhgp.contracts.budget import DEFAULT_VERIFICATION_RESERVED
from longtask.acceptance.checks import parse_check
from longtask.contracts.attention import from_dict as attention_from_dict
from longtask.contracts.authority import from_dict as authority_from_dict
from longtask.contracts.continuity import from_dict as continuity_from_dict
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
)
from longtask.persistence.store import (
    get_contract,
    get_events_by_request_id,
)
from longtask.rpc.errors import ErrorCode, RpcError

if TYPE_CHECKING:
    from longtask.rpc.server import RequestEnvelope


# C4 修复（P1）：actor 服务端派生，不再从 params.actor 取（不变式 #2）。
# envelope.client_id 是稳定受信源（longtask-cli=用户，mcp=模型，executor=执行者）。
_TRUSTED_CLIENT_ACTORS: dict[str, str] = {
    "longtask-cli": "user",
    "cli": "user",  # 本地开发/测试客户端别名
    "cli-test": "user",  # 测试夹具使用的固定客户端 ID
    "mcp": "model",
    "executor": "executor",
    "verifier": "verifier",
    "daemon": "daemon",
    "daemon-wakeup": "daemon",
    "system": "system",
}


def _budget_int(value: Any, field: str) -> int:
    """Parse a budget integer while rejecting bool-as-int coercion."""
    if isinstance(value, (bool, float)):
        raise TypeError(f"budget.{field} must be an integer")
    return int(value)


def _workload_float(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("workload_estimate.initial_hours must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise TypeError("workload_estimate.initial_hours must be a finite number")
    return parsed


def resolve_actor(envelope: RequestEnvelope, params: dict[str, Any]) -> str:
    """从 envelope 派生服务端可信 actor（C4 修复）。

    params["actor"] 仅作审计标签追加（不再覆盖派生值），避免模型端塞
    actor=user 冒充用户。
    """
    actor = _TRUSTED_CLIENT_ACTORS.get(envelope.client_id)
    if actor is None:
        raise RpcError(
            code=ErrorCode.AUTH_FAILED,
            message=f"unknown client_id: {envelope.client_id}",
        )
    return actor


def require_principal(envelope: RequestEnvelope, params: dict[str, Any], *, action: str) -> str:
    """用户专属操作的 actor 门禁（SPEC §4.2：自主执行前 MUST 有 Principal 批准）。

    approve/pause/resume/cancel/arbitrate/goal 推进是 Principal 的决定权，
    模型客户端（actor="model"）不得自批准或代行——MCP 的 destructiveHint
    注解只是提示、host 可忽略，强制点必须在服务端。
    """
    actor = resolve_actor(envelope, params)
    if actor != "user":
        raise RpcError(
            code=ErrorCode.AUTH_FAILED,
            message=(
                f"{action} requires the user (Principal); "
                "model clients must ask the user to run it via the CLI"
            ),
        )
    return actor


def parse_contract_draft(
    params: dict[str, Any],
    envelope: RequestEnvelope | None = None,
) -> ContractDraft:
    """从请求入参解析并校验 ContractDraft（DESIGN §4、§11.6）。

    4th-round review (2026-09-08): ``contract/prepare`` builds the
    Acceptance via the legacy namespace helper, which used to
    silently drop the structured ``spec`` and ``spec_hash``
    fields.  Spec dispatch and plan-gate would then bind to a
    null spec.  The ``lhgp`` namespace helper was already fixed
    in the 3rd round; the ``longtask`` namespace is the actual
    call site for the contract RPC handler, so it gets the same
    fix here.
    """
    draft_data: dict[str, Any] = params.get("draft", params)
    try:
        title = str(draft_data["title"])
        objective = str(draft_data["objective"])
        raw_deadline = draft_data["deadline_at"]
        if hasattr(raw_deadline, "tzinfo"):  # datetime-like
            deadline_at = raw_deadline
        else:
            deadline_at = datetime.fromisoformat(str(raw_deadline))

        hard_constraints = dict(draft_data["hard_constraints"])
        acc_raw = draft_data["acceptance"]
        spec_raw = acc_raw.get("spec")
        spec_obj = spec_raw if isinstance(spec_raw, dict) else None
        spec_hash_raw = acc_raw.get("spec_hash")
        spec_hash = spec_hash_raw if isinstance(spec_hash_raw, str) and spec_hash_raw else None
        acceptance = Acceptance(
            standard=str(acc_raw["standard"]),
            checks=tuple(parse_check(c) for c in acc_raw["checks"]),
            verifier=str(acc_raw.get("verifier", "cross_check")),
            spec=spec_obj,
            spec_hash=spec_hash,
        )

        workload_estimate = draft_data.get("workload_estimate")
        if workload_estimate is not None and isinstance(workload_estimate, dict):
            workload_initial_hours = _workload_float(workload_estimate["initial_hours"])
        elif "workload_initial_hours" in draft_data:
            workload_initial_hours = _workload_float(draft_data["workload_initial_hours"])
        else:
            raise KeyError("workload_estimate.initial_hours")

        budget_raw = draft_data["budget"]
        budget = Budget(
            max_dispatches=_budget_int(budget_raw["max_dispatches"], "max_dispatches"),
            max_escalations=_budget_int(budget_raw["max_escalations"], "max_escalations"),
            max_concurrent_attempts=_budget_int(
                budget_raw["max_concurrent_attempts"], "max_concurrent_attempts"
            ),
            max_attempt_minutes=_budget_int(
                budget_raw["max_attempt_minutes"], "max_attempt_minutes"
            ),
            max_output_bytes=_budget_int(budget_raw["max_output_bytes"], "max_output_bytes"),
            verification_attempts_reserved=_budget_int(
                budget_raw.get("verification_attempts_reserved", DEFAULT_VERIFICATION_RESERVED),
                "verification_attempts_reserved",
            ),
        )

        soft_guidance = dict(draft_data.get("soft_guidance", {}))
        context = dict(draft_data.get("context", {}))
        execution = dict(draft_data.get("execution", {}))
        client_meta = dict(draft_data.get("client_meta", {}))
        authority = authority_from_dict(draft_data.get("authority"))
        attention = attention_from_dict(draft_data.get("attention"))
        continuity = continuity_from_dict(draft_data.get("continuity"))
        # 5th-round follow-up: a model caller (client_id="mcp")
        # cannot pre-authorize its own contract.  The trusted
        # source is the bound Goal's ``plan.pre_authorized``
        # (Principal-pinned at goal/update time); the model's
        # per-contract claim is stripped at the boundary so a
        # spoofed envelope cannot escalate the scope.
        caller_client_id = (
            str(getattr(envelope, "client_id", "") or "") if envelope is not None else ""
        )
        if caller_client_id == "mcp":
            auto_approve = AutoApprove()
        else:
            auto_approve = auto_approve_from_dict(draft_data.get("auto_approve"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"malformed contract draft parameter: {exc}",
        ) from exc

    draft = ContractDraft(
        title=title,
        objective=objective,
        deadline_at=deadline_at,
        hard_constraints=hard_constraints,
        acceptance=acceptance,
        workload_initial_hours=workload_initial_hours,
        budget=budget,
        soft_guidance=soft_guidance,
        context=context,
        execution=execution,
        client_meta=client_meta,
        authority=authority,
        attention=attention,
        continuity=continuity,
        auto_approve=auto_approve,
    )
    errors = draft.validate()
    if errors:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="; ".join(errors),
            details={"errors": errors},
        )
    return draft


# 合同 ID 的安全 slug（projections/contract_dir 直接以它拼目录：任何
# 路径分隔符/盘符/../ 都会写出数据根之外——安全审查 RPC-C2）。
# 与 canonical lhgp.rpc.handlers._common 保持一致——该不变量由
# tests/unit/test_handler_common_parity.py 强制，不靠注释维持（审计 B2：
# idempotent_replay 的归属守卫就曾只长在本文件、canonical 侧缺失）。
_CONTRACT_ID_RE = re.compile(r"^[0-9a-zA-Z][0-9a-zA-Z_.-]*\Z")


def _is_safe_contract_id(contract_id: str) -> bool:
    if not _CONTRACT_ID_RE.match(contract_id):
        return False
    return contract_id not in (".", "..") and ".." not in contract_id.split("-")


def require_contract_id(params: dict[str, Any]) -> str:
    """入参 contract_id 必填、非空且为安全 slug（防路径穿越）。

    非字符串（如数字 123）直接 VALIDATION_FAILED，不静默 str() 化——
    类型错误就该报类型错误（工具面审计 O6）。
    """
    raw = params.get("contract_id")
    if raw is not None and not isinstance(raw, str):
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="contract_id must be a string")
    contract_id = str(raw or "").strip()
    if not contract_id:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="contract_id is required")
    if not _is_safe_contract_id(contract_id):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                f"contract_id '{contract_id}' must be a path-free slug "
                "(letters, digits, '_', '-', '.'; no separators, no '..')"
            ),
        )
    return contract_id


def idempotent_replay(
    conn: sqlite3.Connection,
    envelope: RequestEnvelope,
    contract_id: str,
) -> dict[str, Any] | None:
    """幂等重放快速返回（DESIGN §11.3）。

    若 envelope.request_id 已在 events 表里有事件，直接返回当前合同视图，
    不重复执行副作用。返回 None 表示这不是重放，调用方继续正常路径。
    归属校验（审计持久化-R1）：已有事件的 contract_id 与本次操作的
    contract_id 不一致时不再静默吞掉合法写入——跨合同复用 request_id
    属于客户端错误，抛 IdempotencyMismatchError。
    """
    if not envelope.request_id:
        return None
    existing_events = get_events_by_request_id(conn, envelope.request_id)
    if not existing_events:
        return None
    event_contract_ids = {e.contract_id for e in existing_events if e.contract_id}
    if event_contract_ids and contract_id not in event_contract_ids:
        # 跨合同复用 request_id 是客户端错误，不能静默吞掉本次写入
        # （审计持久化-R1）。
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                f"request_id {envelope.request_id!r} belongs to contract(s) "
                f"{sorted(event_contract_ids)}, not {contract_id!r}"
            ),
        )
    existing = get_contract(conn, contract_id)
    if existing is None:
        return None
    return {"ok": True, "result": existing.to_dict()}


def _parse_iso(s: str) -> datetime:
    """从 ISO 字符串解析为带 tz 的 datetime（公开助手，方便测试）。"""
    return datetime.fromisoformat(s)


__all__ = [
    "_parse_iso",
    "idempotent_replay",
    "parse_contract_draft",
    "require_contract_id",
    "require_principal",
    "resolve_actor",
]
