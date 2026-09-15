"""完成事件行的凭据加固（SPEC §12.1、§11.3）。

事件行是**自报**的完成声明：任何打印出该行的输出都会被采信，包括 harness
把文档示例回显出来。spawn 注入的 per-attempt session_token 已在子进程环境里，
因此鼓励 harness 在事件行里带上它——带且匹配 → completion_attested=True；
不带 → False（仍按既有语义采信，存量 harness 不破）；带了但不匹配 →
拒绝该行（防「别的 attempt 的回显」被当成完成声明）。
"""

from __future__ import annotations

from typing import Any

from longtask.adapters.subprocess_adapter import _scan_finished

TOKEN = "tok-abc-123"  # noqa: S105 - test fixture
OTHER_TOKEN = "tok-other-456"  # noqa: S105 - test fixture


class _Fake:
    def __init__(self, expected: str | None) -> None:
        self.finished_event: dict[str, Any] | None = None
        self.completion_attested: bool | None = None
        self._expected_session_token = expected

    def _note_finished(self, event: dict[str, Any]) -> None:
        self.finished_event = event


def _line(**extra: object) -> bytes:
    import json

    payload = {"event": "attempt/finished", "outcome": "succeeded", "returncode": 0}
    payload.update(extra)
    return json.dumps(payload, separators=(",", ":")).encode()


class TestAttestation:
    def test_bare_event_is_accepted_and_unattested(self) -> None:
        """不带凭据：采信（向后兼容），但审计上标为未凭据。"""
        m = _Fake(TOKEN)
        assert _scan_finished(_line(), m) is True
        assert m.completion_attested is False
        assert m.finished_event is not None

    def test_matching_token_is_accepted_and_attested(self) -> None:
        m = _Fake(TOKEN)
        assert _scan_finished(_line(session_token=TOKEN), m) is True
        assert m.completion_attested is True

    def test_mismatched_token_is_refused(self) -> None:
        """错 token 视同未自报：拒绝该行，绝不采信。"""
        m = _Fake(TOKEN)
        assert _scan_finished(_line(session_token=OTHER_TOKEN), m) is False
        assert m.finished_event is None
        assert m.completion_attested is None

    def test_token_when_none_injected_is_refused(self) -> None:
        """本 attempt 未注入凭据却带着 token：拒绝（来源不明）。"""
        m = _Fake(None)
        assert _scan_finished(_line(session_token=TOKEN), m) is False

    def test_non_string_token_is_refused(self) -> None:
        m = _Fake(TOKEN)
        assert _scan_finished(_line(session_token=123), m) is False

    def test_whitespace_variant_still_matches(self) -> None:
        """容错空白与凭据校验并存：Python 默认分隔符形式也认。"""
        import json

        m = _Fake(TOKEN)
        line = json.dumps(
            {"event": "attempt/finished", "outcome": "succeeded", "session_token": TOKEN}
        ).encode()
        assert _scan_finished(line, m) is True
        assert m.completion_attested is True

    def test_other_events_and_non_json_are_still_refused(self) -> None:
        m = _Fake(TOKEN)
        assert _scan_finished(b'{"event":"attempt/started"}', m) is False
        assert _scan_finished(b'"event": "attempt/finished" not json', m) is False
