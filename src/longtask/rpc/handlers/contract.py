"""contract/* 方法 handler：合同生命周期控制面（DESIGN §4、§5、§11.2）。

9 个方法覆盖完整生命周期：
- contract/prepare    起草（drafted）
- contract/approve    批准（drafted → active）
- contract/get        查单份
- contract/list       分页查
- contract/patch      修订可改字段
- contract/pause      暂停（active → paused）
- contract/resume     恢复（paused / blocked → active）
- contract/cancel     用户主动终止
- contract/arbitrate  人工裁决（expired / blocked → complete/archived/active）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lhgp.acceptance.checks import parse_check
from lhgp.contracts.contract_view import AcceptanceStatus, DeadlineStatus
from longtask.contracts.schema import (
    FROZEN_FIELDS,
    Acceptance,
    ContractState,
)
from longtask.contracts.state_machine import (
    is_terminal_state,
    is_valid_transition,
)
from longtask.persistence.attempts import list_contract_attempts
from longtask.persistence.decisions import list_decisions
from longtask.persistence.events import EventType
from longtask.persistence.events_query import (
    get_events,
    get_latest_forecast_snapshot,
    get_recent_events,
)
from longtask.persistence.store import (
    STORE_SCHEMA_VERSION,
    IdempotencyMismatchError,
    RevisionConflictError,
    StoreError,
    StoreTamperedError,
    append_event,
    get_contract,
    list_contracts,
    patch_contract,
    save_contract,
    update_contract_state,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.handlers._common import (
    _CONTRACT_ID_RE,
    idempotent_replay,
    parse_contract_draft,
    require_contract_id,
    require_principal,
    resolve_actor,
)

if TYPE_CHECKING:
    from longtask.rpc.server import RequestEnvelope


def handle_contract_prepare(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """起草/创建合同（DESIGN §4、§5、§11.2、§11.6）。"""
    params = envelope.params
    draft = parse_contract_draft(params, envelope=envelope)

    contract_id = str(params.get("contract_id", "")).strip()
    if not contract_id:
        date_prefix = now.strftime("%Y%m%d")
        contract_id = f"lt-{date_prefix}-{now.strftime('%H%M%S%f')[:8]}"
    elif not _CONTRACT_ID_RE.match(contract_id):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(f"contract_id '{contract_id}' violates format {_CONTRACT_ID_RE.pattern!r}"),
        )

    actor = resolve_actor(envelope, params)
    goal_id_param = str(params.get("goal_id") or "").strip() or None
    try:
        view = save_contract(
            conn,
            draft=draft,
            contract_id=contract_id,
            now=now,
            request_id=envelope.request_id,
            actor=actor,
            goal_id=goal_id_param,
        )
    except StoreTamperedError as exc:
        raise RpcError(code=ErrorCode.STORE_TAMPERED, message=str(exc)) from exc
    except IdempotencyMismatchError as exc:
        raise RpcError(code=ErrorCode.IDEMPOTENCY_REPLAY_MISMATCH, message=str(exc)) from exc
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc

    return {"ok": True, "result": view.to_dict()}


def handle_contract_approve(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """用户批准合同：drafted → active（DESIGN §5、§11.2）。"""
    params = envelope.params
    contract_id = require_contract_id(params)
    require_principal(envelope, params, action="contract/approve")

    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    expected_revision = _coerce_int(params.get("expected_revision"), "expected_revision")

    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if (
        not is_valid_transition(current.state, ContractState.ACTIVE)
        or current.state != ContractState.DRAFTED
    ):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"cannot approve contract in state '{current.state.value}'; "
                "approve is only valid from 'drafted'"
            ),
        )

    actor = resolve_actor(envelope, params)
    try:
        updated = update_contract_state(
            conn,
            contract_id=contract_id,
            new_state=ContractState.ACTIVE,
            now=now,
            expected_revision=expected_revision,
            request_id=envelope.request_id,
            actor=actor,
        )
    except RevisionConflictError as exc:
        raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    return {"ok": True, "result": updated.to_dict()}


# Daemon-class actor IDs that are allowed to call auto-approve.
# Anything else (model, executor, verifier) is rejected with
# AUTH_FAILED so a compromised client cannot promote its own
# contract by routing through the daemon-side endpoint.
_AUTO_APPROVE_ALLOWED_CLIENTS: frozenset[str] = frozenset({"daemon", "daemon-wakeup", "system"})


def handle_contract_auto_approve(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """Daemon-driven DRAFTED → ACTIVE promotion (no Principal gate).

    The user pre-authorised the contract at submit time
    (``auto_approve.enabled=True``); this endpoint is the
    daemon's executor of that pre-authorisation.  Only the
    daemon-class client is allowed (``_AUTO_APPROVE_ALLOWED_CLIENTS``);
    a model client cannot promote its own contract.
    """
    contract_id = require_contract_id(envelope.params)
    if envelope.client_id not in _AUTO_APPROVE_ALLOWED_CLIENTS:
        raise RpcError(
            code=ErrorCode.AUTH_FAILED,
            message=(
                f"contract/auto-approve requires a daemon-class client; "
                f"got client_id={envelope.client_id!r}"
            ),
        )
    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay
    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if current.state != ContractState.DRAFTED:
        # Already promoted or in a terminal state — not an error,
        # the tick will just skip it.
        return {
            "ok": True,
            "result": {
                "contract_id": contract_id,
                "skipped": True,
                "reason": f"state is {current.state.value!r}, not drafted",
            },
        }
    if not current.draft.auto_approve.enabled:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                f"contract {contract_id} has auto_approve.enabled=False; "
                "auto-approve is only for pre-authorised contracts"
            ),
        )
    actor = resolve_actor(envelope, envelope.params)
    # Revision CAS (5th-round review): pass the current revision so
    # ``update_contract_state`` rejects any concurrent writer that
    # has already bumped the revision.  Without this, two
    # concurrent daemon clients racing on the same DRAFTED contract
    # both succeed, producing duplicate CONTRACT_APPROVED events
    # and a non-monotonic revision history.
    try:
        updated = update_contract_state(
            conn,
            contract_id=contract_id,
            new_state=ContractState.ACTIVE,
            now=now,
            expected_revision=int(current.revision),
            request_id=envelope.request_id,
            actor=actor,
        )
    except RevisionConflictError:
        # Another daemon client raced us to the promotion.  The
        # contract is already ACTIVE; surface a state-skipped
        # response so the caller can move on.
        return {
            "ok": True,
            "result": {
                "contract_id": contract_id,
                "skipped": True,
                "reason": "lost CAS race; another writer already promoted",
            },
        }
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    return {"ok": True, "result": updated.to_dict()}


def handle_contract_get(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """查询单份合同权威视图（DESIGN §11.2、§11.6）。"""
    contract_id = require_contract_id(envelope.params)
    contract = get_contract(conn, contract_id)
    if contract is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    # 决策历史属于合同的可审计风险上下文；按 contract_id 精确隔离，
    # 避免同一 Goal 下不同合同的升级记录串线。
    result = contract.to_dict()
    raw_limit = envelope.params.get("decision_limit", 50)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="decision_limit must be an integer",
        )
    decision_limit = raw_limit
    if not 1 <= decision_limit <= 200:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="decision_limit must be between 1 and 200",
        )
    raw_attempt_limit = envelope.params.get("attempt_limit", 20)
    if isinstance(raw_attempt_limit, bool) or not isinstance(raw_attempt_limit, int):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="attempt_limit must be an integer",
        )
    attempt_limit = raw_attempt_limit
    if not 1 <= attempt_limit <= 100:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="attempt_limit must be between 1 and 100",
        )
    result["decision_history"] = list_decisions(
        conn,
        contract_id=contract_id,
        limit=decision_limit,
    )
    # Deadline 风险是合同级模型可见性的一部分；不要要求调用方再读取
    # 全量事件流才能知道当前快照。只暴露本合同最新的协议生成快照。
    result["deadline_snapshot"] = get_latest_forecast_snapshot(conn, contract_id=contract_id)
    result["attempt_history"] = [
        {
            "attempt_id": attempt.attempt_id,
            "contract_id": attempt.contract_id,
            "contract_revision": attempt.contract_revision,
            "role": attempt.role,
            "executor_id": attempt.executor_id,
            "model_id": attempt.model_id,
            "state": attempt.state,
            "admitted_at": attempt.admitted_at.isoformat(),
            "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
            "terminal_at": attempt.terminal_at.isoformat() if attempt.terminal_at else None,
            "return_code": attempt.return_code,
            "error_class": attempt.error_class,
            "external_run_id": attempt.external_run_id,
            "recovery_strategy": attempt.recovery_strategy,
        }
        for attempt in list_contract_attempts(conn, contract_id=contract_id, limit=attempt_limit)
    ]
    verification_events = {
        EventType.VERIFICATION_REQUESTED,
        EventType.VERIFICATION_CONSUMED,
        EventType.VERIFICATION_STARTED,
    }
    verification_history: list[dict[str, Any]] = []
    for event in reversed(get_recent_events(conn, contract_id=contract_id, limit=20)):
        if event.event_type not in verification_events:
            continue
        try:
            payload = json.loads(event.payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        verification_history.append(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "attempt_id": event.attempt_id,
                "contract_revision": event.contract_revision,
                "actor": event.actor,
                "created_at": event.created_at.isoformat(),
                "payload": payload,
            }
        )
        if len(verification_history) >= 20:
            break
    result["verification_history"] = verification_history
    return {"ok": True, "result": result}


def handle_contract_list(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """分页查询合同列表（DESIGN §11.2、§11.6）。"""
    params = envelope.params
    filter_state: ContractState | None = None
    raw_state = params.get("state")
    if raw_state is not None:
        try:
            filter_state = ContractState(str(raw_state))
        except ValueError as exc:
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message=f"unknown contract state '{raw_state}'",
            ) from exc

    after_contract_id: str | None = params.get("cursor", params.get("after_contract_id"))
    limit = _coerce_int(params.get("limit", 20), "limit")
    if limit is None or not 1 <= limit <= 200:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="limit must be between 1 and 200",
        )

    try:
        contracts = list_contracts(
            conn,
            state=filter_state,
            after_contract_id=after_contract_id,
            limit=limit,
        )
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc

    contract_rows: list[dict[str, Any]] = []
    for contract in contracts:
        row = contract.to_dict()
        row["deadline_snapshot"] = get_latest_forecast_snapshot(
            conn, contract_id=contract.contract_id
        )
        contract_rows.append(row)

    return {
        "ok": True,
        "result": {
            "contracts": contract_rows,
            "next_cursor": contracts[-1].contract_id if contracts else after_contract_id,
            "has_more": len(contracts) == limit,
        },
    }


def handle_contract_patch(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """修订合同可修改字段（DESIGN §4、§11.2、§11.7）。

    仅允许修改 soft_guidance / acceptance / workload_initial_hours；
    冻结区字段禁止修改；终态合同禁止修订。
    """
    params = envelope.params
    contract_id = require_contract_id(params)
    # 合同修订是 Principal 决定权（安全审查 RPC-C1 的遗漏面）：模型客户端
    # 不得改写任何合同字段——即使冻结区有 blocklist，软区字段（如验收
    # standard）被模型单方面改写同样破坏「合同共同维护」语义。
    require_principal(envelope, params, action="contract/patch")

    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    expected_revision = _coerce_int(params.get("expected_revision"), "expected_revision")
    if expected_revision is None:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="expected_revision is required for contract/patch",
        )

    patch_data: dict[str, Any] = params.get("patch", {})
    if not patch_data:
        patch_data = {
            k: v
            for k, v in params.items()
            if k not in ("contract_id", "expected_revision", "actor")
        }

    forbidden_frozen = set(patch_data.keys()) & FROZEN_FIELDS
    if forbidden_frozen:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"cannot modify frozen fields in patch: {sorted(forbidden_frozen)}",
            details={"frozen_fields": list(forbidden_frozen)},
        )

    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if is_terminal_state(current.state):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=f"cannot patch contract in terminal state '{current.state.value}'",
        )

    soft_guidance: dict[str, Any] | None = (
        dict(patch_data["soft_guidance"]) if "soft_guidance" in patch_data else None
    )
    acceptance: Acceptance | None = None
    if "acceptance" in patch_data:
        acc_raw = patch_data["acceptance"]
        try:
            # 3rd-round review (2026-09-08): patch 必须保留 spec /
            # spec_hash，否则验收要求在 patch 路径上静默消失，
            # plan gate 的 spec_hash 绑定随之失效。
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
        except (KeyError, TypeError) as exc:
            # 审计 RPC-R7：畸形 acceptance 之前裸抛 KeyError → INTERNAL，
            # 现在映射为可处理的校验错误。
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message=f"malformed acceptance patch: {exc}",
            ) from exc
        acc_errors = acceptance.validate()
        if acc_errors:
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message="; ".join(acc_errors),
                details={"errors": acc_errors},
            )

    workload_initial_hours: float | None = None
    if "workload_estimate" in patch_data and isinstance(patch_data["workload_estimate"], dict):
        workload_initial_hours = float(patch_data["workload_estimate"]["initial_hours"])
    elif "workload_initial_hours" in patch_data:
        workload_initial_hours = float(patch_data["workload_initial_hours"])

    if workload_initial_hours is not None and workload_initial_hours <= 0:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="workload_initial_hours must be positive",
        )

    actor = resolve_actor(envelope, params)
    try:
        updated = patch_contract(
            conn,
            contract_id=contract_id,
            expected_revision=expected_revision,
            now=now,
            soft_guidance=soft_guidance,
            acceptance=acceptance,
            workload_initial_hours=workload_initial_hours,
            request_id=envelope.request_id,
            actor=actor,
        )
    except RevisionConflictError as exc:
        raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    return {"ok": True, "result": updated.to_dict()}


def handle_contract_request_verification(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """用户直接请求验收（SPEC §12.4）：不派 executor，只请求 verifier。

    典型场景：执行预算耗尽（blocked need-user）但交付物疑似已就绪。
    handler 只做校验与事件落库；daemon tick 消费 verification/requested
    事件派生 verifier（RPC handler 没有进程表，spawn 必须由 daemon 的
    AttemptRunner 做——与 control/interrupt 相同的「写事件-消费」分工）。
    """
    params = envelope.params
    contract_id = require_contract_id(params)
    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if is_terminal_state(current.state):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=f"contract {contract_id} is terminal ({current.state.value})",
        )
    # A request that is durably queued but not yet consumed already represents
    # the user's intent.  Reject a second request before it can spend another
    # verification reservation; a fresh request is allowed after consumption.
    verification_events = get_events(conn, contract_id=contract_id)
    consumed_request_ids: set[int] = set()
    for event in verification_events:
        if event.event_type != EventType.VERIFICATION_CONSUMED:
            continue
        try:
            consumed_request_ids.add(
                int(json.loads(event.payload_json or "{}")["request_event_id"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    pending_request = next(
        (
            event
            for event in verification_events
            if event.event_type == EventType.VERIFICATION_REQUESTED
            and event.event_id not in consumed_request_ids
        ),
        None,
    )
    if pending_request is not None:
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"verification request {pending_request.event_id} already pending; "
                "wait for daemon consumption before requesting another"
            ),
        )
    # 进行中的 verifier attempt：重复请求无意义
    running_verifier = conn.execute(
        "SELECT attempt_id FROM attempts "
        "WHERE goal_id = ? AND role = 'verifier' "
        "AND state NOT IN ('succeeded', 'failed', 'cancelled', 'stale', 'orphaned') "
        "LIMIT 1",
        (current.goal_id,),
    ).fetchone()
    if running_verifier is not None:
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"verifier attempt {running_verifier[0]} already in progress; "
                "wait for its verdict before requesting another"
            ),
        )
    # 验证预算（§12.4 独立记账）：耗尽时如实拒绝并说明升级路径
    from longtask.promoter.records import _count_verifier_attempts

    reserved = current.draft.budget.verification_attempts_reserved
    used = _count_verifier_attempts(conn, contract_id)
    if used >= reserved:
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"verification budget exhausted: {used}/{reserved} verifier "
                "attempts used (§12.4); raise verification_attempts_reserved "
                "via a contract revision"
            ),
        )

    actor = resolve_actor(envelope, params)
    # blocked → active：verifier 派生要求 ACTIVE 态；升级历史保留在事件流
    if current.state == ContractState.BLOCKED:
        try:
            update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=ContractState.ACTIVE,
                now=now,
                event_type=EventType.CONTRACT_RESUMED,
                event_payload={"reason": "user requested verification (§12.4)"},
                actor=actor,
            )
        except StoreError as exc:
            raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
        # 状态迁移会产生新的不可变合同 revision；验收请求必须绑定最新版本。
        refreshed = get_contract(conn, contract_id)
        if refreshed is None:
            raise RpcError(code=ErrorCode.INTERNAL, message="contract disappeared after resume")
        current = refreshed
    append_event(
        conn,
        contract_id=contract_id,
        goal_id=current.goal_id,
        event_type=EventType.VERIFICATION_REQUESTED,
        payload={
            "requested_by": actor,
            "reason": str(params.get("reason") or f"{actor} requested verification"),
            "budget_used": used,
            "budget_reserved": reserved,
        },
        now=now,
        actor=actor,
        contract_revision=current.revision,
        role=actor,
        payload_schema_version=STORE_SCHEMA_VERSION,
    )
    return {
        "ok": True,
        "result": {
            "contract_id": contract_id,
            "verification_requested": True,
            "note": "daemon will dispatch an independent verifier on its next tick",
        },
    }


def handle_contract_user_confirm(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """User confirms a CANDIDATE spec (Principal-gated, full-completion path).

    A contract whose acceptance spec has a ``judge == "user"``
    criterion transitions to ``acceptance_status = CANDIDATE``
    after a verifier pass — the dispatcher deliberately skips
    it until the user signs off (no executor is allowed to mark
    the user criterion resolved).  This tool is the only path
    that moves ``CANDIDATE → PASSED`` and triggers full contract
    completion: the user-confirm is treated as the *final*
    acceptance step, not just a status flip.

    Defense-in-depth (4th-round review 2026-09-08): the
    Principal gate runs here, not in the MCP tool wrapper.  The
    wrapper may not have a real envelope (e.g. legacy stdio
    model clients), and trusting the wrapper to gate is a known
    bypass.  A model client whose synthetic envelope has
    ``client_id="mcp"`` will be rejected with AUTH_FAILED before
    the CANDIDATE→PASSED transition.

    After resolving the user, the handler replays the verifier
    success path: transition to ``COMPLETE`` with
    ``acceptance_status=PASSED`` (which also writes the
    ``CONTRACT_COMPLETED`` event via ``_STATE_TO_EVENT``), and
    advance the bound Goal stage so the next contract is
    generated.  The old version only flipped the status and
    left the contract ACTIVE — the dispatcher immediately
    re-dispatched, the Goal was never advanced, and the next
    contract was never created.

    5th-round review: the event append + state update + goal
    advance are one atomic transaction with a pre-read
    ``expected_revision`` CAS.  Two concurrent user_confirm
    calls would otherwise both write the ACCEPTANCE_STATUS_CHANGED
    event, then race the contract state update; only the thread
    whose ``update_contract_state`` wins the revision CAS
    commits, the other raises ``REVISION_CONFLICT`` and the
    outer transaction rolls its events back.
    """
    params = envelope.params
    contract_id = require_contract_id(params)
    # Principal gate runs in the handler (not the MCP wrapper)
    # so a synthetic envelope with client_id="mcp" cannot bypass.
    principal_actor = require_principal(envelope, params, action="contract/user-confirm")
    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    note = str(params.get("note") or "").strip() or None
    # 5th-round review: the event append + state update +
    # goal advance are one atomic transaction with a
    # pre-read ``expected_revision`` CAS.  Two concurrent
    # user_confirm calls would otherwise both write
    # ACCEPTANCE_STATUS_CHANGED, then race the contract
    # state update; only the thread whose
    # ``update_contract_state`` wins the revision CAS commits,
    # the other raises ``REVISION_CONFLICT`` and the outer
    # transaction rolls its events back.  ``BEGIN IMMEDIATE``
    # inside ``transaction()`` serializes the write lock.
    from longtask.persistence.store import (
        RevisionConflictError,
        advance_goal_after_verified_contract,
        transaction,
    )
    from longtask.rpc.handlers._lifecycle import auto_create_next_stage_contract

    # Pre-transaction snapshot.  ``current.revision`` here is
    # the CAS baseline that ``update_contract_state`` will check
    # against.  Reading it OUTSIDE the transaction is what makes
    # the CAS span the BEGIN IMMEDIATE boundary: two concurrent
    # threads both read the same baseline, but only one wins the
    # write lock and the other's expected_revision check fails.
    pre_current = get_contract(conn, contract_id)
    if pre_current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if pre_current.acceptance_status != AcceptanceStatus.CANDIDATE:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                f"contract {contract_id} is in {pre_current.acceptance_status.value!r}; "
                "user-confirm is only valid for CANDIDATE (verifier passed with a "
                "user criterion pending)"
            ),
        )
    expected_revision = int(pre_current.revision)
    pre_acceptance = pre_current.acceptance_status

    # Evidence lookup + refusal check runs OUTSIDE the
    # try/transaction so the raise itself is not entangled
    # with the transaction context manager's ``__exit__``
    # attribute setting on a slots/frozen RpcError.
    # Python 3.13 + slots + frozen dataclass + Exception
    # base class has a known quirk where raising
    # RpcError through a ``with`` block triggers an
    # extra super() chain in traceback assembly that
    # fails with "obj is not an instance or subtype of
    # type".  Keeping the raise outside the try block
    # sidesteps the issue.
    verifier_evidence = _latest_verifier_evidence(conn, contract_id, expected_revision)
    verifier_evidence = _latest_verifier_evidence(conn, contract_id, expected_revision)
    # 5th-round P1 regression fix: when the user_confirm
    # path closes the contract, the
    # CONTRACT_COMPLETED event must carry meaningful
    # evidence.  The previous implementation passed
    # ``evidence={}`` whenever no verifier attempt had
    # recorded ATTEMPT_SUCCEEDED (the common case for
    # hand-rolled fixtures, and any flow that bypasses
    # the verifier on the way to CANDIDATE).
    #
    # 7th-round P1 refinement: distinguish "no event at
    # all" from "event exists but content is stale"
    # (the reviewer's P1 repro: user edits
    # ``acceptance`` after a verifier pass — the
    # CANDIDATE state is preserved but the verifier
    # evidence is invalidated by the spec_hash
    # mismatch).  Synthesising a stub for the stale
    # case is unsafe: it lets a contract land in
    # COMPLETE without a fresh check.  The
    # ``stale`` flag carries that distinction; when
    # the call is at the CANDIDATE pre-state (the
    # only user_confirm entry), a stale result is a
    # hard fail — the user must rerun verification
    # instead of confirming a stale pass.
    verifier_matched = bool(verifier_evidence.get("matched"))
    verifier_stale = bool(verifier_evidence.get("stale"))
    verifier_no_event = bool(verifier_evidence.get("no_event"))
    verifier_attempt_id = verifier_evidence.get("attempt_id")
    verifier_payload = verifier_evidence.get("payload") or {}
    if not verifier_matched and verifier_stale and pre_acceptance == AcceptanceStatus.CANDIDATE:
        # CANDIDATE + stale evidence = the reviewer's
        # P1 repro: a verifier pass put the contract
        # in CANDIDATE, the user then edited
        # ``acceptance``, the CANDIDATE state
        # survived the edit but the verifier
        # evidence no longer matches the new spec.
        # Refuse to confirm on stale evidence — the
        # user must re-run verification.
        # Build the RpcError outside the helper so
        # ``super(Exception, instance)`` chain is not
        # exercised inside a context manager (Python
        # 3.13 + slots + frozen dataclass + Exception
        # base class has a known quirk where raising
        # RpcError through a ``with`` block triggers
        # an extra super() chain in traceback assembly
        # that fails with "obj is not an instance or
        # subtype of type").
        refusal = RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"contract {contract_id} has no valid verifier "
                "evidence (acceptance was edited after the last "
                "verifier pass); re-run verification before "
                "user_confirm"
            ),
        )
        raise refusal
    if not verifier_matched and verifier_no_event:
        # Hand-rolled fixture / pre-CANDIDATE path:
        # synthesise a stub so the audit log and
        # the next-stage ``previous_evidence`` still
        # have something concrete to point at.
        verifier_attempt_id = f"user-confirm:{principal_actor}"
        verifier_payload = {
            "source": "user-confirm",
            "resolved_principal": principal_actor,
            "from_status": pre_acceptance.value,
            "to_status": AcceptanceStatus.PASSED.value,
            "note": note,
            "confirmed_at": now.isoformat(),
        }

    try:
        with transaction(conn):
            append_event(
                conn,
                contract_id=contract_id,
                event_type=EventType.ACCEPTANCE_STATUS_CHANGED,
                payload={
                    "reason": "user-confirmed",
                    "from_status": pre_acceptance.value,
                    "to_status": AcceptanceStatus.PASSED.value,
                    "note": note,
                },
                now=now,
                actor=principal_actor,
            )
            # CONTRACT_COMPLETED is written by ``update_contract_state``
            # (COMPLETE → CONTRACT_COMPLETED via ``_STATE_TO_EVENT``);
            # we merge the verifier evidence into its ``event_payload``
            # so the same event carries the user-confirm provenance
            # without producing a second CONTRACT_COMPLETED row.
            completed_event_payload: dict[str, Any] = {
                "verifier": verifier_attempt_id,
                "evidence": verifier_payload,
                "user_confirmed": True,
            }
            updated = update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=ContractState.COMPLETE,
                now=now,
                expected_revision=expected_revision,
                acceptance_status=AcceptanceStatus.PASSED,
                deadline_status=(
                    DeadlineStatus.MET
                    if now <= pre_current.draft.deadline_at
                    else DeadlineStatus.MISSED
                ),
                event_payload=completed_event_payload,
                actor=principal_actor,
            )
            advance_goal_after_verified_contract(conn, updated, now)
            # User-confirm closes the contract and advances the
            # Goal; if the next stage has no bound contract, create
            # it now so the dispatcher can pick it up on the next
            # tick (mirrors the verifier-driven path in the daemon
            # tick — both must funnel through this helper so
            # user-confirm and verifier-passed produce the same
            # next-stage contract).  Best-effort: any failure here
            # is silent, ``goal/next`` will surface
            # ``create_contract`` so the caller can re-attempt.
            #
            # 5th-round P1 regression: the user-confirm evidence
            # (principal + note + transition) is forwarded to the
            # next stage as ``previous_evidence`` so the new
            # contract's executor has a concrete reference to the
            # stage that produced it.  Previously the field was
            # always ``None`` here, leaving the next stage with no
            # trace of the user-confirm.
            auto_create_next_stage_contract(
                conn,
                updated,
                now,
                previous_evidence={
                    "source": "user-confirm",
                    "contract_id": contract_id,
                    "resolved_principal": principal_actor,
                    "note": note,
                    "completed_at": now.isoformat(),
                    "from_status": pre_acceptance.value,
                    "to_status": AcceptanceStatus.PASSED.value,
                },
            )
    except RevisionConflictError as exc:
        # The CAS rejected the state update.  The transaction
        # rolled back any events we appended; the contract view
        # is the same as before the call.
        raise RpcError(
            code=ErrorCode.REVISION_CONFLICT,
            message=str(exc),
        ) from exc
    except StoreError as exc:
        raise RpcError(
            code=ErrorCode.INTERNAL,
            message=f"failed to mark contract complete: {exc}",
        ) from exc
    return {
        "contract_id": contract_id,
        "user_confirmed": True,
        "acceptance_status": AcceptanceStatus.PASSED.value,
        "contract_state": ContractState.COMPLETE.value,
        "note": note,
    }


def _latest_verifier_evidence(
    conn: sqlite3.Connection, contract_id: str, revision: int
) -> dict[str, Any]:
    """Return the most recent matching verifier success evidence.

    Used by :func:`handle_contract_user_confirm`.  Returns a
    dict with four keys:

    - ``attempt_id`` / ``payload``: the verifier event that
      ran against the contract's current ``acceptance.spec_hash``
    - ``matched`` (bool): True when a content-bound match
      was found.
    - ``stale`` (bool): True when at least one verifier
      event exists but its ``spec_hash`` differs from the
      current ``acceptance.spec_hash`` — the evidence is
      from a prior acceptance version.  ``stale`` lets
      the caller refuse the confirm on the audit-trail
      grounds that the verifier ran against a different
      spec; without this signal the synthesised-evidence
      branch would happily complete the contract.
    - ``no_event`` (bool): True when no verifier event
      exists at all (hand-rolled fixtures that stage
      the contract directly into CANDIDATE without a
      verifier run).  Caller is free to synthesise a
      stub in this case — the audit log is the only
      signal, the verifier did not actually run, and the
      test author owns the consequences.

    7th-round P1 fix: returning just the event is no
    longer enough — the caller must distinguish "no
    event at all" from "event exists but content is
    stale", because the appropriate next step differs.
    """
    from longtask.persistence.events_query import get_events
    from longtask.persistence.store import get_contract

    contract = get_contract(conn, contract_id)
    current_spec_hash = contract.draft.acceptance.spec_hash if contract is not None else None

    no_event = True
    for event in get_events(conn, contract_id=contract_id):
        is_verifier = event.role == "verifier" or (
            event.role is None
            and (event.payload_json or "").strip().startswith('{"role": "verifier"')
        )
        if not is_verifier:
            continue
        if str(event.event_type) != EventType.ATTEMPT_SUCCEEDED.value:
            continue
        no_event = False
        try:
            payload = json.loads(event.payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        # Content binding: a verifier event whose spec_hash
        # differs from the contract's current spec_hash is
        # from a prior acceptance version — flag it stale
        # and skip.
        event_spec_hash = payload.get("spec_hash")
        if (
            current_spec_hash is not None
            and event_spec_hash is not None
            and event_spec_hash != current_spec_hash
        ):
            continue
        return {
            "attempt_id": event.attempt_id,
            "payload": payload,
            "matched": True,
            "stale": False,
            "no_event": False,
        }
    return {
        "attempt_id": None,
        "payload": {},
        "matched": False,
        "stale": not no_event,
        "no_event": no_event,
    }


def handle_contract_pause(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """用户主动暂停：active → paused（DESIGN §5、§11.2）。"""
    params = envelope.params
    contract_id = require_contract_id(params)
    require_principal(envelope, params, action="contract/pause")

    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    expected_revision = _coerce_int(params.get("expected_revision"), "expected_revision")
    reason = params.get("reason")

    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if not is_valid_transition(current.state, ContractState.PAUSED):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"cannot pause contract in state '{current.state.value}'; "
                "pause is only valid from 'active'"
            ),
        )

    actor = resolve_actor(envelope, params)
    event_payload = {"reason": str(reason)} if reason else None
    try:
        updated = update_contract_state(
            conn,
            contract_id=contract_id,
            new_state=ContractState.PAUSED,
            now=now,
            expected_revision=expected_revision,
            event_type=EventType.CONTRACT_PAUSED,
            event_payload=event_payload,
            request_id=envelope.request_id,
            actor=actor,
        )
    except RevisionConflictError as exc:
        raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    return {"ok": True, "result": updated.to_dict()}


def handle_contract_resume(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """恢复执行：paused / blocked → active（DESIGN §5、§11.2）。"""
    params = envelope.params
    contract_id = require_contract_id(params)
    require_principal(envelope, params, action="contract/resume")

    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    expected_revision = _coerce_int(params.get("expected_revision"), "expected_revision")
    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if current.state not in (
        ContractState.PAUSED,
        ContractState.BLOCKED,
    ) or not is_valid_transition(current.state, ContractState.ACTIVE):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"cannot resume contract in state '{current.state.value}'; "
                "resume is only valid from 'paused' or 'blocked'"
            ),
        )

    actor = resolve_actor(envelope, params)
    try:
        updated = update_contract_state(
            conn,
            contract_id=contract_id,
            new_state=ContractState.ACTIVE,
            now=now,
            expected_revision=expected_revision,
            event_type=EventType.CONTRACT_RESUMED,
            request_id=envelope.request_id,
            actor=actor,
        )
    except RevisionConflictError as exc:
        raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    return {"ok": True, "result": updated.to_dict()}


def handle_contract_cancel(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """用户主动终止：任意非终态 → cancelled（DESIGN §5、§11.2）。"""
    params = envelope.params
    contract_id = require_contract_id(params)
    require_principal(envelope, params, action="contract/cancel")

    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    expected_revision = _coerce_int(params.get("expected_revision"), "expected_revision")
    reason = params.get("reason")

    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if is_terminal_state(current.state) or not is_valid_transition(
        current.state, ContractState.CANCELLED
    ):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"cannot cancel contract in terminal state '{current.state.value}'; "
                "cancel is only allowed from non-terminal states"
            ),
        )

    actor = resolve_actor(envelope, params)
    event_payload = {"reason": str(reason)} if reason else None
    try:
        updated = update_contract_state(
            conn,
            contract_id=contract_id,
            new_state=ContractState.CANCELLED,
            now=now,
            expected_revision=expected_revision,
            event_type=EventType.CONTRACT_CANCELLED,
            event_payload=event_payload,
            request_id=envelope.request_id,
            actor=actor,
        )
    except RevisionConflictError as exc:
        raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    return {"ok": True, "result": updated.to_dict()}


_ARBITRATION_DECISIONS: dict[str, ContractState] = {
    "complete": ContractState.COMPLETE,
    "accepted": ContractState.COMPLETE,
    "archived": ContractState.ARCHIVED,
    "discard": ContractState.ARCHIVED,
    "active": ContractState.ACTIVE,
    "extend": ContractState.ACTIVE,
    "resume": ContractState.ACTIVE,
}


def handle_contract_arbitrate(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """Deadline / blocked / expired 人工裁决（DESIGN §5、§11.2、§11.5 时序 C）。

    支持裁决目标：complete（采纳部分成果）、archived（作废）、active（延期续跑）。
    """
    params = envelope.params
    contract_id = require_contract_id(params)
    require_principal(envelope, params, action="contract/arbitrate")

    if (replay := idempotent_replay(conn, envelope, contract_id)) is not None:
        return replay

    decision_raw = str(params.get("decision", params.get("target_state", ""))).strip().lower()
    if decision_raw not in _ARBITRATION_DECISIONS:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=(
                f"invalid arbitration decision '{decision_raw}'; "
                "must be one of: complete, archived, active "
                "(or alias: accepted, discard, extend, resume)"
            ),
        )
    target_state = _ARBITRATION_DECISIONS[decision_raw]
    expected_revision = _coerce_int(params.get("expected_revision"), "expected_revision")
    note = params.get("note", params.get("reason"))

    current = get_contract(conn, contract_id)
    if current is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    if current.state not in (ContractState.EXPIRED, ContractState.BLOCKED):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"cannot arbitrate contract in state '{current.state.value}'; "
                "arbitrate is only allowed for 'expired' or 'blocked' contracts"
            ),
        )
    if not is_valid_transition(current.state, target_state):
        raise RpcError(
            code=ErrorCode.STATE_FORBIDDEN,
            message=(
                f"invalid arbitration transition from '{current.state.value}' "
                f"to '{target_state.value}'"
            ),
        )

    actor = resolve_actor(envelope, params)
    event_payload: dict[str, Any] = {
        "decision": decision_raw,
        "target_state": target_state.value,
    }
    if note:
        event_payload["note"] = str(note)

    try:
        updated = update_contract_state(
            conn,
            contract_id=contract_id,
            new_state=target_state,
            now=now,
            expected_revision=expected_revision,
            event_type=EventType.CONTRACT_ARBITRATED,
            event_payload=event_payload,
            request_id=envelope.request_id,
            actor=actor,
        )
    except RevisionConflictError as exc:
        raise RpcError(code=ErrorCode.REVISION_CONFLICT, message=str(exc)) from exc
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    return {"ok": True, "result": updated.to_dict()}


def _coerce_int(raw: Any, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (bool, float)):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"{name} must be an integer (boolean is not accepted)",
        )
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"{name} must be an integer: {exc}",
        ) from exc


__all__ = [
    "handle_contract_approve",
    "handle_contract_arbitrate",
    "handle_contract_cancel",
    "handle_contract_get",
    "handle_contract_list",
    "handle_contract_patch",
    "handle_contract_pause",
    "handle_contract_prepare",
    "handle_contract_request_verification",
    "handle_contract_resume",
]
