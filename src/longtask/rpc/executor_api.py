"""执行者侧 RPC（DESIGN §11.2）：被拉起的会话对协议「有手有嘴」。

三个方法给执行中的 attempt 主动协作通道（此前只能靠文件 + 被动观察）：
- attempt/status：读自己的 attempt 事件史与租约代次（写回前的自查）；
- lease/renew：持当前代次续心跳（会话还活着，别回收我，§7）；
- attempt/write-back：写回进度与终态（事件 + 可选合同迁移，§7
  fencing：旧代次写回 LEASE_FENCED 丢弃，§14.1）。

安全边界不变：lease_generation 与 attempt_id 必须匹配当前租约
（fencing）；合同状态迁移仍走状态机校验；request_id 幂等（重试
不产生第二个事件）。progress 心跳外的进度更新走既有事件词汇
（context/scratch-updated），不发明词汇表外的事件名。

词汇注意：Method 词表里这是 ATTEMPT_STATUS / ATTEMPT_LOGS /
LEASE_RENEW / LEASE_RELEASE 的执行者侧实现。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from longtask.contracts.authority import binding_for_executor, models_allow
from longtask.contracts.schema import ContractState
from longtask.persistence.attempts import get_attempt
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    EventInput,
    LeaseFencedError,
    append_event,
    attempt_evidence_binding,
    get_contract,
    get_events,
    get_lease,
    renew_lease,
    write_back,
)
from longtask.rpc.errors import ErrorCode, RpcError

if TYPE_CHECKING:
    from longtask.rpc.server import RequestEnvelope


def _require(params: dict[str, Any], key: str) -> str:
    value = str(params.get(key, "")).strip()
    if not value:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message=f"{key} is required")
    return value


def _strict_int(value: Any, *, field: str) -> int:
    """Reject booleans and lossy numeric coercions at the executor boundary."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"{field} must be an integer",
        )
    return cast(int, value)


def _require_contract(conn: sqlite3.Connection, contract_id: str) -> Any:
    contract = get_contract(conn, contract_id)
    if contract is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"contract {contract_id} not found",
        )
    return contract


def _require_lease(conn: sqlite3.Connection, *, contract_id: str, attempt_id: str) -> Any:
    """取租约并校验持有人：不是本 attempt 的租约按 fenced 拒绝。"""
    lease = get_lease(conn, contract_id)
    if lease is None or lease.holder_attempt_id != attempt_id:
        raise RpcError(
            code=ErrorCode.LEASE_FENCED,
            message=f"no live lease held by attempt {attempt_id} on {contract_id}",
        )
    return lease


def handle_attempt_status(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """attempt/status：执行者自查本 attempt 的事件史与租约代次。"""
    params = envelope.params
    contract_id = _require(params, "contract_id")
    attempt_id = _require(params, "attempt_id")
    _require_contract(conn, contract_id)

    # O4（工具面审计）：未知 attempt 曾返回 ok+空事件，与「尚无事件」
    # 不可区分，模型无法发现 attempt_id 拼错。fail-closed：
    # attempts 表无此行即 UNKNOWN_ATTEMPT。
    attempt_row = get_attempt(conn, attempt_id)
    if attempt_row is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_ATTEMPT,
            message=f"attempt {attempt_id} does not exist under contract {contract_id}",
        )

    contract_events = get_events(conn, contract_id=contract_id)
    events = [e for e in contract_events if e.attempt_id == attempt_id]
    lease = get_lease(conn, contract_id)
    return {
        "ok": True,
        "result": {
            "contract_id": contract_id,
            "attempt_id": attempt_id,
            "events": [
                {
                    "event_id": e.event_id,
                    "event_type": str(e.event_type),
                    "payload": e.payload_json,
                    "created_at": e.created_at.isoformat(),
                }
                for e in events
            ],
            "lease": {
                "generation": lease.generation if lease else None,
                "holder_attempt_id": lease.holder_attempt_id if lease else None,
                "is_alive": lease.is_alive(now) if lease else False,
            },
        },
    }


def handle_lease_renew(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """lease/renew：执行者持自己的代次续心跳（§7 会话存活声明）。"""
    params = envelope.params
    contract_id = _require(params, "contract_id")
    attempt_id = _require(params, "attempt_id")
    _require_contract(conn, contract_id)
    lease = _require_lease(conn, contract_id=contract_id, attempt_id=attempt_id)

    try:
        timeout_seconds = _strict_int(params.get("timeout_seconds", 1800), field="timeout_seconds")
        if timeout_seconds <= 0:
            raise ValueError
    except ValueError:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="timeout_seconds must be a positive integer",
        ) from None

    try:
        renewed = renew_lease(
            conn,
            contract_id=contract_id,
            holder_attempt_id=attempt_id,
            lease_generation=lease.generation,
            heartbeat_at=now,
            timeout=timedelta(seconds=timeout_seconds),
            request_id=envelope.request_id or None,
            actor="model",
        )
    except LeaseFencedError as exc:
        # 续约窗口内被回收/接管：如实 fenced，执行者应停写
        raise RpcError(code=ErrorCode.LEASE_FENCED, message=str(exc)) from exc
    return {
        "ok": True,
        "result": {
            "contract_id": contract_id,
            "attempt_id": attempt_id,
            "generation": renewed.generation,
            "heartbeat_at": renewed.heartbeat_at.isoformat(),
            "timeout_seconds": renewed.timeout.total_seconds(),
        },
    }


