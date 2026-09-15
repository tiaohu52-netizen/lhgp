"""DESIGN §14：外部输出的结构性清洗（``lhgp.untrusted.sanitize``）。"""

from __future__ import annotations

from lhgp.acceptance.verdict import parse_verdict_block
from lhgp.untrusted.sanitize import has_terminal_controls, sanitize_terminal_text


def test_sanitize_strips_csi_color_sequences() -> None:
    assert sanitize_terminal_text("\x1b[31mred\x1b[0m plain") == "red plain"


def test_sanitize_strips_osc_title_terminated_by_bel() -> None:
    assert sanitize_terminal_text("\x1b]0;window title\x07payload") == "payload"


def test_sanitize_strips_osc_terminated_by_string_terminator() -> None:
    assert sanitize_terminal_text("\x1b]0;t\x1b\\payload") == "payload"


def test_sanitize_strips_dcs_string_sequence() -> None:
    assert sanitize_terminal_text("\x1bP1;2;3qpayload\x1b\\after") == "after"


def test_sanitize_keeps_text_structure_controls() -> None:
    assert sanitize_terminal_text("a\tb\nc\rd") == "a\tb\nc\rd"


def test_sanitize_drops_bidi_override_that_can_spoof_display_order() -> None:
    assert sanitize_terminal_text("\u202eevil\u202c tail") == "evil tail"


def test_sanitize_keeps_zero_width_joiners_used_for_text_shaping() -> None:
    assert sanitize_terminal_text("a\u200cb\U0001f469\u200d\U0001f4bb") == (
        "a\u200cb\U0001f469\u200d\U0001f4bb"
    )


def test_sanitize_drops_c1_control_characters() -> None:
    assert sanitize_terminal_text("a\x85b\x9bc") == "abc"


def test_sanitize_drops_del_character() -> None:
    assert sanitize_terminal_text("a\x7fb") == "ab"


def test_sanitize_drops_truncated_escape_sequence_at_end_of_output() -> None:
    assert sanitize_terminal_text("abc\x1b[") == "abc"


def test_sanitize_is_idempotent() -> None:
    dirty = "\x1b[1m**\u202edone\u202c**\x1b[0m\n"
    once = sanitize_terminal_text(dirty)
    assert sanitize_terminal_text(once) == once


def test_sanitize_empty_input_returns_empty() -> None:
    assert sanitize_terminal_text("") == ""


def test_sanitize_recovers_verdict_fence_that_ansi_wrapped() -> None:
    """着色序列插进围栏 token 时，清洗后判定块必须重新可解析。

    这是本模块的实战动机：verifier 的 harness 给输出上色，原本有效的
    证据会退化成「无证据」（``parse_verdict_block`` 返回 None）。
    """
    colored = (
        "prose\n"
        "```\x1b[32mlhgp-verdict\x1b[0m\n"
        '{"verdict": "succeeded", "checks": [{"check_id": "c1", "outcome": "pass"}]}\n'
        "```\n"
    )
    assert parse_verdict_block(colored) is None
    parsed = parse_verdict_block(sanitize_terminal_text(colored))
    assert parsed is not None
    assert parsed.verdict == "succeeded"


def test_has_terminal_controls_agrees_with_sanitize() -> None:
    assert has_terminal_controls("\x1b[31mred\x1b[0m") is True
    assert has_terminal_controls("plain text\n") is False
