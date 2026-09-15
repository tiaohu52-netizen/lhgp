"""protocol/* handler：版本协商与事件流读取。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lhgp import PROTOCOL_VERSION, __version__
from lhgp.persistence.events import EventType
from lhgp.persistence.store import StoreError, append_event, get_contract, get_events
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.methods import Method

if TYPE_CHECKING:
    from lhgp.rpc.server import RequestEnvelope


def handle_protocol_hello(envelope: RequestEnvelope, **kwargs: Any) -> dict[str, Any]:
    """服务端问候与版本协商。"""
    return {
        "ok": True,
        "result": {
            "protocol_version": PROTOCOL_VERSION,
            "server_version": __version__,
            "methods": [m.value for m in Method],
        },
    }


def handle_protocol_events(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """按 cursor 分页读取事件流。"""
    params = envelope.params
    contract_id: str | None = params.get("contract_id")
    cursor = _coerce_int(params.get("cursor", params.get("after_event_id")), "cursor")
    limit = _coerce_int(params.get("limit", 50), "limit")
    if limit is None or limit <= 0:
        raise RpcError(code=ErrorCode.VALIDATION_FAILED, message="limit must be positive")
    try:
        events = get_events(conn, contract_id=contract_id, after_event_id=cursor, limit=limit)
    except StoreError as exc:
        raise RpcError(code=ErrorCode.INTERNAL, message=str(exc)) from exc
    events_payload = [
        {
            "event_id": e.event_id,
            "contract_id": e.contract_id,
            "attempt_id": e.attempt_id,
            "lease_generation": e.lease_generation,
            "event_type": e.event_type,
            "payload_json": e.payload_json,
            "payload": json.loads(e.payload_json),
            "request_id": e.request_id,
            "created_at": e.created_at.isoformat(),
            "actor": e.actor,
            "schema_version": e.schema_version,
        }
        for e in events
    ]
    next_cursor = events[-1].event_id if events else cursor
    return {
        "ok": True,
        "result": {
            "events": events_payload,
            "next_cursor": next_cursor,
            "has_more": len(events) == limit,
        },
    }


def handle_daemon_wake(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """接受本机计划任务唤醒信号并落审计事件。

    该方法只记录 ``wakeup/rtc-fired``，不读取合同内容、不做仲裁；主 daemon
    在下一轮 tick 才推进合同。任务不存在时返回明确错误，避免把错误的计划
    任务伪装成成功唤醒。
    """
    from lhgp.rpc.handlers._common import resolve_actor

    actor = resolve_actor(envelope, envelope.params)
    if actor != "daemon":
        raise RpcError(
            code=ErrorCode.AUTH_FAILED,
            message="daemon/wake requires a daemon client",
        )
    raw_task_id = envelope.params.get("task_id")
    task_id = str(raw_task_id).strip() if raw_task_id is not None else ""
    prefix = "longtask-wakeup-"
    if not task_id.startswith(prefix) or len(task_id) == len(prefix):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message="task_id must use the longtask-wakeup-<contract_id> format",
        )
    contract_id = task_id[len(prefix) :]
    contract = get_contract(conn, contract_id)
    if contract is None:
        raise RpcError(
            code=ErrorCode.UNKNOWN_CONTRACT,
            message=f"unknown contract: {contract_id}",
        )
    append_event(
        conn,
        contract_id=contract_id,
        goal_id=contract.goal_id,
        event_type=EventType.WAKEUP_RTC_FIRED,
        payload={"task_id": task_id},
        now=now,
        actor=actor,
        request_id=envelope.request_id or None,
        contract_revision=contract.revision,
        role="daemon",
    )
    return {
        "ok": True,
        "result": {
            "task_id": task_id,
            "contract_id": contract_id,
            "queued_for_daemon": True,
        },
    }


def _coerce_int(raw: Any, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (bool, float)):
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"{name} must be an integer",
        )
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RpcError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"{name} must be an integer: {exc}",
        ) from exc


__all__ = ["handle_daemon_wake", "handle_protocol_events", "handle_protocol_hello"]
