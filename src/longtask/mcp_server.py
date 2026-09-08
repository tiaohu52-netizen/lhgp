"""MCP server 薄层（DESIGN §11.1、§17）：让任何 MCP 兼容的 agent harness 能
「发现并使用」longtask 协议。

设计选择：暴露一组**面向 AI 任务流**的工具（不是把底层 JSON-RPC 方法
一对一透传——那只是 RPC 隧道，不是 AI 接口）。工具覆盖探活、诊断、目标/合同
生命周期、执行器接力、验收请求、审计与控制，模型可以独立走完
"立合同→批准→交付→验收"四步；规范命名与兼容命名双轨并存：

- longtask_health: 协议探活 + 返回可用工具清单（mcp discoverable 钩子）
- longtask_list_executors: 选哪个执行器
- longtask_prepare_contract: 立合同（填 objective / acceptance.checks /
  hard_constraints / deadline —— 详见 skills/longtask-contract/SKILL.md）
- longtask_approve_contract: 批准进入调度
- longtask_get_contract / longtask_list_contracts: 跟踪状态
- longtask_attach_to_executor: 模型作为执行者被协议拉起时，认领
  attempt 并写回（attempt/status + attempt/write-back 包装；lease/renew
  留接口未自动包，由执行者侧 RPC 单独调用以保留心跳节流）

传输：stdio JSON-RPC 2.0 文本协议（line-delimited JSON，标准 MCP
transport）。零新增三方依赖：标准库 json + asyncio（readline 阻塞模式
更轻，AI 工具链路用 stream 模式不必要）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from longtask import PROTOCOL_VERSION, __version__
from longtask.adapters.registry import ExecutorRegistry
from longtask.cli.doctor import run_doctor
from longtask.cli.paths import default_data_root
from longtask.persistence.notifications import list_notifications
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.methods import Method
from longtask.rpc.server import parse_envelope, route


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _mcp_request_id(method: Method, args: dict[str, Any]) -> str:
    """Return an explicit or stable derived request key for MCP retries.

    MCP tool calls do not expose the transport request id to the wrapped RPC
    handler.  A canonical digest keeps an omitted-id retry idempotent while an
    explicit key still lets callers intentionally distinguish identical calls.
    """
    explicit = args.get("request_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    payload = {key: value for key, value in args.items() if key != "request_id"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"mcp:{method.value}:{digest}"


# ─── MCP 工具：每个工具对应一个 JSON-RPC method + 入参映射 ─────────────


def tool_health(_args: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "implementation_version": __version__,
        "status": "ok",
        "tools": TOOL_NAMES,
        "tool_count": len(TOOL_NAMES),
    }


def tool_doctor(_args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Run local preflight diagnostics without changing contract state."""
    report = run_doctor(ctx["root"])
    return {
        "all_ok": report.all_ok,
        "protocol_version": report.protocol_version,
        "package_version": report.package_version,
        "checks": [
            {"name": check.name, "ok": check.ok, "message": check.message, "details": check.details}
            for check in report.checks
        ],
    }


def tool_list_executors(_args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """可用的执行器池（用户框定后）：模型先看这个再选执行器。"""
    reg = ctx["registry"]
    raw_enabled_only = _args.get("enabled_only", False)
    if not isinstance(raw_enabled_only, bool):
        raise ValueError("enabled_only must be a boolean")
    enabled_only = raw_enabled_only
    entries = reg.list_entries(enabled_only=enabled_only)
    return {
        "executors": [
            {
                "id": e.id,
                "kind": e.kind,
                "enabled": e.enabled,
                "capabilities": e.capabilities.to_dict()
                if hasattr(e.capabilities, "to_dict")
                else {},
                "cost_hint": str(
                    e.cost_hint.value if hasattr(e.cost_hint, "value") else e.cost_hint
                ),
            }
            for e in entries
        ]
    }


def _validate_checks_argument(checks: Any) -> Any:
    """字符串验收检查在 MCP 边界 fail-closed（工具面审计 C1）。

    parse_check 对纯字符串原样放行；若把整个字符串当 list 交给
    tuple(parse_check(c) for c in ...)，"file-exists" 会变成 11 个单字符
    垃圾检查冻进合同。schema 虽声明 array，dispatch 层不校验——在边界
    显式拒绝，错误信息直接告诉模型正确的传法。
    """
    if isinstance(checks, str):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                "acceptance_checks must be an array of strings; got a single "
                f'string. Pass ["{checks}"] instead.'
            ),
        )
    if not isinstance(checks, list):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="acceptance_checks must be an array of strings",
        )
    return checks


