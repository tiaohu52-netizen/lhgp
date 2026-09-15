"""P3 外部运行句柄（SPEC §11.3）单元测试。

关注 parse_legacy_session_ref：老 attempt 写库的 session_ref 仍是
subprocess:<attempt_id>:<pid> 字符串；P3 reconcile 需要把它解成
ExternalRunHandle 走四分支判定。

词汇对齐说明：recovery_strategy 取规范原文的
`reattach | poll | nonrecoverable`。早期草稿用过的 `orphan_grace` /
`fence_respawn` 不是策略而是「宽限后的处置动作」，由 reconciler 据策略
与宽限期推导，不写进句柄。
"""

from __future__ import annotations

import pytest

from longtask.adapters.handles import (
    RECOVERY_NONRECOVERABLE,
    RECOVERY_POLL,
    RECOVERY_STRATEGIES,
    ExternalRunHandle,
    parse_legacy_session_ref,
)

pytestmark = pytest.mark.unit


class TestParseLegacySessionRef:
    def test_subprocess_format_unpacks_pid_as_external_run_id(self) -> None:
        h = parse_legacy_session_ref("subprocess:att-20260901-001:12345")
        assert h.external_run_id == "12345"
        assert h.session_locator == "att-20260901-001"
        # pid 只作提示：能观察就观察（poll），观察不到就是状态未知
        assert h.recovery_strategy == RECOVERY_POLL
        assert h.capability_snapshot == {"transport": "subprocess"}
        assert h.process_identity == {"pid": "12345"}

    def test_unknown_format_defaults_to_nonrecoverable(self) -> None:
        h = parse_legacy_session_ref("mcp-xyz")
        assert h.recovery_strategy == RECOVERY_NONRECOVERABLE
        assert h.session_locator == "mcp-xyz"

    def test_fake_format_is_nonrecoverable(self) -> None:
        """纯内存适配器：进程一退就再也联系不上，不许声称可恢复。"""
        h = parse_legacy_session_ref("fake:att-1")
        assert h.recovery_strategy == RECOVERY_NONRECOVERABLE
        assert h.capability_snapshot == {"transport": "fake"}

    def test_to_dict_round_trip(self) -> None:
        h = ExternalRunHandle(
            external_run_id="abc",
            session_locator="sess-1",
            recovery_strategy="reattach",
            capability_snapshot={"x": 1},
            process_identity={"pid": 42},
        )
        d = h.to_dict()
        h2 = ExternalRunHandle.from_dict(d)
        assert h2 == h

    def test_from_dict_missing_keys_use_defaults(self) -> None:
        h = ExternalRunHandle.from_dict({})
        assert h.external_run_id == ""
        assert h.session_locator == ""
        assert h.recovery_strategy == RECOVERY_NONRECOVERABLE
        assert h.capability_snapshot == {}
        assert h.process_identity == {}

    def test_strategy_vocabulary_matches_spec(self) -> None:
        """策略词汇就是规范 §11.3 的三个值，不多不少。"""
        assert RECOVERY_STRATEGIES == ("reattach", "poll", "nonrecoverable")

    def test_is_recoverable_distinguishes_poll_from_nonrecoverable(self) -> None:
        assert ExternalRunHandle("r", "s", RECOVERY_POLL).is_recoverable() is True
        assert ExternalRunHandle("r", "s", "reattach").is_recoverable() is True
        assert ExternalRunHandle("r", "s", RECOVERY_NONRECOVERABLE).is_recoverable() is False
