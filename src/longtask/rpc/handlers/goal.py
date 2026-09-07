"""goal/* 方法 handler（DESIGN §10.4、§11.2、§6）。

goal/* 是合同/尝试之上的"对外承诺"语义：调用 goal/prepare 不直接落
副作用，而是先返回一份 admission offer，列示当前可用的执行器、被
拒的执行器与原因、forecast 与可声明的 guarantee。SPEC §10.4 要求
goal/prepare 返回 7 类信息：eligible/rejected executors、
acceptance_executable、forecast p50/p90/confidence、
verification_reserve_sufficient、safe_start_by、
uncontrolled_risks、declared_guarantees。

P2 范围：
- goal/prepare 走单一 validator（contracts.validation.validate_draft）
- 调用 admission/eligibility.evaluate 给每个候选做 7 条件判定
- 落库与 contract/prepare 同样 save_contract（不改 draft），不拒接；
  拒接留给 goal/admission_check 这个只读方法
- 返回 result = {"contract": <view>, "admission": <offer.to_dict()>}

P3 之后再做：uncontrolled_risks / declared_guarantees 从 continuity/
authority/attention 字段聚合（目前留空元组）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from longtask.acceptance.checks import check_identity
from longtask.admission.eligibility import (
    CandidateFacts,
)
from longtask.admission.eligibility import (
    evaluate as evaluate_eligibility,
)
from longtask.admission.offer import (
    ExecutorCandidateView,
    Offer,
)
from longtask.contracts.authority import binding_for_executor
from longtask.contracts.contract_draft import from_dict
from longtask.contracts.validation import validate_draft
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    advance_goal,
    get_contract,
    get_events_by_request_id,
    get_goal,
    goal_contract_draft,
    goal_next_action,
    list_goals,
    patch_goal,
    save_contract,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.handlers._common import (
    _is_safe_contract_id,
    _parse_iso,
    require_principal,
    resolve_actor,
)

if TYPE_CHECKING:
    from longtask.rpc.server import RequestEnvelope


def _strict_candidate_bool(candidate: dict[str, Any], key: str, default: bool) -> bool:
    """Parse admission facts without truthiness coercion.

    Registry snapshots are an integration boundary; a string such as
    ``"false"`` must not become a passing eligibility fact.
    """
    value = candidate.get(key, default)
    if not isinstance(value, bool):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"admission candidate field '{key}' must be a boolean",
        )
    return value


def _build_admission_offer(
    *,
    draft_dict: dict[str, Any],
    registry_view: list[dict[str, Any]] | None,
) -> Offer:
    """根据 draft 与（可选的）执行器注册表快照构造一份 admission offer。

    不直接执行 prepare；只把 draft 通过 validate_draft 走结构校验，
    然后逐候选跑 evaluate（）。本函数不知道 budget 当前已消耗数——
    P4 之前 `budget_available` 给定 True；P4 由 caller 注入。
    """
    draft = from_dict(draft_dict)
    eligible: list[ExecutorCandidateView] = []
    rejected: list[ExecutorCandidateView] = []
    for cand in registry_view or []:
        facts = CandidateFacts(
            executor_id=str(cand.get("executor_id") or ""),
            executor_enabled_globally=_strict_candidate_bool(cand, "enabled", False),
            executor_concurrency_available=_strict_candidate_bool(
                cand, "concurrency_available", True
            ),
            capability_satisfied=_strict_candidate_bool(cand, "capability_satisfied", True),
            constraint_enforcement_proven=_strict_candidate_bool(
                cand, "constraint_enforcement_proven", True
            ),
            budget_available=_strict_candidate_bool(cand, "budget_available", True),
            verifier_independent=_strict_candidate_bool(cand, "verifier_independent", True),
        )
        requested_role = str(cand.get("requested_role", "executor"))
        raw_models = cand.get("models") or [cand.get("requested_model", "*")]
        models = tuple(str(model) for model in raw_models if str(model).strip()) or ("*",)
        if "*" in models:
            binding = binding_for_executor(draft.authority, facts.executor_id)
            if binding is not None and binding.models != ("*",):
                models = binding.models

        verdict = None
        requested_model = models[0]
        for model in models:
            candidate_verdict = evaluate_eligibility(
                authority=draft.authority,
                facts=facts,
                requested_model=model,
                requested_role=requested_role,
            )
            if candidate_verdict.eligible:
                verdict = candidate_verdict
                requested_model = model
                break
            verdict = candidate_verdict
        if verdict is None:
            continue
        view = ExecutorCandidateView(
            executor_id=facts.executor_id,
            models=(requested_model,),
            reason=(
                "all 7 conditions satisfied"
                if verdict.eligible
                else f"failed: {','.join(verdict.failed)}"
            ),
        )
        if verdict is not None and verdict.eligible:
            eligible.append(view)
        else:
            rejected.append(view)

    return Offer(
        eligible_executors=tuple(eligible),
        rejected_executors=tuple(rejected),
        acceptance_executable=bool(draft.acceptance.checks),
        forecast_p50_minutes=None,  # P4 之前不计算
        forecast_p90_minutes=None,
        forecast_confidence=None,
        verification_reserve_sufficient=True,  # P5 之前默认 True
        safe_start_by=None,  # P4 由 forecast 推导
        uncontrolled_risks=(),  # P3 再聚合
        declared_guarantees=(),
    )


def handle_goal_prepare(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    registry: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """goal/prepare：先校验再落库，返回 contract + admission offer。

    与 contract/prepare 区别：goal/prepare 多返回 admission（7 类信息）
    不拒接；contract/prepare 走老路径，单返合同视图。
    """
    params = envelope.params
    draft_data = params.get("draft", params)

    # 单一 validator：CLI / MCP / dataclass 三条路径在此汇合
    errors = validate_draft(draft_data)
    if errors:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="; ".join(errors),
            details={"errors": errors},
        )

    # request_id 幂等：之前已落库则原样返回（附 offer）
    if envelope.request_id and get_events_by_request_id(conn, envelope.request_id):
        cid = str(params.get("contract_id", "")).strip()
        existing = get_contract(conn, cid) if cid else None
        if existing is None:
            # 兜底：找最近一次同 request_id 的 contract_id（事件表上反查）
            from longtask.persistence.store import get_events

            events = get_events(conn)
            for ev in events:
                if ev.request_id == envelope.request_id and ev.contract_id:
                    existing = get_contract(conn, ev.contract_id)
                    if existing is not None:
                        break
        if existing is None:
            raise RpcError(
                code=ErrorCode.UNKNOWN_CONTRACT,
                message="idempotent replay: original contract not found",
            )
        return {
            "ok": True,
            "result": {
                "contract": existing.to_dict(),
                "admission": _build_admission_offer(
                    draft_dict=existing.draft.to_dict(),
                    registry_view=None,
                ).to_dict(),
            },
        }

    contract_id = str(params.get("contract_id", "")).strip()
    if not contract_id:
        date_prefix = now.strftime("%Y%m%d")
        contract_id = f"lt-{date_prefix}-{now.strftime('%H%M%S%f')[:8]}"
    elif not _is_safe_contract_id(contract_id):
        # 用户自报 ID 与 contract/prepare 同一安全约束：projections 以
        # contract_id 拼目录，任何路径片段都会写到数据根之外（安全审查 C2）。
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                f"contract_id '{contract_id}' must be a path-free slug "
                "(letters, digits, '_', '-', '.'; no separators, no '..')"
            ),
        )

    draft = from_dict(draft_data)
    actor = resolve_actor(envelope, params)
    goal_id_param = str(params.get("goal_id", "")).strip() or None
    stage_id_param = str(params.get("stage_id", "")).strip() or None
    goal_before = get_goal(conn, goal_id_param) if goal_id_param else None
    if stage_id_param:
        if goal_before is None:
            raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message="goal_id not found")
        stages = goal_before.get("plan", {}).get("stages", [])
        stage = next(
            (
                item
                for item in stages
                if isinstance(item, dict) and str(item.get("id")) == stage_id_param
            ),
            None,
        )
        if stage is None:
            raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="stage_id is not in Goal plan")
        bound = stage.get("contract_id")
        if bound and bound != contract_id:
            raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="stage already has a contract")
        required_checks = stage.get("acceptance_checks", [])
        if isinstance(required_checks, list) and required_checks:
            # Compare on a canonical identity, not on object identity: a typed
            # check is unhashable (args is a dict) and would otherwise crash
            # here instead of yielding a refusal a model caller can act on.
            actual_checks = {check_identity(check) for check in draft.acceptance.checks}
            missing = sorted({check_identity(item) for item in required_checks} - actual_checks)
            if missing:
                raise RpcError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="contract acceptance checks do not cover stage requirements",
                    details={"missing_checks": missing},
                )

    view = save_contract(
        conn,
        draft=draft,
        contract_id=contract_id,
        goal_id=(str(params["goal_id"]).strip() if params.get("goal_id") else None),
        now=now,
        request_id=envelope.request_id,
        actor=actor,
    )

    # admission offer：若调用方注入了 registry，提供候选视图
    registry_view = None
    if registry is not None and hasattr(registry, "snapshot_for_admission"):
        # P1 review fix (2026-09-08): inject running_attempts so the
        # admission snapshot reflects each executor's actual load, not
        # always running=0. Without this, snapshot_for_admission would
        # claim concurrency_available=True even for an executor already
        # saturated by an in-flight attempt.
        from longtask.persistence.attempts import count_running_by_executor

        registry_view = registry.snapshot_for_admission(
            contract=draft,
            running_attempts=count_running_by_executor(conn),
        )
    offer = _build_admission_offer(
        draft_dict=view.draft.to_dict(),
        registry_view=registry_view,
    )
    if stage_id_param and goal_before is not None:
        if goal_id_param is None:
            raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="goal_id is required")
        updated_plan = dict(goal_before.get("plan", {}))
        updated_stages = [dict(item) for item in updated_plan.get("stages", [])]
        for stage in updated_stages:
            if str(stage.get("id")) == stage_id_param:
                stage["contract_id"] = contract_id
        updated_plan["stages"] = updated_stages
        patch_goal(
            conn,
            goal_id=goal_id_param,
            now=now,
            plan=updated_plan,
            expected_revision=int(goal_before["revision"]),
            actor=actor,
        )

    # 把 admission 概要也写进 contract/prepared 事件 payload（便于审计/回放）
    from longtask.persistence.store import append_event

    append_event(
        conn,
        contract_id=contract_id,
        goal_id=view.goal_id,
        event_type=EventType.CONTRACT_PREPARED,
        payload={
            "goal_prepare": True,
            "eligible_executors": [c.executor_id for c in offer.eligible_executors],
            "rejected_executors": [c.executor_id for c in offer.rejected_executors],
        },
        now=now,
        actor=actor,
    )

    return {
        "ok": True,
        "result": {
            "contract": view.to_dict(),
            "admission": offer.to_dict(),
        },
    }


def handle_goal_admission_check(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    registry: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """goal/admission_check：只读——对已存在合同重算 admission offer。

    用于模型侧在续接时再次确认"现在还能不能跑"。不修改合同。
    """
    params = envelope.params
    contract_id = str(params.get("contract_id", "")).strip()
    if not contract_id:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="contract_id is required")

    existing = get_contract(conn, contract_id)
    if existing is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )

    registry_view = None
    if registry is not None and hasattr(registry, "snapshot_for_admission"):
        # P1 review fix: see the comment in handle_goal_prepare. Inject
        # the live running count so concurrency_available reflects
        # reality, not a frozen zero.
        from longtask.persistence.attempts import count_running_by_executor

        registry_view = registry.snapshot_for_admission(
            contract=existing.draft,
            running_attempts=count_running_by_executor(conn),
        )

    offer = _build_admission_offer(
        draft_dict=existing.draft.to_dict(),
        registry_view=registry_view,
    )
    return {"ok": True, "result": {"contract_id": contract_id, "admission": offer.to_dict()}}


def handle_goal_get(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a stable Goal identity and its contract history."""
    goal_id = str(envelope.params.get("goal_id", "")).strip()
    if not goal_id:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="goal_id is required")
    goal = get_goal(conn, goal_id)
    if goal is None:
        raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message=f"goal {goal_id} not found")
    return {"ok": True, "result": {"goal": goal}}