def handle_control_interrupt(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """control/interrupt：用户请求打断执行中的 attempt（DESIGN §10 可干涉）。

    写 attempt/cancelled 事件（attempt_id + reason + via=control/interrupt）；
    daemon 在下一轮 tick 顶部消费并调用 AttemptRunner.cancel_attempt。
    跨进程打断必须经盘上事件（attempt 句柄由 daemon 进程持有）；
    daemon 离线时请求持久化，下次 tick 兑现。
    """
    params = envelope.params
    contract_id = _require(params, "contract_id")
    attempt_id = _require(params, "attempt_id")
    _require_contract(conn, contract_id)
    reason = str(params.get("reason", "interrupt requested"))
    # 审计归属必须真实：interrupt 可由模型经 MCP 触发，硬编码 actor=user
    # 曾把模型动作记成用户动作（工具面审计 R3）。
    # 延迟导入：handlers._common 反向依赖本模块的 facade 链。
    from longtask.rpc.handlers._common import resolve_actor

    actor = resolve_actor(envelope, params)

    append_event(
        conn,
        contract_id=contract_id,
        attempt_id=attempt_id,
        event_type=EventType.ATTEMPT_CANCELLED,
        payload={"reason": reason, "via": "control/interrupt", "requested_by": actor},
        now=now,
        actor=actor,
        request_id=envelope.request_id or None,
    )
    return {
        "ok": True,
        "result": {
            "contract_id": contract_id,
            "attempt_id": attempt_id,
            "queued_for_daemon": True,
            "reason": reason,
        },
    }


def handle_attempt_write_back(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """attempt/write-back：写回进度与终态（§5.1、§7）。

    入参：
    - contract_id / attempt_id / write_generation：必填，fencing 三元组；
    - progress_note（可选）：本次进度要点，落 context/scratch-updated 事件
      （词汇表内，语义=该 attempt 的临时工作记忆更新）；
    - contract_state（可选）：合同迁移，走 write_back 的状态机校验；
    - attempt_state（可选，succeeded/failed）：声明本 attempt 终态，
      落对应 attempt/* 事件；未声明则只写进度。
    """
    params = envelope.params
    contract_id = _require(params, "contract_id")
    attempt_id = _require(params, "attempt_id")
    _require_contract(conn, contract_id)

    try:
        write_generation = _strict_int(params.get("write_generation", -1), field="write_generation")
    except RpcError:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED, message="write_generation must be an integer"
        ) from None
    if write_generation < 0:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="write_generation is required")

    # Per-attempt session token（安全加固）：只对 executor 客户端强制。
    # daemon 内部调用（verifier 派发、reconcile 等）走主令牌已可信。
    if envelope.client_id == "executor":
        session_token = params.get("session_token")
        if not isinstance(session_token, str) or not session_token:
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message="session_token is required (injected as LHGP_SESSION_TOKEN at spawn)",
            )
        import hashlib

        token_hash = hashlib.sha256(session_token.encode()).hexdigest()
        hash_row = conn.execute(
            "SELECT session_token_hash, state FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if hash_row is None:
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message=f"attempt {attempt_id} not found for token check",
            )
        stored_hash, attempt_state = hash_row[0], hash_row[1]
        if attempt_state in ("succeeded", "failed", "cancelled", "stale"):
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message=f"attempt {attempt_id} is terminal; session token revoked",
            )
        if stored_hash is None or token_hash != stored_hash:
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message="session_token mismatch; "
                "use the LHGP_SESSION_TOKEN environment variable from spawn",
            )

    contract_state: ContractState | None = None
    raw_state = params.get("contract_state")
    if raw_state is not None:
        try:
            contract_state = ContractState(str(raw_state))
        except ValueError:
            raise RpcError(
                code=ErrorCode.VALIDATION_FAILED,
                message=f"unknown contract_state: {raw_state}",
            ) from None
        # 完成态是验收结论，只能由 verifier 裁决路径写入（tick
        # _judge_verifier_outcomes）；执行者自报 complete = 伪造工作量
        # （安全审查 RPC-C3），明确拒接而非交给状态机碰运气。
        attempt_for_role = get_attempt(conn, attempt_id)
        writer_role = attempt_for_role.role if attempt_for_role is not None else "executor"
        if (
            contract_state in (ContractState.COMPLETE, ContractState.SATISFIED)
            and writer_role != "verifier"
        ):
            raise RpcError(
                code=ErrorCode.STATE_FORBIDDEN,
                message=(
                    "attempt/write-back cannot set a contract to a completion state; "
                    "report attempt_state and let the verifier decide acceptance"
                ),
            )

    events: list[EventInput] = []
    actual_model = str(params.get("model_id", "")).strip()
    # 消耗台账（§11.3）：执行者自报 token/成本，形状 fail-closed 校验后
    # 随写回落库。自报是下界——压缩/缓存刷新可能让真实消耗更高。
    raw_usage = params.get("usage")
    usage: dict[str, Any] | None = None
    if raw_usage is not None:
        from lhgp.persistence.usage import UsageInvalidError, normalize_usage

        try:
            usage = normalize_usage(raw_usage)
        except UsageInvalidError as exc:
            raise RpcError(code=ErrorCode.VALIDATION_FAILED, message=str(exc)) from None
    note = params.get("progress_note")
    if note is not None and str(note).strip():
        events.append(
            EventInput(
                event_type=EventType.CONTEXT_SCRATCH_UPDATED,
                payload={"attempt_id": attempt_id, "note": str(note)},
                attempt_id=attempt_id,
            )
        )
    raw_attempt_state = params.get("attempt_state")
    if raw_attempt_state is not None:
        state_text = str(raw_attempt_state)
        attempt = get_attempt(conn, attempt_id)
        attempt_role = attempt.role if attempt is not None else "executor"
        contract = get_contract(conn, contract_id)
        if attempt is not None and attempt.executor_id and contract is not None:
            binding = binding_for_executor(contract.draft.authority, attempt.executor_id)
            if binding is not None and not actual_model and "*" not in binding.models:
                raise RpcError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="model_id is required for an explicitly model-bound attempt",
                )
            if (
                binding is not None
                and actual_model
                and not models_allow(contract.draft.authority, binding=binding, model=actual_model)
            ):
                raise RpcError(
                    code=ErrorCode.CAPABILITY_MISSING,
                    message=f"model_id is not allowed by contract authority: {actual_model}",
                )
        evidence = params.get("evidence")
        if attempt_role == "verifier" and state_text in {"succeeded", "failed"}:
            if not isinstance(evidence, list) or not evidence:
                raise RpcError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="verifier terminal write-back requires a non-empty evidence list",
                )
            invalid = [
                item
                for item in evidence
                if not isinstance(item, dict)
                or not str(item.get("check_id", "")).strip()
                or item.get("outcome") not in {"pass", "fail", "undetermined"}
                or not str(item.get("source", "")).strip()
            ]
            if invalid:
                raise RpcError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message="each verifier evidence item requires check_id, outcome and source",
                )
        match state_text:
            case "succeeded":
                events.append(
                    EventInput(
                        event_type=EventType.ATTEMPT_SUCCEEDED,
                        payload={
                            "reported_by": "model",
                            "role": attempt_role,
                            "evidence": evidence,
                            **({"usage": usage} if usage else {}),
                            # 8th/9th-round: the runtime, not the model, writes
                            # the content identity of what was checked, and it
                            # writes the identity of the revision THIS attempt
                            # was admitted at -- so neither a later acceptance
                            # edit nor a forged fingerprint can retroactively
                            # bless this evidence.
                            **(
                                attempt_evidence_binding(
                                    conn, contract_id, attempt.contract_revision
                                )
                                if attempt_role == "verifier" and attempt is not None
                                else {}
                            ),
                            **({"model_id": actual_model} if actual_model else {}),
                        },
                        attempt_id=attempt_id,
                    )
                )
            case "failed":
                events.append(
                    EventInput(
                        event_type=EventType.ATTEMPT_FAILED,
                        payload={
                            "reported_by": "model",
                            "role": attempt_role,
                            "reason": str(note or ""),
                            "evidence": evidence,
                            **({"usage": usage} if usage else {}),
                            **({"model_id": actual_model} if actual_model else {}),
                        },
                        attempt_id=attempt_id,
                    )
                )
            case _:
                raise RpcError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message=f"attempt_state must be succeeded|failed, got: {raw_attempt_state}",
                )

    try:
        result = write_back(
            conn,
            contract_id=contract_id,
            attempt_id=attempt_id,
            write_generation=write_generation,
            now=now,
            contract_state=contract_state,
            request_id=envelope.request_id or None,
            actor="model",
            events=events,
            model_id=actual_model or None,
            usage=usage,
        )
    except LeaseFencedError as exc:
        raise RpcError(code=ErrorCode.LEASE_FENCED, message=str(exc)) from exc

    return {
        "ok": True,
        "result": {
            "contract_id": result.contract_id,
            "attempt_id": attempt_id,
            "lease_generation": result.lease_generation,
            "event_ids": list(result.event_ids),
            "revision": result.revision,
        },
    }


__all__ = [
    "handle_attempt_status",
    "handle_attempt_write_back",
    "handle_control_interrupt",
    "handle_lease_renew",
]
