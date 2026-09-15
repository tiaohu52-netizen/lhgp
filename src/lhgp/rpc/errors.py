"""Canonical structured RPC errors and retry policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNKNOWN_CONTRACT = "UNKNOWN_CONTRACT"
    UNKNOWN_ATTEMPT = "UNKNOWN_ATTEMPT"
    UNKNOWN_EXECUTOR = "UNKNOWN_EXECUTOR"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    LEASE_FENCED = "LEASE_FENCED"
    LEASE_HELD = "LEASE_HELD"
    PARTITION_CONFLICT = "PARTITION_CONFLICT"
    CONSTRAINT_UNTRANSLATABLE = "CONSTRAINT_UNTRANSLATABLE"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    CONTEXT_CAPACITY_REFUSED = "CONTEXT_CAPACITY_REFUSED"
    CONTEXT_STALE = "CONTEXT_STALE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    STATE_FORBIDDEN = "STATE_FORBIDDEN"
    STORE_TAMPERED = "STORE_TAMPERED"
    IDEMPOTENCY_REPLAY_MISMATCH = "IDEMPOTENCY_REPLAY_MISMATCH"
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"


RETRYABLE: dict[ErrorCode, bool] = {
    ErrorCode.VALIDATION_FAILED: False,
    ErrorCode.UNKNOWN_CONTRACT: False,
    ErrorCode.UNKNOWN_ATTEMPT: False,
    ErrorCode.UNKNOWN_EXECUTOR: False,
    ErrorCode.REVISION_CONFLICT: True,
    ErrorCode.LEASE_FENCED: False,
    ErrorCode.LEASE_HELD: True,
    ErrorCode.PARTITION_CONFLICT: False,
    ErrorCode.CONSTRAINT_UNTRANSLATABLE: False,
    ErrorCode.CAPABILITY_MISSING: False,
    ErrorCode.CONTEXT_CAPACITY_REFUSED: False,
    ErrorCode.CONTEXT_STALE: True,
    ErrorCode.BUDGET_EXHAUSTED: False,
    ErrorCode.STATE_FORBIDDEN: False,
    ErrorCode.STORE_TAMPERED: False,
    ErrorCode.IDEMPOTENCY_REPLAY_MISMATCH: False,
    ErrorCode.AUTH_FAILED: False,
    ErrorCode.AUTH_REQUIRED: False,
    ErrorCode.RATE_LIMITED: True,
    ErrorCode.INTERNAL: True,
}


@dataclass(slots=True)
class RpcError(Exception):
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # ``Exception.__init__`` writes ``self.args = (message,)``,
        # which fails on a ``slots=True`` dataclass (no
        # ``__dict__`` for the args attribute).  Setting
        # ``args`` through ``object.__setattr__`` keeps the
        # rest of the field layout intact while still giving
        # the exception a usable ``.args`` for traceback
        # assembly.  Without this, raising an RpcError
        # through a ``with`` block on Python 3.13 triggers
        # an extra ``super(Exception, self).__init__()`` chain
        # in traceback assembly that fails with
        # ``obj is not an instance or subtype of type``.
        object.__setattr__(self, "args", (self.message,))

    @property
    def retryable(self) -> bool:
        return RETRYABLE[self.code]

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            },
        }

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> RpcError:
        error = payload.get("error", {})
        raw_code = str(error.get("code", ""))
        try:
            code = ErrorCode(raw_code)
        except ValueError:
            code = ErrorCode.INTERNAL
        return RpcError(
            code=code,
            message=str(error.get("message", raw_code or "unknown error")),
            details=dict(error.get("details", {})),
        )


__all__ = ["RETRYABLE", "ErrorCode", "RpcError"]