def handle_goal_list(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> dict[str, Any]:
    """List stable Goals independently from individual contract revisions."""
    raw_limit = envelope.params.get("limit", 20)
    if isinstance(raw_limit, (bool, float)):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="limit must be an integer",
        )
    try:
        limit = max(1, min(1000, int(raw_limit)))
    except (TypeError, ValueError):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED, message="limit must be an integer"
        ) from None
    return {"ok": True, "result": {"goals": list_goals(conn, limit=limit)}}


def handle_goal_update(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """CAS-update the long-lived Goal plan or progress document.

    Goal 计划是长期承诺的权威状态：按 ADR-004 规则 6，模型只能提议修订、
    不得直接写入——本方法收归 Principal（user）。模型应向用户陈述修订
    建议由其执行，或等待未来的「计划提案」通道（E3）。
    """
    params = envelope.params
    require_principal(envelope, params, action="goal/update")
    goal_id = str(params.get("goal_id", "")).strip()
    if not goal_id:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="goal_id is required")
    for key in ("plan", "progress"):
        if key in params and not isinstance(params[key], dict):
            raise RpcError(code=ErrorCode.VALIDATION_FAILED, message=f"{key} must be an object")
    expected = params.get("revision")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="revision must be an integer")
    try:
        goal = patch_goal(
            conn,
            goal_id=goal_id,
            now=now,
            plan=params.get("plan"),
            progress=params.get("progress"),
            expected_revision=expected,
            actor=resolve_actor(envelope, params),
        )
    except Exception as exc:
        if isinstance(exc, RpcError):
            raise
        from longtask.persistence.errors import RevisionConflictError, StoreError

        if isinstance(exc, RevisionConflictError):
            raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
        if isinstance(exc, StoreError):
            raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message=str(exc)) from exc
        raise
    return {"ok": True, "result": {"goal": goal}}


