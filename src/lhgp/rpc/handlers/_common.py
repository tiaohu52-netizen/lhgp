"""Canonical shared RPC handler guards.

Authentication and identifier validation live here so canonical handlers do not
need to import the legacy package.  Contract parsing and replay lookup remain
temporarily delegated until their persistence dependencies are migrated.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lhgp.acceptance.checks import parse_check
from lhgp.contracts.attention import from_dict as attention_from_dict
from lhgp.contracts.authority import from_dict as authority_from_dict
from lhgp.contracts.budget import DEFAULT_VERIFICATION_RESERVED
from lhgp.contracts.continuity import from_dict as continuity_from_dict
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.store import get_contract, get_events_by_request_id
from lhgp.rpc.errors import ErrorCode, RpcError

if TYPE_CHECKING:
    from lhgp.rpc.server import RequestEnvelope

_TRUSTED_CLIENT_ACTORS: dict[str, str] = {
    "longtask-cli": "user",
    "cli": "user",
    "cli-test": "user",
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


def _parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp for handler-side input normalization."""
    return datetime.fromisoformat(value)


def resolve_actor(envelope: RequestEnvelope, params: dict[str, Any]) -> str:
    """从受信 client_id 派生 actor，拒绝未知客户端。"""
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


# 合同 ID 的安全 slug（projections/contract_dir 直接以它拼目录：任何
# 路径分隔符/盘符/../ 都会写出数据根之外——安全审查 RPC-C2）。
# 不强制 lt- 前缀：goal/prepare 的阶段合同沿用自定义 ID（如 stage 合同），
# 防线是「无路径字符」而非命名风格。
_CONTRACT_ID_RE = re.compile(r"^[0-9a-zA-Z][0-9a-zA-Z_.-]*$")


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


def parse_contract_draft(params: dict[str, Any]) -> ContractDraft:
    """解析并验证合同草稿，统一 canonical contracts 组件。

    3rd-round review (2026-09-08): 验收字段新增的 ``spec`` 和
    ``spec_hash`` 必须与 ``standard``/``checks``/``verifier`` 一同
    透传；之前手工构造 ``Acceptance`` 把这两个字段静默丢掉了，
    下游 spec dispatch 和 plan gate 的 spec_hash 绑定都会因为
    spec=None 而绕过全部新行为。
    """
    draft_data: dict[str, Any] = params.get("draft", params)
    try:
        title = str(draft_data["title"])
        objective = str(draft_data["objective"])
        raw_deadline = draft_data["deadline_at"]
        deadline_at = (
            raw_deadline
            if hasattr(raw_deadline, "tzinfo")
            else datetime.fromisoformat(str(raw_deadline))
        )
        acc_raw = draft_data["acceptance"]
        spec_raw = acc_raw.get("spec")
        spec_obj = spec_raw if isinstance(spec_raw, dict) else None
        spec_hash_raw = acc_raw.get("spec_hash")
        spec_hash = spec_hash_raw if isinstance(spec_hash_raw, str) and spec_hash_raw else None
        acceptance = Acceptance(
            standard=str(acc_raw["standard"]),
            checks=tuple(parse_check(item) for item in acc_raw["checks"]),
            verifier=str(acc_raw.get("verifier", "cross_check")),
            spec=spec_obj,
            spec_hash=spec_hash,
        )
        workload_estimate = draft_data.get("workload_estimate")
        if isinstance(workload_estimate, dict):
            workload_initial_hours = _workload_float(workload_estimate["initial_hours"])
        else:
            workload_initial_hours = _workload_float(draft_data["workload_initial_hours"])
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
        draft = ContractDraft(
            title=title,
            objective=objective,
            deadline_at=deadline_at,
            hard_constraints=dict(draft_data["hard_constraints"]),
            acceptance=acceptance,
            workload_initial_hours=workload_initial_hours,
            budget=budget,
            soft_guidance=dict(draft_data.get("soft_guidance", {})),
            context=dict(draft_data.get("context", {})),
            execution=dict(draft_data.get("execution", {})),
            client_meta=dict(draft_data.get("client_meta", {})),
            authority=authority_from_dict(draft_data.get("authority")),
            attention=attention_from_dict(draft_data.get("attention")),
            continuity=continuity_from_dict(draft_data.get("continuity")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"malformed contract draft parameter: {exc}",
        ) from exc
    errors = draft.validate()
    if errors:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="; ".join(errors),
            details={"errors": errors},
        )
    return draft


def idempotent_replay(
    conn: sqlite3.Connection,
    envelope: RequestEnvelope,
    contract_id: str,
) -> dict[str, Any] | None:
    """检测 request_id 重放并返回已有合同快照，不重复执行副作用。"""
    if not envelope.request_id or not get_events_by_request_id(conn, envelope.request_id):
        return None
    existing = get_contract(conn, contract_id)
    return {"ok": True, "result": existing.to_dict()} if existing is not None else None


__all__ = [
    "_parse_iso",
    "idempotent_replay",
    "parse_contract_draft",
    "require_contract_id",
    "require_principal",
    "resolve_actor",
]
