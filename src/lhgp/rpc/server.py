"""Canonical JSON-RPC envelope parsing and handler routing."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lhgp import PROTOCOL_VERSION
from lhgp.persistence.errors import (
    IllegalStateTransitionError,
    StoreError,
    StoreTamperedError,
)
from lhgp.persistence.events_query import append_event
from lhgp.persistence.paths import default_data_root
from lhgp.persistence.store import StoreConfig, connect, ensure_schema
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.handlers import HANDLERS
from lhgp.rpc.methods import Method
from longtask.rpc.dispatch import RETRY_EVENT_TYPE, with_transient_retry

if TYPE_CHECKING:
    from lhgp.adapters.registry import ExecutorRegistry

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    method: Method
    request_id: str
    client_id: str
    protocol_version: int
    params: dict[str, Any] = field(default_factory=dict)


def parse_envelope(raw: dict[str, Any]) -> RequestEnvelope:
    try:
        method = Method(str(raw["method"]))
        raw_request_id = raw["request_id"]
        raw_client_id = raw["client_id"]
        if not isinstance(raw_request_id, str) or not isinstance(raw_client_id, str):
            raise TypeError("request_id and client_id must be strings")
        request_id = raw_request_id
        client_id = raw_client_id
        raw_protocol_version = raw["protocol_version"]
        if isinstance(raw_protocol_version, bool):
            raise ValueError("protocol_version must be an integer")
        protocol_version = int(raw_protocol_version)
    except (KeyError, TypeError, ValueError) as exc:
        raise RpcError(ErrorCode.VALIDATION_FAILED, f"malformed request envelope: {exc}") from exc
    if protocol_version != PROTOCOL_VERSION:
        raise RpcError(
            ErrorCode.VALIDATION_FAILED,
            f"protocol_version {protocol_version} unsupported by this daemon "
            f"(speaks {PROTOCOL_VERSION})",
        )
    if not request_id or not client_id:
        raise RpcError(ErrorCode.VALIDATION_FAILED, "request_id and client_id must be non-empty")
    raw_params = raw.get("params", {})
    if not isinstance(raw_params, dict):
        raise RpcError(ErrorCode.VALIDATION_FAILED, "params must be an object")
    return RequestEnvelope(method, request_id, client_id, protocol_version, dict(raw_params))


def route(
    envelope: RequestEnvelope,
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    registry: ExecutorRegistry | None = None,
) -> dict[str, Any]:
    handler = HANDLERS.get(envelope.method)
    if handler is None:
        raise RpcError(
            ErrorCode.STATE_FORBIDDEN,
            f"method '{envelope.method.value}' not implemented",
            {"request_id": envelope.request_id},
        )
    current_time = now or datetime.now(UTC)

    # RETRY_ATTEMPTED audit emission. The closure captures the dispatch's
    # conn/now so each retry is logged against the right session. When
    # the caller didn't supply a conn (the default-conn path below
    # creates one inside the function), we can't reach it from here, so
    # the event is silently skipped in that rare path.
    def _emit_retry(attempt: int, exc: BaseException, delay: float) -> None:
        if conn is None:
            return
        try:
            append_event(
                conn,
                contract_id=None,
                event_type=RETRY_EVENT_TYPE,
                now=current_time,
                payload={
                    "attempt": attempt,
                    "delay_seconds": round(delay, 3),
                    "error_type": type(exc).__name__,
                    "method": envelope.method.value,
                },
                request_id=envelope.request_id,
                actor="daemon",
            )
        except Exception as audit_exc:  # never let audit failure break the retry
            _logger.debug("retry audit emission failed: %s", audit_exc)

    @with_transient_retry(on_retry=_emit_retry)
    def _dispatch() -> dict[str, Any]:
        if conn is not None:
            return handler(envelope, conn=conn, now=current_time, registry=registry)
        data_dir = default_data_root()
        data_dir.mkdir(parents=True, exist_ok=True)
        default_conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            ensure_schema(default_conn)
            return handler(envelope, conn=default_conn, now=current_time, registry=registry)
        finally:
            default_conn.close()

    try:
        return _dispatch()
    except StoreTamperedError as exc:
        # 存储被外部改动 / 行损坏（审计 B5）。此前读路径抛的是 ``json.JSONDecodeError``
        # 或 ``KeyError`` 这类**不属于 StoreError 层级**的裸异常，会直接穿过 RPC 边界
        # 冒到传输层；而映射是靠每个 handler 手写的（本包内就有三种写法），漏写即
        # 漏报——实测 ``contract/get``、``goal/prepare``、``goal/admission-check``
        # 三处就是漏的，其中 contract/get 正是模型读取合同的主要入口。
        #
        # 把映射收到边界：所有方法（含以后新增的）按构造即被覆盖，且
        # ``ErrorCode.STORE_TAMPERED`` 早就带着 ``RETRYABLE=False`` 等着被用。
        # handler 内部更具体的映射仍先命中，行为不变。
        raise RpcError(
            ErrorCode.STORE_TAMPERED,
            str(exc),
            {"request_id": envelope.request_id},
        ) from exc
    except IllegalStateTransitionError as exc:
        # 状态机兜底（审计 B1）。handler 的守卫（approve/pause/resume/cancel/
        # arbitrate）先命中，给出更精确的提示；走到这里说明有代码路径绕过了
        # handler——把合同推进了非法状态。语义上这是「该状态下不允许」，不是
        # 内部错误，因此映射为 STATE_FORBIDDEN（RETRYABLE=False）。
        raise RpcError(
            ErrorCode.STATE_FORBIDDEN,
            str(exc),
            {"request_id": envelope.request_id},
        ) from exc
    except StoreError as exc:
        raise RpcError(
            ErrorCode.INTERNAL,
            str(exc),
            {"request_id": envelope.request_id},
        ) from exc


__all__ = ["RequestEnvelope", "parse_envelope", "route"]