def handle_goal_advance(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """Complete the current staged step using revision CAS.

    阶段完成依赖合同验收绑定（stage acceptance binding）背书，不是任意
    自批——与 contract/approve 的 Principal 决定权性质不同，不设 actor
    门禁（模型客户端可通过 MCP 正常推进已验收阶段）。
    """
    params = envelope.params
    goal_id = str(params.get("goal_id", "")).strip()
    stage_id = str(params.get("stage_id", "")).strip()
    revision = params.get("revision")
    if not goal_id or not stage_id or isinstance(revision, bool) or not isinstance(revision, int):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="goal_id, stage_id and integer revision are required",
        )
    try:
        result = advance_goal(
            conn,
            goal_id=goal_id,
            complete_stage=stage_id,
            now=now,
            expected_revision=revision,
            actor=resolve_actor(envelope, params),
        )
    except ValueError as exc:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message=str(exc)) from exc
    except Exception as exc:
        from longtask.persistence.errors import RevisionConflictError, StoreError

        if isinstance(exc, RevisionConflictError):
            raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
        if isinstance(exc, StoreError):
            raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message=str(exc)) from exc
        raise
    return {"ok": True, "result": {"goal": result}}


def handle_goal_next(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return the next safe model action without mutating Goal state."""
    goal_id = str(envelope.params.get("goal_id", "")).strip()
    if not goal_id:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="goal_id is required")
    try:
        return {"ok": True, "result": {"next": goal_next_action(conn, goal_id=goal_id)}}
    except Exception as exc:
        from longtask.persistence.errors import StoreError

        if isinstance(exc, StoreError):
            raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message=str(exc)) from exc
        raise


def handle_goal_contract_draft(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return a draft for the current Goal stage without creating a contract."""
    goal_id = str(envelope.params.get("goal_id", "")).strip()
    stage_id = envelope.params.get("stage_id")
    if not goal_id:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="goal_id is required")
    try:
        result = goal_contract_draft(
            conn,
            goal_id=goal_id,
            stage_id=str(stage_id).strip() if stage_id is not None else None,
        )
    except ValueError as exc:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message=str(exc)) from exc
    except Exception as exc:
        from longtask.persistence.errors import StoreError

        if isinstance(exc, StoreError):
            raise RpcError(code=ErrorCode.UNKNOWN_CONTRACT, message=str(exc)) from exc
        raise
    return {"ok": True, "result": result}


__all__ = [
    "_parse_iso",
    "handle_goal_admission_check",
    "handle_goal_advance",
    "handle_goal_contract_draft",
    "handle_goal_get",
    "handle_goal_list",
    "handle_goal_next",
    "handle_goal_prepare",
    "handle_goal_update",
]
