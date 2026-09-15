"""线协议错误码（DESIGN §11.7）。

对应 claim: error-code-registry-complete（quality/claims.json）。
"""

from __future__ import annotations

import pytest

from longtask.rpc.errors import RETRYABLE, ErrorCode, RpcError

pytestmark = pytest.mark.unit


class TestRegistry:
    def test_every_code_has_retryable_entry(self) -> None:
        missing = [c for c in ErrorCode if c not in RETRYABLE]
        assert missing == [], f"codes missing retryable entry: {missing}"

    def test_no_orphan_retryable_entries(self) -> None:
        orphan = [c for c in RETRYABLE if not isinstance(c, ErrorCode)]
        assert orphan == []

    def test_design_codes_present(self) -> None:
        # DESIGN §11.7 列出的码一个不能少（新增只许追加）
        expected = {
            "VALIDATION_FAILED",
            "UNKNOWN_CONTRACT",
            "UNKNOWN_ATTEMPT",
            "UNKNOWN_EXECUTOR",
            "REVISION_CONFLICT",
            "LEASE_FENCED",
            "LEASE_HELD",
            "PARTITION_CONFLICT",
            "CONSTRAINT_UNTRANSLATABLE",
            "CAPABILITY_MISSING",
            "CONTEXT_CAPACITY_REFUSED",
            "CONTEXT_STALE",
            "BUDGET_EXHAUSTED",
            "STATE_FORBIDDEN",
            "STORE_TAMPERED",
            "IDEMPOTENCY_REPLAY_MISMATCH",
            "AUTH_FAILED",
            "AUTH_REQUIRED",
            "RATE_LIMITED",
            "INTERNAL",
        }
        assert expected <= {c.value for c in ErrorCode}


class TestPayload:
    def test_roundtrip(self) -> None:
        err = RpcError(
            code=ErrorCode.REVISION_CONFLICT,
            message="contract revision is stale",
            details={"expected": 3, "actual": 5},
        )
        payload = err.to_payload()
        assert payload["ok"] is False
        assert payload["error"]["retryable"] is True  # REVISION_CONFLICT 重读后重试
        restored = RpcError.from_payload(payload)
        assert restored.code == ErrorCode.REVISION_CONFLICT
        assert restored.details == {"expected": 3, "actual": 5}

    def test_unknown_code_falls_back_to_internal(self) -> None:
        # 客户端必须把未知错误码按 INTERNAL 处理并展示原文（DESIGN §11.7）
        payload = {
            "ok": False,
            "error": {
                "code": "FUTURE_CODE_FROM_V2",
                "message": "something new",
                "retryable": False,
                "details": {},
            },
        }
        err = RpcError.from_payload(payload)
        assert err.code == ErrorCode.INTERNAL
        assert err.message == "something new"  # 原文保留展示

    def test_lease_fenced_not_retryable(self) -> None:
        # 过期 generation 写回：重试无意义（DESIGN §7 fencing）
        assert RETRYABLE[ErrorCode.LEASE_FENCED] is False