def tool_prepare_contract(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """立远期合同。详见 skills/longtask-contract/SKILL.md §4。"""
    payload: dict[str, Any] = {
        "title": args["title"],
        "objective": args["objective"],
        "deadline_at": args["deadline_at"],
        "hard_constraints": args.get("hard_constraints", {}),
        "acceptance": {
            "standard": args["acceptance_standard"],
            "checks": _validate_checks_argument(args["acceptance_checks"]),
        },
        # Preserve the model value for the single runtime validator; coercing
        # here would turn true/false into 1.0/0.0 and bypass fail-closed input
        # checks.
        "workload_estimate": {"initial_hours": args.get("workload_initial_hours", 1.0)},
        "budget": args.get(
            "budget",
            {
                "max_dispatches": 5,
                "max_escalations": 1,
                "max_concurrent_attempts": 1,
                "max_attempt_minutes": 60,
                "max_output_bytes": 1048576,
            },
        ),
        "authority": args.get("authority", {}),
        "attention": args.get("attention", {}),
        "continuity": args.get("continuity", {}),
        "auto_approve": args.get("auto_approve", {}),
        "context": args.get("context", {}),
        "execution": args.get("execution", {}),
        "client_meta": args.get("client_meta", {}),
    }
    params: dict[str, Any] = {"draft": payload}
    # contract_id / goal_id 提到 envelope params（与 handle_contract_prepare 的入参对齐）
    if args.get("contract_id"):
        params["contract_id"] = args["contract_id"]
    if args.get("goal_id"):
        params["goal_id"] = args["goal_id"]
    envelope = parse_envelope(
        {
            "method": Method.CONTRACT_PREPARE.value,
            "request_id": _mcp_request_id(Method.CONTRACT_PREPARE, args),
            # MCP is a model-controlled boundary; never let tool arguments
            # override the trusted client identity used for actor derivation.
            "client_id": "mcp",
            "protocol_version": PROTOCOL_VERSION,
            "params": params,
        }
    )
    return route(envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"])


def tool_user_confirm_spec_verdict(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """User confirms a CANDIDATE spec verdict (Principal-gated).

    A contract whose acceptance spec has a ``judge == "user"``
    criterion transitions to ``CANDIDATE`` after a verifier pass.
    The dispatcher deliberately skips CANDIDATE contracts so the
    user has the final say.  This tool is the only path that
    moves the contract to ``PASSED``.

    4th-round review (2026-09-08): the Principal gate lives in
    :func:`handle_contract_user_confirm`, not here.  The previous
    wrapper-level gate was bypassable: when ``ctx["envelope"]`` was
    absent (the default MCP stdio context for model clients), the
    ``if caller_envelope is not None`` branch silently skipped
    the check, the synthetic envelope was built with
    ``client_id="mcp"``, and the handler recorded ``actor=model``.
    The handler now runs ``require_principal`` on the routed
    envelope regardless of wrapper-side state, so the only
    authoritative gate is the one inside the handler.

    A typical model-caller workflow:
    1. ``contract.get`` to see ``acceptance_status``.
    2. Notice ``CANDIDATE`` and the spec's user criterion.
    3. Ask the user to review the artifacts.
    4. The user runs ``lhgp contract user-confirm <contract_id>``
       (or calls this tool from a user-class MCP client).
    """
    caller_envelope = ctx.get("envelope")
    caller_client_id = caller_envelope.client_id if caller_envelope is not None else "mcp"
    envelope = parse_envelope(
        {
            "method": Method.CONTRACT_USER_CONFIRM.value,
            "request_id": _mcp_request_id(Method.CONTRACT_USER_CONFIRM, args),
            "client_id": caller_client_id,
            "protocol_version": PROTOCOL_VERSION,
            "params": {
                "contract_id": args["contract_id"],
                "note": args.get("note"),
            },
        }
    )
    return route(envelope, conn=ctx["conn"], now=_now(), registry=ctx.get("registry"))


def tool_approve_contract(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    envelope = parse_envelope(
        {
            "method": Method.CONTRACT_APPROVE.value,
            "request_id": _mcp_request_id(Method.CONTRACT_APPROVE, args),
            "client_id": "mcp",
            "protocol_version": PROTOCOL_VERSION,
            "params": {
                "contract_id": args["contract_id"],
                "expected_revision": args.get("revision"),
            },
        }
    )
    return route(envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"])


def tool_request_verification(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """用户触发验收（§12.4）：只派 verifier，不派执行者。"""
    envelope = parse_envelope(
        {
            "method": Method.CONTRACT_REQUEST_VERIFICATION.value,
            "request_id": _mcp_request_id(Method.CONTRACT_REQUEST_VERIFICATION, args),
            "client_id": "mcp",
            "protocol_version": PROTOCOL_VERSION,
            "params": {"contract_id": args["contract_id"]},
        }
    )
    return route(envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"])


def tool_get_contract(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    envelope = parse_envelope(
        {
            "method": Method.CONTRACT_GET.value,
            "request_id": _mcp_request_id(Method.CONTRACT_GET, args),
            "client_id": "mcp",
            "protocol_version": PROTOCOL_VERSION,
            "params": {
                "contract_id": args["contract_id"],
                "decision_limit": args.get("decision_limit", 50),
                "attempt_limit": args.get("attempt_limit", 20),
            },
        }
    )
    return route(envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"])


def tool_list_contracts(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    envelope = parse_envelope(
        {
            "method": Method.CONTRACT_LIST.value,
            "request_id": _mcp_request_id(Method.CONTRACT_LIST, args),
            "client_id": "mcp",
            "protocol_version": PROTOCOL_VERSION,
            "params": {
                "state": args.get("state"),
                "limit": args.get("limit", 20),
            },
        }
    )
    return route(envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"])


def tool_prepare_goal(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """goal/prepare：立合同并返回 7 类 admission offer（§6.3）。

    与 prepare_contract 的区别：可关联 goal_id / stage_id（激活
    advance_goal 与 goal_contract_draft 的前提），响应含 admission
    offer（解释为什么被允许/拒接）。
    """
    draft = {
        "title": args["title"],
        "objective": args["objective"],
        "deadline_at": args["deadline_at"],
        "hard_constraints": args.get("hard_constraints", {}),
        "acceptance": {
            "standard": args["acceptance_standard"],
            "checks": _validate_checks_argument(args["acceptance_checks"]),
            # goal/prepare 的 validate_draft 不默认 verifier（与 contract
            # 路径不同），工具面补齐同一默认，避免最小载荷被拒。
            "verifier": args.get("acceptance_verifier", "cross_check"),
        },
        "workload_estimate": {"initial_hours": args.get("workload_initial_hours", 1.0)},
        "budget": args.get(
            "budget",
            {
                "max_dispatches": 5,
                "max_escalations": 1,
                "max_concurrent_attempts": 1,
                "max_attempt_minutes": 60,
                "max_output_bytes": 1048576,
            },
        ),
        "authority": args.get("authority", {}),
        "attention": args.get("attention", {}),
        "continuity": args.get("continuity", {}),
        "auto_approve": args.get("auto_approve", {}),
        "context": args.get("context", {}),
        "execution": args.get("execution", {}),
        "client_meta": args.get("client_meta", {}),
    }
    params: dict[str, Any] = {"draft": draft}
    if args.get("contract_id"):
        params["contract_id"] = args["contract_id"]
    if args.get("goal_id"):
        params["goal_id"] = args["goal_id"]
    if args.get("stage_id"):
        params["stage_id"] = args["stage_id"]
    envelope = parse_envelope(
        {
            "method": Method.GOAL_PREPARE.value,
            "request_id": _mcp_request_id(Method.GOAL_PREPARE, args),
            "client_id": "mcp",
            "protocol_version": PROTOCOL_VERSION,
            "params": params,
        }
    )
    return route(envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"])


def tool_brief(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """接手包：一份合同的状态/风险/预算/最近失败/下一步（只读聚合）。"""
    from lhgp.persistence.insights import build_brief

    return build_brief(ctx["conn"], contract_id=args["contract_id"], now=_now())


def tool_resume_attempt(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """读取 active.md + handover.md，拼出一次可喂入新 LLM 会话的 self-contained brief。

    会落 attempt/resumed 审计事件（EventType.ATTEMPT_RESUMED）。
    返回的 body 字段直接给模型当上下文。

    P1 review fix: the audit event's actor is the actual MCP caller
    (``agent:mcp``) — not the hard-coded ``"user"`` that the resume
    helper used to default to.  Forensics distinguishes a real user
    keystroke from an agent-initiated resume.
    """
    from lhgp.contracts import build_resume_brief

    brief = build_resume_brief(
        ctx["root"],
        args["contract_id"],
        args["attempt_id"],
        next_attempt_id=args.get("next_attempt_id"),
        conn=ctx["conn"],
        now=_now(),
        actor="agent:mcp",
    )
    return {
        "contract_id": brief.contract_id,
        "attempt_id": brief.attempt_id,
        "next_attempt_id": brief.next_attempt_id,
        "active_md_path": str(brief.active_md_path),
        "handover_md_path": str(brief.handover_md_path),
        "body": brief.body,
    }


def tool_board(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """多合同一屏：风险/状态/预算/下次决策点，按风险排序（只读聚合）。"""
    from lhgp.persistence.insights import build_board

    rows = build_board(
        ctx["conn"],
        now=_now(),
        include_terminal=bool(args.get("include_terminal", False)),
        limit=int(args.get("limit", 200)),
    )
    return {"board": rows, "count": len(rows)}


def tool_stats(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """成本台账：attempt 按角色/执行器/状态分布与实际墙钟时长（只读）。"""
    from lhgp.persistence.insights import build_stats

    return build_stats(ctx["conn"], contract_id=args.get("contract_id"))


def tool_propose_plan(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """提交 Goal 计划修订提案（ADR-004 规则 6）：只落 goal/proposed 事件，"""
    """不写权威状态；用户批准后由用户经 CLI 执行 goal/update。"""
    from longtask.persistence.events import EventType
    from longtask.persistence.store import append_event, get_goal

    goal_id = args["goal_id"]
    goal = get_goal(ctx["conn"], goal_id)
    if goal is None:
        from longtask.rpc.errors import ErrorCode, RpcError

        raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message=f"goal {goal_id} not found")
    # E3 校验：提案结构必须通过 validate_proposed_plan（含权限字段禁止）
    from lhgp.promoter.proposals import validate_proposed_plan

    plan = args.get("plan")
    validation = validate_proposed_plan(plan)
    if not validation.ok:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="; ".join(validation.errors),
        )
    proposal = {
        "goal_id": goal_id,
        "proposed_by": "model",
        "reason": str(args.get("reason", "")),
        "plan": args.get("plan"),
        "note": str(args.get("note", "")),
        "status": "pending",
    }
    event = append_event(
        ctx["conn"],
        contract_id=goal_id,
        goal_id=goal_id,
        event_type=EventType.GOAL_PROPOSED,
        payload=proposal,
        now=_now(),
        actor="model",
    )
    return {
        "ok": True,
        "proposal_event_id": event.event_id,
        "status": "pending",
        "hint": "user applies via: lhgp goal update <goal_id> --revision N (plan from proposal)",
    }


def tool_send_message(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """发送结构化消息（directive/context/question）。"""
    from lhgp.persistence.messages import send_message

    event_id = send_message(
        ctx["conn"],
        contract_id=args["contract_id"],
        from_actor="model",
        kind=args.get("kind", "context"),
        text=args["text"],
        now=_now(),
        to_agent=args.get("to"),
    )
    return {"ok": True, "event_id": event_id}


def tool_inbox(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """读取合同上的消息（directive/question/context）。"""
    from lhgp.persistence.messages import get_messages

    msgs = get_messages(ctx["conn"], contract_id=args["contract_id"])
    return {"messages": msgs, "count": len(msgs)}


def tool_get_goal(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Read a stable Goal aggregate, independent of a contract revision."""
    return _mcp_route(Method.GOAL_GET, args, ctx)


def tool_list_goals(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """List stable Goal aggregates."""
    return _mcp_route(Method.GOAL_LIST, args, ctx)


def tool_update_goal(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """CAS-update a Goal's long-lived plan or progress."""
    return _mcp_route(Method.GOAL_UPDATE, args, ctx)


def tool_advance_goal(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Complete the current Goal stage with revision CAS."""
    return _mcp_route(Method.GOAL_ADVANCE, args, ctx)


def tool_next_goal_action(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Get the next safe model action for a Goal."""
    return _mcp_route(Method.GOAL_NEXT, args, ctx)


def tool_goal_contract_draft(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Build a reviewable contract draft from a Goal stage."""
    return _mcp_route(Method.GOAL_CONTRACT_DRAFT, args, ctx)


def _mcp_route(method: Method, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """为控制类工具构造统一的模型侧 RPC envelope。"""
    return route(
        parse_envelope(
            {
                "method": method.value,
                "request_id": _mcp_request_id(method, args),
                "client_id": "mcp",
                "protocol_version": PROTOCOL_VERSION,
                "params": dict(args),
            }
        ),
        conn=ctx["conn"],
        now=_now(),
        registry=ctx["registry"],
    )


def tool_attempt_status(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """读取执行 attempt 的事件、租约和当前状态。"""
    return _mcp_route(Method.ATTEMPT_STATUS, args, ctx)


def tool_notifications(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """只读通知队列状态；默认不返回 payload 内容。"""
    status = args.get("status")
    goal_id = args.get("goal_id")
    if status is not None and not isinstance(status, str):
        raise ValueError("status must be a string")
    if goal_id is not None and not isinstance(goal_id, str):
        raise ValueError("goal_id must be a string")
    raw_limit = args.get("limit", 50)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise ValueError("limit must be an integer")
    limit = raw_limit
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    raw_include_payload = args.get("include_payload", False)
    if not isinstance(raw_include_payload, bool):
        raise ValueError("include_payload must be a boolean")
    include_payload = raw_include_payload
    rows = list_notifications(
        ctx["conn"],
        status=status or None,
        goal_id=goal_id or None,
        limit=limit,
    )
    return {
        "notifications": [
            {
                "notification_id": item.notification_id,
                "idempotency_key": item.idempotency_key,
                "goal_id": item.goal_id,
                "event_type": item.event_type,
                "channel": item.channel,
                "status": item.status,
                "attempts": item.attempts,
                "available_at": item.available_at.isoformat(),
                "last_error": item.last_error,
                **({"payload": item.payload} if include_payload else {}),
            }
            for item in rows
        ]
    }


def tool_interrupt_attempt(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """排队中断执行中的 attempt，由 daemon 兑现实际取消。"""
    return _mcp_route(Method.CONTROL_INTERRUPT, args, ctx)


# ─── P6 feedback / learning / portfolio / enforcement tools ───────────────


def tool_submit_evaluation(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Record a user evaluation for a finished contract.

    Inserts into user_evaluations, appends a user/evaluation-submitted
    event via the canonical event log, and returns the assigned
    evaluation_id. The user's rating (1-5) + verdict (accept/partial/
    reject) is the primary input to lhgp.learning.extract_template_signals.
    """
    from datetime import UTC, datetime

    from lhgp.feedback import (
        EvaluationRating,
        EvaluationVerdict,
        UserEvaluation,
        record_evaluation,
    )
    from lhgp.persistence.events import EventType
    from lhgp.persistence.events_query import append_event

    contract_id = str(args.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("contract_id is required")
    rating = int(args.get("rating") or 0)
    if not 1 <= rating <= 5:
        raise ValueError("rating must be 1..5")
    verdict = EvaluationVerdict(str(args.get("verdict") or "").strip())
    contract_revision = int(args.get("contract_revision") or 1)
    evaluation = UserEvaluation(
        contract_id=contract_id,
        contract_revision=contract_revision,
        attempt_id=args.get("attempt_id"),
        evaluator=str(args.get("evaluator") or "user"),
        rating=EvaluationRating(str(rating)),
        verdict=verdict,
        comments=str(args.get("comments") or ""),
    )
    conn = ctx["conn"]
    with _mcp_store_transaction(conn) as c:
        evaluation_id = record_evaluation(c, evaluation)
        append_event(
            c,
            contract_id=contract_id,
            event_type=EventType.USER_EVALUATION_SUBMITTED,
            payload={
                "evaluation_id": evaluation_id,
                "rating": int(rating),
                "verdict": verdict.value,
            },
            now=datetime.now(UTC),
            contract_revision=contract_revision,
            attempt_id=evaluation.attempt_id,
            actor=evaluation.evaluator,
        )
    return {
        "evaluation_id": evaluation_id,
        "contract_id": contract_id,
        "contract_revision": contract_revision,
        "rating": int(rating),
        "verdict": verdict.value,
        "created_at": datetime.now(UTC).isoformat(),
    }


def tool_submit_plan(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Submit a Plan for a contract attempt and validate it against the gate.

    Always writes a ``PLAN_SUBMITTED`` event for audit. If the validator
    returns ``approved=True``, also writes a ``PLAN_APPROVED`` event so
    the runner can dispatch the attempt; otherwise writes a
    ``PLAN_REJECTED`` event with the rejection reasons in the payload.
    The runner requires a matching ``PLAN_APPROVED`` in the contract's
    recent event stream before it can transition ``DISPATCHING -> RUNNING``.
    """
    from datetime import UTC, datetime

    from lhgp.contracts.plan import Plan, PlanStep
    from lhgp.persistence.events import EventType
    from lhgp.persistence.events_query import append_event
    from lhgp.persistence.store import get_contract

    contract_id = str(args.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("contract_id is required")
    steps_raw = args.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("steps must be a non-empty array")
    submitted_by = str(args.get("submitted_by") or "agent:mcp").strip()

    # Capture the current accepted check IDs and revision so the runner
    # can reject a stale PLAN_APPROVED after acceptance criteria change
    # or after the contract gets a new revision.
    from lhgp.contracts.plan import _extract_check_identifiers

    view = get_contract(ctx["conn"], contract_id)
    if view is None:
        from longtask.rpc.errors import ErrorCode, RpcError

        raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message=f"contract {contract_id} not found")
    accepted_check_ids = list(_extract_check_identifiers(view))

    steps: list[PlanStep] = []
    for index, raw in enumerate(steps_raw, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"steps[{index}] must be an object")
        try:
            steps.append(
                PlanStep(
                    step_id=int(raw.get("step_id", index)),
                    action=str(raw.get("action") or ""),
                    target=str(raw.get("target") or ""),
                    rationale=str(raw.get("rationale") or ""),
                    expected_outcome=str(raw.get("expected_outcome") or ""),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"steps[{index}] is malformed: {exc}") from exc

    now = datetime.now(UTC)
    plan = Plan(
        contract_id=contract_id,
        steps=tuple(steps),
        submitted_at=now,
        submitted_by=submitted_by,
    )
    validation = plan.validate(view)

    conn = ctx["conn"]
    # 5th-round follow-up: the per-contract ``auto_approve`` is
    # the model's *claim*.  The trusted source is the bound
    # Goal's ``plan.pre_authorized`` (user-pinned).  Augment
    # the validation here so a plan whose steps are inside
    # the user's pre-grant is auto-approved even when the
    # contract's own auto_approve field is empty (which is
    # the case for MCP-issued contracts after the parse-time
    # strip).
    from dataclasses import replace

    from longtask.persistence.store import get_goal

    if getattr(view, "goal_id", None) and validation.approved and validation.requires_signoff:
        goal = get_goal(conn, view.goal_id)
        if isinstance(goal, dict):
            plan_obj = goal.get("plan")
            if isinstance(plan_obj, dict):
                pre_auth = plan_obj.get("pre_authorized")
                if isinstance(pre_auth, dict) and pre_auth.get("enabled"):
                    granted = {str(a) for a in (pre_auth.get("actions") or ()) if a}
                    if granted and all(step.action in granted for step in steps):
                        validation = replace(validation, requires_signoff=False)
    append_event(
        conn,
        contract_id=contract_id,
        event_type=EventType.PLAN_SUBMITTED,
        payload={
            "submitted_by": submitted_by,
            "step_count": len(steps),
            "step_ids": [s.step_id for s in steps],
        },
        now=now,
        actor=submitted_by,
    )
    if validation.approved and not validation.requires_signoff:
        # 3rd-round review: inside the contract's auto-approve
        # scope.  The runner can dispatch without an explicit
        # human sign-off.
        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.PLAN_APPROVED,
            payload={
                "submitted_by": submitted_by,
                "step_count": len(steps),
                "contract_revision": view.revision,
                "content_hash": plan.content_hash,
                "accepted_check_ids": list(accepted_check_ids),
                "spec_hash": view.draft.acceptance.spec_hash,
                "auto_approved": True,
            },
            now=now,
            actor="daemon",
        )
        # P1 review fix: a contract that was BLOCKED(NO_EXECUTOR) because
        # the gate refused the previous dispatch must be re-activated so
        # the next daemon tick re-runs _dispatch_attempt against the now-
        # valid approval. Without this, the contract would sit BLOCKED
        # until the user manually patched it.
        from longtask.cli.dispatch import wake_blocked_after_plan_approval

        wake_blocked_after_plan_approval(conn, contract_id, now)
    elif validation.approved and validation.requires_signoff:
        # 3rd-round review: structurally valid but outside the
        # contract's auto-approve scope.  Recorded as
        # PLAN_SUBMITTED (not PLAN_APPROVED) with the out-of-scope
        # actions listed; the user must call ``lhgp_plan_signoff``
        # to convert it to PLAN_APPROVED.  No wake here — the
        # contract stays BLOCKED until the sign-off lands.
        oos_actions = sorted(
            {s.action for s in steps if not view.draft.auto_approve.covers_action(s.action)}
        )
        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.PLAN_REJECTED,
            payload={
                "submitted_by": submitted_by,
                "step_count": len(steps),
                "rejection_reasons": ["requires_signoff"],
                "out_of_scope_actions": oos_actions,
                "auto_approve_enabled": view.draft.auto_approve.enabled,
            },
            now=now,
            actor="daemon",
        )
    else:
        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.PLAN_REJECTED,
            payload={
                "submitted_by": submitted_by,
                "rejection_reasons": list(validation.rejection_reasons),
            },
            now=now,
            actor="daemon",
        )

    return {
        "contract_id": contract_id,
        "approved": validation.approved,
        "requires_signoff": validation.requires_signoff,
        "rejection_reasons": list(validation.rejection_reasons),
        "step_count": len(steps),
        "submitted_at": now.isoformat(),
    }


def tool_plan_signoff(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """User explicitly signs off on an out-of-scope plan.

    3rd-round review (2026-09-08): ``lhgp_submit_plan`` auto-approves
    plans whose every step is inside the contract's ``auto_approve``
    scope.  Plans that are structurally valid but use actions
    outside that scope (or have ``auto_approve.enabled=False``) are
    recorded as ``PLAN_REJECTED`` with a ``requires_signoff`` flag
    instead.  ``lhgp_plan_signoff`` is the human-in-the-loop
    channel that promotes one such submission to a real
    ``PLAN_APPROVED`` event.

    The caller passes the same ``contract_id`` + ``steps`` they
    submitted, plus a ``signoff_by`` (free-form actor identity,
    e.g. ``user:human``) and an optional ``note`` for the audit
    trail.  The validator re-runs to confirm the plan is still
    structurally valid; if it isn't, no sign-off is recorded and
    the rejection reasons come back.  The bound plan
    (``content_hash``) is written to the approval event so
    subsequent audits can prove which exact plan the user signed.
    """
    from datetime import UTC, datetime

    from lhgp.contracts.plan import Plan, PlanStep
    from lhgp.persistence.events import EventType
    from lhgp.persistence.events_query import append_event
    from lhgp.persistence.store import get_contract
    from longtask.rpc.handlers._common import require_principal
    from longtask.rpc.server import RequestEnvelope

    # P0 fix (3rd-round review 2026-09-08): the caller MUST be a
    # Principal (user).  The previous implementation took the
    # caller's claimed ``signoff_by`` verbatim, so a model client
    # could write ``"user:human"`` and promote its own plan.
    # Server-side gate enforced here — the actor recorded on the
    # event is the resolved principal, not the caller's claim.
    envelope = ctx.get("envelope")
    if not isinstance(envelope, RequestEnvelope):
        from longtask.rpc.errors import ErrorCode, RpcError

        # 5th-round follow-up: the MCP runtime builds a
        # ``ctx`` without an ``envelope`` key (the model is
        # not in the RPC path).  The previous INTERNAL error
        # was a poor error code — the real diagnosis is
        # "this tool is Principal-only; ask the user to run
        # it via CLI".  Surface it as AUTH_FAILED with
        # guidance so a model caller knows to escalate.
        raise RpcError(
            code=ErrorCode.AUTH_FAILED,
            message=(
                "lhgp_plan_signoff is Principal-only; the MCP runtime "
                "does not carry a Principal envelope.  Ask the user "
                "to run it via the CLI: lhgp plan signoff <contract_id> "
                "<plan.json>"
            ),
        )
    principal_actor = require_principal(envelope, args, action="lhgp_plan_signoff")

    contract_id = str(args.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("contract_id is required")
    steps_raw = args.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("steps must be a non-empty array")
    # The recorded actor is the resolved principal, not the
    # caller's ``signoff_by`` claim.
    signoff_by = principal_actor
    note = str(args.get("note") or "").strip() or None

    conn = ctx["conn"]
    view = get_contract(conn, contract_id)
    if view is None:
        from longtask.rpc.errors import ErrorCode, RpcError

        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )

    steps: list[PlanStep] = []
    for index, raw in enumerate(steps_raw, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"steps[{index}] must be an object")
        try:
            steps.append(
                PlanStep(
                    step_id=int(raw.get("step_id", index)),
                    action=str(raw.get("action") or ""),
                    target=str(raw.get("target") or ""),
                    rationale=str(raw.get("rationale") or ""),
                    expected_outcome=str(raw.get("expected_outcome") or ""),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"steps[{index}] is malformed: {exc}") from exc

    now = datetime.now(UTC)
    plan = Plan(
        contract_id=contract_id,
        steps=tuple(steps),
        submitted_at=now,
        submitted_by=signoff_by,
    )
    validation = plan.validate(view)
    if not validation.approved:
        return {
            "contract_id": contract_id,
            "signed_off": False,
            "rejection_reasons": list(validation.rejection_reasons),
            "step_count": len(steps),
        }

    from lhgp.contracts.plan import _extract_check_identifiers

    accepted_check_ids = list(_extract_check_identifiers(view))
    append_event(
        conn,
        contract_id=contract_id,
        event_type=EventType.PLAN_APPROVED,
        payload={
            "signed_off_by": signoff_by,
            "note": note,
            "step_count": len(steps),
            "contract_revision": view.revision,
            "content_hash": plan.content_hash,
            "accepted_check_ids": accepted_check_ids,
            "spec_hash": view.draft.acceptance.spec_hash,
            "auto_approved": False,
        },
        now=now,
        actor=signoff_by,
    )
    from longtask.cli.dispatch import wake_blocked_after_plan_approval

    wake_blocked_after_plan_approval(conn, contract_id, now)
    return {
        "contract_id": contract_id,
        "signed_off": True,
        "step_count": len(steps),
        "signed_off_by": signoff_by,
        "signed_off_at": now.isoformat(),
        "content_hash": plan.content_hash,
    }


def tool_compute_diff(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Compute the file diff between an attempt and the current workspace."""
    from datetime import UTC, datetime
    from pathlib import Path

    from lhgp.feedback import compute_acceptance_diff, get_latest_diff, record_diff
    from lhgp.persistence.events import EventType
    from lhgp.persistence.events_query import append_event

    contract_id = str(args.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("contract_id is required")
    contract_revision = int(args.get("contract_revision") or 1)
    attempt_id = args.get("attempt_id")
    workspace_arg = args.get("workspace")
    root = ctx["root"]
    workspace = Path(workspace_arg) if workspace_arg else root / "contracts" / contract_id
    workspace.mkdir(parents=True, exist_ok=True)

    prior = get_latest_diff(ctx["conn"], contract_id, contract_revision)
    diff = compute_acceptance_diff(
        contract_id,
        contract_revision,
        workspace,
        before=(prior.snapshot_before if prior else None),
        attempt_id=attempt_id,
    )
    conn = ctx["conn"]
    with _mcp_store_transaction(conn) as c:
        diff_id = record_diff(c, diff)
        append_event(
            c,
            contract_id=contract_id,
            event_type=EventType.ACCEPTANCE_DIFF_COMPUTED,
            payload={
                "diff_id": diff_id,
                "files_changed_count": len(diff.files_changed),
                "summary": diff.summary,
            },
            now=datetime.now(UTC),
            contract_revision=contract_revision,
            attempt_id=attempt_id,
        )
    return {
        "diff_id": diff_id,
        "summary": diff.summary,
        "files_changed": diff.files_changed,
        "snapshot_before_count": len(diff.snapshot_before.get("files", [])),
        "snapshot_after_count": len(diff.snapshot_after.get("files", [])),
        "computed_at": datetime.now(UTC).isoformat(),
    }


def tool_evolve_templates(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Mine high-quality contracts and materialise new auto-* template files."""
    from lhgp.feedback.types import EvaluationRating
    from lhgp.learning import TemplateEvolver

    min_rating = int(args.get("min_rating") or 4)
    quality_threshold = float(args.get("quality_threshold") or 0.7)
    limit = int(args.get("limit") or 50)
    evolver = TemplateEvolver(
        min_rating=EvaluationRating(str(min(min_rating, 5))),
        quality_threshold=quality_threshold,
    )
    result = evolver.run(ctx["conn"], limit=limit)
    return {
        "written": [str(p) for p in result.written],
        "skipped_low_quality": result.skipped_low_quality,
        "skipped_existing": result.skipped_existing,
    }


def tool_portfolio(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Snapshot all contracts in a single read."""
    from lhgp.portfolio import portfolio_summary

    include_terminal = bool(args.get("include_terminal", True))
    limit = int(args.get("limit") or 500)
    snap = portfolio_summary(ctx["conn"], include_terminal=include_terminal, limit=limit)
    return snap.to_dict()


def tool_trace_contract(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Full timeline + latest user_evaluation + latest acceptance_diff for a contract."""
    from lhgp.portfolio import trace_contract

    contract_id = str(args.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("contract_id is required")
    contract_revision = args.get("contract_revision")
    if contract_revision is not None:
        contract_revision = int(contract_revision)
    limit = int(args.get("limit") or 500)
    return trace_contract(
        ctx["conn"], contract_id, contract_revision=contract_revision, limit=limit
    )


def tool_deadline_report(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Compute the deadline-level decision for every active contract."""
    from datetime import UTC, datetime

    from lhgp.enforcement import (
        DeadlineEnforcer,
        compute_deadline_level,
        format_deadline_report,
    )
    from lhgp.persistence.store import get_contract

    limit = int(args.get("limit") or 500)
    conn = ctx["conn"]
    rows = conn.execute(
        """
        SELECT contract_id FROM contracts
        WHERE state IN ('active', 'paused', 'blocked', 'drafted')
        ORDER BY updated_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    pairs = []
    now = datetime.now(UTC)
    for (cid,) in rows:
        view = get_contract(conn, cid)
        if view is None:
            continue
        decision = compute_deadline_level(view.draft.deadline_at, now=now)
        pairs.append((view, decision))
    enforcer = DeadlineEnforcer()
    actions = enforcer.enforce_all(pairs)
    return format_deadline_report(actions)


def _mcp_store_transaction(conn: Any) -> Any:
    """Context manager for MCP tool writes; uses longtask's transaction() helper.

    Bare :func:`sqlite3.Connection` does not give us a context manager for
    transactions (lhgp.persistence.transaction is a thin re-export, but
    not all connection objects exposed to MCP have a transaction() method
    bound). We use the longtask.persistence.schema.transaction directly.
    """
    from contextlib import contextmanager

    from longtask.persistence.schema import transaction

    @contextmanager
    def _ctx() -> Any:
        with transaction(conn) as c:
            yield c

    return _ctx()


def _get_session_token() -> str:
    """从环境变量获取 per-attempt session token（daemon spawn 时注入）。"""
    import os

    return os.environ.get("LHGP_SESSION_TOKEN", "")


def tool_write_back(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """写回进度、终态、结构化验收证据和实际模型身份。"""
    return _mcp_route(Method.ATTEMPT_WRITE_BACK, args, ctx)


def tool_attach_to_executor(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """执行者侧：模型被协议拉起时认领自己的 attempt 上下文 + 报告结果。

    流程（合并多次 RPC 调用，AI 一次工具调用完成）：
    1. 读自己的 attempt/status 拿快照（active.md 路径、租约代次）；
    2. 展示 active.md 全文（让模型见到 §4.1 上下文）；
    3. 可选：调用 attempt/write-back 报告 succeeded|failed + 进度；
    4. 可选：调用 lease/renew 续心跳（节流到默认 60s，模型按需调）。

    主动权在模型：模型读完上下文后自己选择继续干还是上报结果。
    """
    contract_id = args["contract_id"]
    attempt_id = args["attempt_id"]
    report_state = args.get("report_state")
    progress_note = args.get("progress_note")

    # 1. 读 attempt 状态（事件史 + 租约）
    status_envelope = parse_envelope(
        {
            "method": Method.ATTEMPT_STATUS.value,
            "request_id": _mcp_request_id(Method.ATTEMPT_STATUS, args),
            "client_id": "mcp",
            "protocol_version": PROTOCOL_VERSION,
            "params": {"contract_id": contract_id, "attempt_id": attempt_id},
        }
    )
    status = route(status_envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"]).get(
        "result", {}
    )

    # 2. 读上下文快照
    snapshot = _read_snapshot(ctx, status)
    out: dict[str, Any] = {
        "status": status,
        "snapshot": snapshot,
    }

    # 3. 报告结果（如有）
    if report_state in ("succeeded", "failed"):
        # 严格 fencing：写回需 write_generation（代次），从 status.lease.generation 取
        generation = (status.get("lease") or {}).get("generation")
        if generation is None:
            out["write_back_error"] = "no live lease; cannot write back"
        else:
            wb_envelope = parse_envelope(
                {
                    "method": Method.ATTEMPT_WRITE_BACK.value,
                    "request_id": _mcp_request_id(Method.ATTEMPT_WRITE_BACK, args),
                    "client_id": "mcp",
                    "protocol_version": PROTOCOL_VERSION,
                    "params": {
                        "contract_id": contract_id,
                        "attempt_id": attempt_id,
                        "write_generation": generation,
                        "attempt_state": report_state,
                        "progress_note": progress_note or "",
                        "session_token": _get_session_token(),
                    },
                }
            )
            try:
                wb = route(wb_envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"])
                out["write_back"] = wb.get("result", wb)
            except RpcError as exc:
                out["write_back_error"] = str(exc)
    elif progress_note and not report_state:
        # 纯进度更新：写 attempt/write-back 但 attempt_state 不传
        # → 由 RPC handler 视作无终态；安全。或不写终态传 progress_note 即可。
        generation = (status.get("lease") or {}).get("generation")
        if generation is None:
            # 无租约 = 无写权：进度从未落库。静默丢弃会让模型以为进度
            # 已存（工具面审计 R4）——必须显式报告。
            out["progress_error"] = (
                "no live lease for this attempt; progress not persisted. "
                "Report state via report_state once the attempt terminates, "
                "or ask the user to re-dispatch."
            )
        else:
            wb_envelope = parse_envelope(
                {
                    "method": Method.ATTEMPT_WRITE_BACK.value,
                    "request_id": _mcp_request_id(Method.ATTEMPT_WRITE_BACK, args),
                    "client_id": "mcp",
                    "protocol_version": PROTOCOL_VERSION,
                    "params": {
                        "contract_id": contract_id,
                        "attempt_id": attempt_id,
                        "write_generation": generation,
                        "progress_note": progress_note,
                        "session_token": _get_session_token(),
                    },
                }
            )
            try:
                out["progress_written"] = route(
                    wb_envelope, conn=ctx["conn"], now=_now(), registry=ctx["registry"]
                ).get("result")
            except RpcError as exc:
                out["progress_error"] = str(exc)
    return out


_SNAPSHOT_TEXT_LIMIT = 20000


def _read_snapshot(ctx: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    """从 attempt/status 的 result 找 contract，再读 context/attempts/<id>/active.md。

    审计 RPC-R9：handover 可能很大，无上限会把模型上下文撑爆——
    超限截断并显式标注。
    """
    contract_id = status.get("contract_id")
    if not contract_id:
        return {}
    root: Path = ctx["root"]
    contract_dir = root / "contracts" / contract_id
    if not contract_dir.is_dir():
        return {"hint": "no contract projection yet (try after first tick)"}
    # 找最近 attempt 的 context/attempts/<id>/active.md
    context_dir = contract_dir / "context" / "attempts"
    if not context_dir.is_dir():
        return {
            "hint": "no context snapshot built yet (§4.1 required=true only)",
            "handover_path": str(contract_dir / "handover.md"),
        }
    # 最新 attempt 目录
    attempts = sorted(context_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
    if not attempts:
        return {"handover_path": str(contract_dir / "handover.md")}

    def _capped(path: Path) -> tuple[str | None, bool]:
        if not path.is_file():
            return None, False
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > _SNAPSHOT_TEXT_LIMIT:
            return text[:_SNAPSHOT_TEXT_LIMIT] + "\n...[truncated]", True
        return text, False

    active_content, active_truncated = _capped(attempts[0] / "active.md")
    handover_content, handover_truncated = _capped(contract_dir / "handover.md")
    out: dict[str, Any] = {
        "active_path": str(attempts[0] / "active.md"),
        "active_content": active_content,
        "handover_path": str(contract_dir / "handover.md"),
        "handover_content": handover_content,
    }
    if active_truncated or handover_truncated:
        out["truncated"] = True
    return out


TOOLS: dict[
    str, tuple[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]], dict[str, Any]]
] = {
    "longtask_health": (
        tool_health,
        {
            "description": (
                "探活：确认 MCP "
                "入口在线，返回协议版本与可用工具名。会话第一步调用；之后按场景选工具——查现状用 "
                "get_contract/get_goal，立合同用 "
                "prepare，批准前或派工异常时用 doctor。本工具只读。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ),
    "longtask_doctor": (
        tool_doctor,
        {
            "description": (
                "本机只读诊断：数据库完整性、注册表、已启用 CLI 能否启动。何时用：批准合同之前（缺 "
                "CLI 先换执行者，省预算）；派工失败之后（原因常在这里）。结果里的 FAIL "
                "项就是需要处理的问题。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ),
    "longtask_list_executors": (
        tool_list_executors,
        {
            "description": (
                "列出执行器池及启停状态（可 enabled_only "
                "过滤）。何时用：起草合同前确认候选；attach/write_back "
                "前确认目标执行器存在。合同 authority "
                "显式绑定时，未绑定的执行器即使列出也不会被派工（default-deny）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "enabled_only": {"type": "boolean", "default": False},
                },
            },
        },
    ),
    "longtask_prepare_contract": (
        tool_prepare_contract,
        {
            "description": (
                "立远期合同。详见 skills/longtask-contract/SKILL.md §4。\n"
                "objective 写验收不是方法；acceptance_checks 逐条可核对；"
                "workload_initial_hours 如实填（u=workload/time_left 决定派工）。"
            ),
            "inputSchema": {
                "type": "object",
                "required": [
                    "title",
                    "objective",
                    "deadline_at",
                    "acceptance_standard",
                    "acceptance_checks",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "objective": {
                        "type": "string",
                        "description": "写验收（产出的结果），不是写方法",
                    },
                    "deadline_at": {
                        "type": "string",
                        "description": "ISO 8601（必含时区），如 2026-09-12T18:00:00+08:00",
                    },
                    "acceptance_standard": {"type": "string"},
                    "acceptance_checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "逐条可独立核对的子项；verifier 照着判",
                    },
                    "hard_constraints": {
                        "type": "object",
                        "default": {},
                        "description": (
                            "如 {file_effects: {mode: workspace-write, workspace_root: /abs/path}}"
                        ),
                    },
                    "workload_initial_hours": {"type": "number", "default": 1.0},
                    "budget": {
                        "type": "object",
                        "description": "可选覆盖默认预算",
                    },
                    "authority": {
                        "type": "object",
                        "description": "允许被唤起的 executor、model 与 role 绑定",
                    },
                    "attention": {"type": "object"},
                    "continuity": {"type": "object"},
                    "auto_approve": {
                        "type": "object",
                        "description": (
                            "Pre-authorised scope for plan auto-approval.  "
                            "{enabled, actions, max_budget_increment, max_spec_changes}.  "
                            "Empty = sign-off always required."
                        ),
                    },
                    "context": {"type": "object"},
                    "execution": {"type": "object"},
                    "client_meta": {"type": "object"},
                    "contract_id": {"type": "string", "description": "可选自定义 ID"},
                    "request_id": {
                        "type": "string",
                        "description": "幂等重试键；重试同一变更时必须复用",
                    },
                },
            },
        },
    ),
    "longtask_approve_contract": (
        tool_approve_contract,
        {
            "description": "批准合同（drafted → active）。仅限用户（Principal）："
            "模型客户端调用会返回 AUTH_FAILED——请提示用户在 CLI 执行 "
            "`lhgp approve <contract_id>`。模型可先用 get_contract 阅读"
            "合同再向用户说明批准影响。",
            "inputSchema": {
                "type": "object",
                "required": ["contract_id"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "revision": {"type": "integer", "description": "CAS 期望版本号（可选）"},
                    "request_id": {"type": "string", "description": "幂等重试键；重试时复用"},
                },
            },
        },
    ),
    "longtask_request_verification": (
        tool_request_verification,
        {
            "description": (
                "请求验收（§12.4）：不派执行者，只派独立 verifier "
                "核对现有交付物。发起者可以是用户也可以是模型（计入该合同验证预算）。何时用：执行预算耗尽但交付物疑似就绪；或执行者自报成功需要正式验收。预算耗尽返回错误并提示升级——不要重试。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "request_id": {"type": "string", "description": "幂等重试键；重试时复用"},
                },
            },
        },
    ),
    "longtask_user_confirm_spec_verdict": (
        tool_user_confirm_spec_verdict,
        {
            "description": (
                "用户确认 CANDIDATE Spec 验收（Principal-gated）。"
                '当 Spec 包含 judge="user" 判据时，verifier 通过后合同停在'
                " CANDIDATE 等用户最终签字；这是唯一把它推到 PASSED 的路径。"
                "模型客户端调用会返回 AUTH_FAILED——请提示用户在 CLI 执行"
                " `lhgp contract user-confirm <contract_id>`。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "note": {
                        "type": "string",
                        "description": "可选：用户签字的备注，会落进审计事件",
                    },
                },
            },
        },
    ),
    "longtask_get_contract": (
        tool_get_contract,
        {
            "description": (
                "查询单份合同权威视图（§11.6 字段表）+ "
                "决策/attempt/验收三段历史。何时用：需要知道某份合同现在怎样、为什么 "
                "blocked、verifier 判了什么。contract_id 从 "
                "list_contracts 拿。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "decision_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                        "description": "返回该合同最近决策历史条数",
                    },
                    "attempt_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                        "description": "返回该合同最近 attempt 历史条数",
                    },
                },
            },
        },
    ),
    "longtask_list_contracts": (
        tool_list_contracts,
        {
            "description": (
                "按状态过滤列出合同，每项含最新 "
                "deadline_snapshot。何时用：接手会话时盘点；筛选风险合同后再用 "
                "get_contract 看细节。响应 has_more=true 时把 "
                "next_cursor 作为 cursor 传入本工具继续翻页。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 20,
                        "description": "最多返回 200 份合同，避免一次读取过大上下文",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "上一页响应的 next_cursor；has_more=true 时传入即可翻页",
                    },
                },
            },
        },
    ),
    "longtask_get_goal": (
        tool_get_goal,
        {
            "description": (
                "查询 Goal 聚合视图：阶段计划、各阶段合同状态、整体进度与 Deadline "
                "风险。何时用：多合同（stages）流程中判断现在在哪一步。看单份合同的事件/租约细节用 "
                "get_contract。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["goal_id"],
                "properties": {"goal_id": {"type": "string"}},
            },
        },
    ),
    "longtask_prepare_goal": (
        tool_prepare_goal,
        {
            "description": (
                "立合同（goal/prepare 路径）：返回合同 + 7 类 admission offer，"
                "并可关联 goal_id/stage_id（这是 advance_goal 与 "
                "goal_contract_draft 可用的前提）。批准仍需用户在 CLI 执行。"
            ),
            "inputSchema": {
                "type": "object",
                "required": [
                    "title",
                    "objective",
                    "deadline_at",
                    "acceptance_standard",
                    "acceptance_checks",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "deadline_at": {"type": "string", "description": "ISO8601 必须带时区"},
                    "acceptance_standard": {"type": "string"},
                    "acceptance_checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "验收检查逐条数组；typed check 传对象数组（file-exists 等）"
                        ),
                    },
                    "acceptance_verifier": {
                        "type": "string",
                        "enum": ["cross_check", "none"],
                        "default": "cross_check",
                        "description": "验收者模式：cross_check=独立 verifier 复核（默认）",
                    },
                    "workload_initial_hours": {"type": "number"},
                    "hard_constraints": {"type": "object"},
                    "budget": {"type": "object"},
                    "authority": {"type": "object"},
                    "attention": {"type": "object"},
                    "continuity": {"type": "object"},
                    "auto_approve": {
                        "type": "object",
                        "description": (
                            "Pre-authorised scope for plan auto-approval.  "
                            "{enabled: bool, actions: [str], max_budget_increment: int, "
                            "max_spec_changes: int}.  Empty object = sign-off always required."
                        ),
                    },
                    "context": {"type": "object"},
                    "execution": {"type": "object"},
                    "client_meta": {"type": "object"},
                    "contract_id": {"type": "string"},
                    "goal_id": {"type": "string", "description": "关联已有 Goal（stages 机制）"},
                    "stage_id": {
                        "type": "string",
                        "description": "绑定 Goal 阶段（acceptance 绑定校验）",
                    },
                },
            },
        },
    ),
    "lhgp_send_message": (
        tool_send_message,
        {
            "description": (
                "发送结构化消息（context=进度笔记 / question=向用户提问）。"
                "directive 类仅用户可发。消息会出现在下个 attempt 的上下文里。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id", "text"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "text": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["context", "question"],
                        "default": "context",
                    },
                    "to": {"type": "string"},
                },
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
    ),
    "lhgp_inbox": (
        tool_inbox,
        {
            "description": (
                "读取合同上的所有消息：用户指令、agent 进度笔记、待回答问题。"
                "何时用：接手 attempt 或 poll 之间检查有没有新指令。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id"],
                "properties": {"contract_id": {"type": "string"}},
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
    ),
    "lhgp_brief": (
        tool_brief,
        {
            "description": (
                "接手包（只读）：一份合同的状态/风险/预算/最近 attempt 与 "
                "verifier 结论/通知。何时用：接手会话、或想知道某合同现在"
                "什么情况、下一步该做什么。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id"],
                "properties": {"contract_id": {"type": "string"}},
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
    ),
    "lhgp_resume_attempt": (
        tool_resume_attempt,
        {
            "description": (
                "attempt 恢复入口：读取 active.md + handover.md，拼出一次 self-contained "
                "resume brief（body 字段直接喂给新会话）。会落 attempt/resumed 审计事件。"
                "何时用：上一轮崩溃 / 上下文耗尽 / 502 失败后想无脑接着干。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id", "attempt_id"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "attempt_id": {"type": "string"},
                    "next_attempt_id": {
                        "type": "string",
                        "description": "可选；新 attempt ID（默认从 attempt_id + 时间派生）",
                    },
                },
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
    ),
    "lhgp_board": (
        tool_board,
        {
            "description": (
                "多合同一屏（只读）：按风险排序的状态/预算/下次决策点表。"
                "何时用：接手会话盘点、汇报。默认不含终态合同；"
                "include_terminal=true 时包含。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_terminal": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
    ),
    "lhgp_stats": (
        tool_stats,
        {
            "description": (
                "成本台账（只读）：attempt 按角色/执行器/状态分布、实际墙钟"
                "p50/max、退出码分布。何时用：写预算前看历史消耗；复盘某合同"
                "或全局执行成本。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"contract_id": {"type": "string"}},
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
    ),
    "lhgp_propose_plan": (
        tool_propose_plan,
        {
            "description": (
                "提交 Goal 计划修订提案（ADR-004 规则 6）：只落 goal/proposed "
                "事件，不写权威状态。用户批准后由用户在 CLI 执行 goal/update "
                "落地。何时用：发现计划需要调整且用户不在场——提案留痕，"
                "不要反复重试。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["goal_id"],
                "properties": {
                    "goal_id": {"type": "string"},
                    "plan": {"type": "object", "description": "建议的计划结构"},
                    "reason": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
    ),
    "longtask_list_goals": (
        tool_list_goals,
        {
            "description": (
                "列出全部 Goal 聚合视图。何时用：接手会话盘点进行中的目标；确定 goal_id "
                "后用 get_goal 看阶段细节。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        },
    ),
    "longtask_update_goal": (
        tool_update_goal,
        {
            "description": "更新 Goal 计划或进度（支持 revision CAS）。仅限用户（Principal）："
            "模型客户端调用会返回 AUTH_FAILED——请向用户陈述修订建议，由用户在 CLI 执行。",
            "inputSchema": {
                "type": "object",
                "required": ["goal_id", "revision"],
                "properties": {
                    "goal_id": {"type": "string"},
                    "revision": {"type": "integer"},
                    "plan": {"type": "object"},
                    "progress": {"type": "object"},
                },
            },
        },
    ),
    "longtask_advance_goal": (
        tool_advance_goal,
        {
            "description": (
                "完成 Goal 当前阶段并推进到下一阶段（revision "
                "CAS）。前置：当前阶段绑定合同的验收已通过——返回 STAGE_NOT_PASSED "
                "时先等 verifier 结果或 "
                "request_verification，不要重试推进。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["goal_id", "stage_id", "revision"],
                "properties": {
                    "goal_id": {"type": "string"},
                    "stage_id": {"type": "string"},
                    "revision": {"type": "integer"},
                },
            },
        },
    ),
    "longtask_next_goal_action": (
        tool_next_goal_action,
        {
            "description": (
                "读取 Goal 当前阶段建议的下一步动作（只读，不执行）。响应的 action "
                "字段给出具体动作（如 "
                "approve/resume_contract），按提示调用对应工具，不要凭猜测跳步。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["goal_id"],
                "properties": {"goal_id": {"type": "string"}},
            },
        },
    ),
    "longtask_goal_contract_draft": (
        tool_goal_contract_draft,
        {
            "description": (
                "根据 Goal 当前阶段生成合同草案（只读预览，不落库）。"
                "何时用：阶段需要新合同时先取推荐草案交用户审阅；"
                "确认后用 prepare_contract/prepare_goal 把草案参数正式立约。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["goal_id"],
                "properties": {
                    "goal_id": {"type": "string"},
                    "stage_id": {"type": "string"},
                },
            },
        },
    ),
    "longtask_attach_to_executor": (
        tool_attach_to_executor,
        {
            "description": (
                "执行者侧便利工具：认领 attempt + 读上下文快照 + "
                "报告结果。执行者被协议拉起时（task_prompt 含 "
                "objective+附言）调用；带 report_state 时写回终态，带 "
                "progress_note 时写回进度（无活租约会显式返回 "
                "progress_error，进度未被保存）。纯读路径返回 "
                "status+snapshot。"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["contract_id", "attempt_id"],
                "properties": {
                    "contract_id": {"type": "string"},
                    "attempt_id": {"type": "string"},
                    "report_state": {
                        "type": "string",
                        "enum": ["succeeded", "failed"],
                        "description": "如要上报结果就填；仅写进度则不填",
                    },
                    "progress_note": {
                        "type": "string",
                        "description": "进度要点（落 context/scratch-updated 事件）",
                    },
                    "request_id": {
                        "type": "string",
                        "description": "写回幂等重试键；重试同一报告时必须复用",
                    },
                },
            },
        },
    ),
}

# P6 新命名与旧命名双轨并存：旧工具保持可发现，新增工具使用协议名
# lhgp_*，避免升级时破坏已有 Agent 的工具缓存。
_RENAMED_TOOLS = {
    "lhgp_health": "longtask_health",
    "lhgp_doctor": "longtask_doctor",
    "lhgp_list_executors": "longtask_list_executors",
    "lhgp_approve_goal": "longtask_approve_contract",
    "lhgp_get_goal": "longtask_get_goal",
    "lhgp_list_goals": "longtask_list_goals",
    "lhgp_update_goal": "longtask_update_goal",
    "lhgp_advance_goal": "longtask_advance_goal",
    "lhgp_next_goal_action": "longtask_next_goal_action",
    "lhgp_goal_contract_draft": "longtask_goal_contract_draft",
    "lhgp_attach_executor": "longtask_attach_to_executor",
    # 合同读取/列表此前只在遗留 longtask_* 命名空间暴露，只用 lhgp_* 规范
    # 工具的 AI 无法查看自己立下的合同，补齐以免规范工具集存在死角。
    "lhgp_get_contract": "longtask_get_contract",
    "lhgp_list_contracts": "longtask_list_contracts",
    "lhgp_request_verification": "longtask_request_verification",
}
for _new_name, _legacy_name in _RENAMED_TOOLS.items():
    _handler, _metadata = TOOLS[_legacy_name]
    TOOLS[_new_name] = (
        _handler,
        {**_metadata, "description": f"[LHGP] {_metadata['description']}"},
    )

# lhgp_prepare_goal 是独立工具（goal/prepare 路径，返回 admission offer），
# 不是 longtask_prepare_contract 的改名——直注册别名，不走改名表。
TOOLS["lhgp_prepare_goal"] = TOOLS["longtask_prepare_goal"]

TOOLS.update(
    {
        "lhgp_attempt_status": (
            tool_attempt_status,
            {
                "description": (
                    "读取 attempt 状态、事件历史与租约（lease.generation 是 "
                    "write_back 的必带参数）。attempt 不存在返回 "
                    "UNKNOWN_ATTEMPT——检查拼写，不要拿空结果当作正常。只读。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id", "attempt_id"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "attempt_id": {"type": "string"},
                    },
                },
            },
        ),
        "lhgp_submit_evaluation": (
            tool_submit_evaluation,
            {
                "description": (
                    "P6 反馈回路：合同达到终态后由用户/外部对结果评分。"
                    "rating 1-5，verdict ∈ {accept, partial, reject}，comments 自由文本。"
                    "触发 user/evaluation-submitted 事件，写入 user_evaluations 表。"
                    "verdict=reject 会被 promoter 用来触发下一轮 escalate。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id", "rating", "verdict"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "contract_revision": {"type": "integer", "minimum": 1},
                        "attempt_id": {"type": "string"},
                        "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                        "verdict": {"enum": ["accept", "partial", "reject"]},
                        "evaluator": {"type": "string", "description": "评估者标识，默认 'user'"},
                        "comments": {"type": "string"},
                    },
                },
            },
        ),
        "lhgp_submit_plan": (
            tool_submit_plan,
            {
                "description": (
                    "Plan-mode gate：合同 attempt 派工前，agent 必须提交结构化计划。"
                    "每个 step 包含 action（白名单：read file/run command/ask user/"
                    "verify acceptance/write file/search code）、target、rationale、"
                    "expected_outcome。validator 检查：步骤数 ≥1、action 在白名单、"
                    "rationale 含 objective 关键词、每个 acceptance check 被某 step 的"
                    " target 或 expected_outcome 覆盖。approved 时落 plan/approved "
                    "事件，runner 看到才放行 DISPATCHING→RUNNING；rejected 时落"
                    " plan/rejected 事件并返回 rejection_reasons。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id", "steps"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "submitted_by": {
                            "type": "string",
                            "description": "提交者标识，默认 'agent:mcp'",
                        },
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": [
                                    "action",
                                    "target",
                                    "rationale",
                                    "expected_outcome",
                                ],
                                "properties": {
                                    "step_id": {"type": "integer", "minimum": 1},
                                    "action": {
                                        "enum": [
                                            "read file",
                                            "run command",
                                            "ask user",
                                            "verify acceptance",
                                            "write file",
                                            "search code",
                                        ]
                                    },
                                    "target": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    "expected_outcome": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        ),
        "lhgp_plan_signoff": (
            tool_plan_signoff,
            {
                "description": (
                    "User sign-off for a plan that lhgp_submit_plan flagged "
                    "as requires_signoff (out of auto-approve scope).  Re-runs "
                    "the validator, then writes PLAN_APPROVED on success and "
                    "wakes the contract.  Returns signed_off=True/False plus "
                    "rejection_reasons when the plan is no longer structurally "
                    "valid."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id", "steps"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "signoff_by": {"type": "string"},
                        "note": {"type": "string"},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["action", "target", "rationale", "expected_outcome"],
                                "properties": {
                                    "step_id": {"type": "integer"},
                                    "action": {"type": "string"},
                                    "target": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    "expected_outcome": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        ),
        "lhgp_compute_diff": (
            tool_compute_diff,
            {
                "description": (
                    "P6 反馈回路：计算 attempt 报告的产物与当前 workspace 状态的 diff。"
                    "返回 created/modified/deleted 列表，存入 acceptance_diffs 表。"
                    "作为 :func:`lhgp_evolve_templates` 的输入信号。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "contract_revision": {"type": "integer", "minimum": 1},
                        "attempt_id": {"type": "string"},
                        "workspace": {
                            "type": "string",
                            "description": "workspace path, default data-dir/contracts/<id>/",
                        },
                    },
                },
            },
        ),
        "lhgp_evolve_templates": (
            tool_evolve_templates,
            {
                "description": (
                    "P6 提升闭环：从 user_evaluations 中挖掘高评分合同，"
                    "自动写入 templates/ 目录。quality_threshold 默认 0.7，"
                    "min_rating 默认 4。每次写入文件命名 auto-<contract_id>-r<r>.json。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "min_rating": {"type": "integer", "minimum": 1, "maximum": 5, "default": 4},
                        "quality_threshold": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "default": 0.7,
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                    },
                },
            },
        ),
        "lhgp_portfolio": (
            tool_portfolio,
            {
                "description": (
                    "P6 多合同管理：聚合所有合同当前状态 + 用户最近评价，"
                    "返回 by_state / by_deadline / by_acceptance 计数 + 列表。"
                    "适合 dashboard / health 检查。只读。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "include_terminal": {"type": "boolean", "default": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
                    },
                },
            },
        ),
        "lhgp_trace": (
            tool_trace_contract,
            {
                "description": (
                    "P6 trace：单份合同完整时间轴，附最新 user_evaluation 与 acceptance_diff。"
                    "调试'为什么这份合同这样关闭'用。只读。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "contract_revision": {"type": "integer", "minimum": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
                    },
                },
            },
        ),
        "lhgp_deadline_report": (
            tool_deadline_report,
            {
                "description": (
                    "P6 deadline 多级升级：扫所有 active 合同，按 elapsed ratio 划 normal/warning/"
                    "urgent/breached，返回每级动作建议（republish / notify / lock）。"
                    "不直接副作用，调用方按 actions 字段落库。只读 + 决策输出。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
                    },
                },
            },
        ),
        "lhgp_notifications": (
            tool_notifications,
            {
                "description": (
                    "查看通知 "
                    "outbox（只读）：blocked(need-user)、预算耗尽、deadline "
                    "风险变化都会产生通知。何时用：合同 blocked "
                    "后了解原因与用户需要做什么。include_payload 默认 "
                    "false（防上下文泄露），确需完整负载才传 true。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"enum": ["pending", "leased", "sent"]},
                        "goal_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                        "include_payload": {"type": "boolean"},
                    },
                },
            },
        ),
        "lhgp_interrupt_attempt": (
            tool_interrupt_attempt,
            {
                "description": (
                    "请求 daemon 优雅中断指定 attempt"
                    "（落 attempt/cancelled 事件，事件归属真实发起者）。"
                    "何时用：仅当用户明确要求停止。"
                    "外部进程的实际终止由 daemon 后续 tick 确认，本工具不保证立即生效。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id", "attempt_id"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "attempt_id": {"type": "string"},
                        "reason": {"type": "string"},
                        "request_id": {"type": "string", "description": "幂等重试键；重试时复用"},
                    },
                },
            },
        ),
        "lhgp_write_back": (
            tool_write_back,
            {
                "description": (
                    "写回 attempt 进度/终态/验收 evidence/实际 "
                    "model_id。write_generation 取自 attempt_status "
                    "的 lease.generation。边界：executor "
                    "不得把合同写成完成态（STATE_FORBIDDEN——完成由 verifier "
                    "裁决）；verifier 终态必须带 evidence；返回 LEASE_FENCED "
                    "表示租约已换代，重新取状态再写。"
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["contract_id", "attempt_id", "write_generation"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "attempt_id": {"type": "string"},
                        "write_generation": {"type": "integer"},
                        "attempt_state": {"enum": ["succeeded", "failed"]},
                        "progress_note": {"type": "string"},
                        "model_id": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "object"}},
                        "request_id": {"type": "string", "description": "幂等重试键；重试时复用"},
                    },
                },
            },
        ),
    }
)

# MCP tool annotations are advisory metadata consumed by hosts before execution.
# Keep the policy explicit here so aliases and future tools cannot silently lose
# the local-only trust boundary or be presented as harmless reads.
_DESTRUCTIVE_TOOLS = {
    # Contract/Goal mutations.  ``destructiveHint`` is intentionally used for
    # any persistent state change, not only process termination: MCP hosts need
    # to request confirmation before a model edits the durable commitment.
    "longtask_prepare_contract",
    "longtask_prepare_goal",
    "longtask_approve_contract",
    "longtask_request_verification",
    "longtask_update_goal",
    "longtask_advance_goal",
    "longtask_attach_to_executor",
    "lhgp_approve_goal",
    "lhgp_update_goal",
    "lhgp_advance_goal",
    "lhgp_attach_executor",
    "lhgp_request_verification",
    "lhgp_interrupt_attempt",
    "lhgp_resume_attempt",
    "lhgp_write_back",
    # P6 反馈回路
    "lhgp_submit_evaluation",
    "lhgp_compute_diff",
    "lhgp_evolve_templates",
    # Plan-mode gate
    "lhgp_submit_plan",
    "lhgp_plan_signoff",
}
_READ_ONLY_TOOLS = {
    "longtask_health",
    "longtask_doctor",
    "longtask_list_executors",
    "longtask_get_contract",
    "longtask_list_contracts",
    "longtask_get_goal",
    "longtask_list_goals",
    "longtask_next_goal_action",
    "longtask_goal_contract_draft",
    "lhgp_health",
    "lhgp_doctor",
    "lhgp_list_executors",
    "lhgp_get_goal",
    "lhgp_list_goals",
    "lhgp_next_goal_action",
    "lhgp_goal_contract_draft",
    "lhgp_get_contract",
    "lhgp_list_contracts",
    "lhgp_attempt_status",
    "lhgp_notifications",
    # P6
    "lhgp_portfolio",
    "lhgp_trace",
    "lhgp_deadline_report",
}
for _tool_name, (_tool_fn, _tool_schema) in list(TOOLS.items()):
    _tool_schema.setdefault(
        "annotations",
        {
            "readOnlyHint": _tool_name in _READ_ONLY_TOOLS,
            "destructiveHint": _tool_name in _DESTRUCTIVE_TOOLS,
            "openWorldHint": False,
        },
    )
TOOL_NAMES = sorted(TOOLS.keys())


# ─── JSON-RPC over stdio ────────────────────────────────────────────────────


def _make_response(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id: Any, code: Any, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _server_name() -> str:
    """Expose the canonical name when launched through the canonical entrypoint."""
    return "lhgp-mcp" if Path(sys.argv[0]).stem == "lhgp-mcp" else "longtask-mcp"


def _validate_arguments(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """按工具 inputSchema 校验 arguments（required / 类型 / 未知键）。

    只实现 MCP schema 实际用到的子集（object/string/integer/boolean/
    number/array），未知键默认拒绝——fail-closed 与协议其他边界一致。
    返回错误信息字符串，None 表示通过。
    """
    properties: dict[str, Any] = schema.get("properties", {})
    if schema.get("type") != "object" and not properties:
        return None
    required = schema.get("required", [])
    for key in required:
        if key not in args:
            return f"missing required parameter: {key}"
    type_checkers: dict[str, Any] = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    for key, value in args.items():
        prop = properties.get(key)
        if prop is None:
            # request_id 是 §11.3 的协议级幂等键，任何工具都可携带
            # （重试纪律），不要求每个 schema 单独声明。
            if key == "request_id":
                if not isinstance(value, str):
                    return "request_id must be a string"
                continue
            return f"unknown parameter: {key}"
        expected = prop.get("type")
        checker = type_checkers.get(expected) if expected else None
        if checker and not checker(value):
            return f"parameter {key!r} must be of type {expected}"
    return None


def _dispatch(ctx: dict[str, Any], method: str, params: Any, req_id: Any) -> dict[str, Any]:
    if method == "initialize":
        return _make_response(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": _server_name(), "version": __version__},
                "capabilities": {"tools": {}},
            },
        )
    if method == "ping":
        return _make_response(req_id, {})
    if method == "tools/list":
        return _make_response(
            req_id,
            {"tools": [{"name": name, **schema} for name, (_fn, schema) in TOOLS.items()]},
        )
    if method == "tools/call":
        if not isinstance(params, dict):
            return _make_error(req_id, -32602, "invalid arguments: params must be an object")
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _make_error(req_id, -32602, "invalid arguments: arguments must be an object")
        if tool_name not in TOOLS:
            return _make_error(req_id, -32602, f"unknown tool: {tool_name}")
        try:
            fn, schema = TOOLS[tool_name]
            # R1（工具面审计）：inputSchema 在服务端强制执行——schema 只给
            # host 看而不校验时，未知键静默忽略、类型不查，是「字符串验收
            # 检查逐字符入库」一类问题的根因。required / 类型 / 未知键三查。
            # schema 是 {description, inputSchema, annotations} 元数据，
            # 校验器吃的是内层 inputSchema——传外层会让 type 检查永远放行。
            validation_error = _validate_arguments(args, schema.get("inputSchema", {}))
            if validation_error is not None:
                return _make_error(req_id, -32602, f"invalid arguments: {validation_error}")
            result = fn(args, ctx)
            # MCP 要求 content 数组里至少一个 item
            return _make_response(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    ]
                },
            )
        except RpcError as exc:
            # 协议错误码语义比 JSON-RPC 数字更有意义；原样透传 StrEnum 值
            return _make_error(req_id, exc.code.value, str(exc.message), exc.details or None)
        except (KeyError, ValueError, TypeError) as exc:
            return _make_error(req_id, -32602, f"invalid arguments: {exc}")
        except Exception as exc:  # 兜底：避免错误透传污染 stdio
            return _make_error(req_id, -32603, f"internal error: {exc}")
    return _make_error(req_id, -32601, f"method not found: {method}")


def _make_context(root: Path) -> dict[str, Any]:
    """每个连接共享 root/conn/registry（这里一次性 init；未来可按请求切换）。"""
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    registry = ExecutorRegistry.load_from_file(root / "registry.json")
    return {"root": root, "conn": conn, "registry": registry}


def serve_stdio(root: Path) -> None:
    """stdio JSON-RPC 入口：读一行 JSON、写一行 JSON（标准 MCP transport）。

    输出始终 ASCII（ensure_ascii=True）：Windows 上 stdio pipe 透明用
    系统编码（cp936 等）转换 utf-8 字符串会破坏非 ASCII 字节，强制 ASCII
    编码无关，模型侧按 UTF-8 解析 JSON 字符串里 \\u 转义即可。
    """
    ctx = _make_context(root)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                sys.stdout.write(
                    json.dumps(
                        _make_error(None, -32700, f"parse error: {exc}"),
                        ensure_ascii=True,
                    )
                    + "\n"
                )
                sys.stdout.flush()
                continue
            req_id = req.get("id")
            method = req.get("method", "")
            params = req.get("params") or {}
            # 通知（无 id）不回
            if req_id is None and method != "initialize":
                continue
            try:
                response = _dispatch(ctx, method, params, req_id)
            except Exception as exc:  # 兜底：避免错误透传污染 stdio
                response = _make_error(req_id, -32603, f"internal error: {exc}")
            if req_id is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=True, default=str) + "\n")
                sys.stdout.flush()
    finally:
        conn = ctx.get("conn")
        if conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    from longtask.console import harden_stdio

    harden_stdio()
    if _server_name() == "longtask-mcp":
        print(
            "warning: 'longtask-mcp' is deprecated; use 'lhgp-mcp' instead",
            file=sys.stderr,
        )

    parser = argparse.ArgumentParser(
        prog=_server_name(),
        description="LHGP Protocol MCP server (stdio JSON-RPC 2.0)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="LHGP 数据目录（默认 ~/.lhgp；旧安装可回退到 ~/.longtask）",
    )
    args = parser.parse_args(argv)
    root = Path(args.data_dir).expanduser().resolve() if args.data_dir else default_data_root()
    root.mkdir(parents=True, exist_ok=True)
    serve_stdio(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
