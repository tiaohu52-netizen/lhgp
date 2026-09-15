"""SPEC §12.4 验收证据通道：verdict 判定块解析与裁决合成。

dogfood v5 发现 4 的修复测试：一次性 CLI verifier 无法调
attempt/write-back RPC，其结论只出现在 stdout——约定 lhgp-verdict
判定块进裁决，协议 undetermined 由模型显式结论填补，确定性结果
优先不被模型覆盖。
"""

from __future__ import annotations

from lhgp.acceptance.verdict import (
    merge_evidence,
    parse_verdict_block,
)

GOOD_BLOCK = """核对完成，两条检查都真实跑过。

```lhgp-verdict
{"verdict": "succeeded", "checks": [
  {"check_id": "file-exists:charfreq.py", "outcome": "pass", "source": "ws/charfreq.py"},
  {"check_id": "command-exit-zero:python test_charfreq.py", "outcome": "pass",
   "source": "activated venv and ran tests", "details": "All tests passed"}
]}
```
"""


class TestParseVerdictBlock:
    def test_parses_valid_block(self) -> None:
        v = parse_verdict_block(GOOD_BLOCK)
        assert v is not None
        assert v.verdict == "succeeded"
        assert v.outcome_for("file-exists:charfreq.py") == "pass"

    def test_missing_block_returns_none(self) -> None:
        assert parse_verdict_block("no verdict here, just prose") is None

    def test_empty_stdout_returns_none(self) -> None:
        assert parse_verdict_block("") is None

    def test_invalid_json_returns_none(self) -> None:
        assert parse_verdict_block("```lhgp-verdict\n{not json}\n```") is None

    def test_invalid_verdict_value_returns_none(self) -> None:
        assert (
            parse_verdict_block(
                '```lhgp-verdict\n{"verdict": "maybe", "checks": ['
                '{"check_id": "a", "outcome": "pass", "source": "s"}]}\n```'
            )
            is None
        )

    def test_invalid_outcome_entry_skipped(self) -> None:
        v = parse_verdict_block(
            '```lhgp-verdict\n{"verdict": "failed", "checks": ['
            '{"check_id": "a", "outcome": "probably", "source": "s"},'
            '{"check_id": "b", "outcome": "fail", "source": "s"}]}\n```'
        )
        assert v is not None
        assert v.outcome_for("a") is None
        assert v.outcome_for("b") == "fail"

    def test_empty_checks_returns_none(self) -> None:
        assert (
            parse_verdict_block('```lhgp-verdict\n{"verdict": "succeeded", "checks": []}\n```')
            is None
        )

    def test_last_block_wins_when_multiple(self) -> None:
        text = (
            "```lhgp-verdict\n"
            '{"verdict": "failed", "checks": [{"check_id": "a", "outcome": "fail", "source": "s"}]}'
            "\n```\n"
            "更正：复核后通过。\n"
            "```lhgp-verdict\n"
            '{"verdict": "succeeded", "checks": '
            '[{"check_id": "a", "outcome": "pass", "source": "s"}]}'
            "\n```"
        )
        v = parse_verdict_block(text)
        assert v is not None
        assert v.verdict == "succeeded"


class TestMergeEvidence:
    def test_deterministic_pass_not_overridden_by_model_fail(self) -> None:
        """协议确定性 pass 优先——模型 fail 不得覆盖（防橡皮图章反向）。"""
        protocol = {"check_id": "file-exists:a.py", "outcome": "pass", "source": "a.py"}
        v = parse_verdict_block(
            '```lhgp-verdict\n{"verdict": "failed", "checks": ['
            '{"check_id": "file-exists:a.py", "outcome": "fail", "source": "x"}]}\n```'
        )
        assert v is not None
        merged = merge_evidence(protocol, v)
        assert merged["outcome"] == "pass"
        assert merged["model_outcome"] == "fail"  # 冲突如实记录供审计

    def test_deterministic_fail_not_overridden_by_model_pass(self) -> None:
        """协议确定性 fail 优先——模型 pass 不得覆盖（防橡皮图章）。"""
        protocol = {
            "check_id": "file-exists:missing.py",
            "outcome": "fail",
            "source": "ws/missing.py",
            "details": "artifact does not exist",
        }
        v = parse_verdict_block(
            '```lhgp-verdict\n{"verdict": "succeeded", "checks": ['
            '{"check_id": "file-exists:missing.py", "outcome": "pass", "source": "x"}]}\n```'
        )
        assert v is not None
        merged = merge_evidence(protocol, v)
        assert merged["outcome"] == "fail"
        assert merged["model_outcome"] == "pass"

    def test_model_fills_protocol_undetermined(self) -> None:
        """dogfood v5 场景：裸 PATH 跑不了 python → undetermined → 模型
        实际激活 venv 跑过测试，其 pass 填补裁决。"""
        protocol = {
            "check_id": "command-exit-zero:python test_charfreq.py",
            "outcome": "undetermined",
            "source": "python test_charfreq.py",
            "details": "[WinError 2]",
        }
        v = parse_verdict_block(GOOD_BLOCK)
        assert v is not None
        merged = merge_evidence(protocol, v)
        assert merged["outcome"] == "pass"
        assert "model:" in merged["details"]
        assert merged["model_outcome"] == "pass"

    def test_both_absent_stays_undetermined(self) -> None:
        protocol = {"check_id": "observable:x", "outcome": "undetermined", "source": "x"}
        merged = merge_evidence(protocol, None)
        assert merged["outcome"] == "undetermined"
        assert merged["model_outcome"] == "absent"

    def test_model_absent_keeps_protocol_outcome(self) -> None:
        protocol = {"check_id": "file-exists:a.py", "outcome": "pass", "source": "a.py"}
        merged = merge_evidence(protocol, None)
        assert merged["outcome"] == "pass"

    def test_verdict_for_other_check_does_not_fill(self) -> None:
        """模型对别的 check 的结论不得填补本 check。"""
        protocol = {
            "check_id": "command-exit-zero:pytest",
            "outcome": "undetermined",
            "source": "pytest",
        }
        v = parse_verdict_block(
            '```lhgp-verdict\n{"verdict": "succeeded", "checks": ['
            '{"check_id": "file-exists:a.py", "outcome": "pass", "source": "a.py"}]}\n```'
        )
        assert v is not None
        merged = merge_evidence(protocol, v)
        assert merged["outcome"] == "undetermined"


class TestVerdictFromOutput:
    """带截断事实的判定块解析（SPEC §12.4：截断丢失 ≠ verifier 没写）。"""

    def test_untruncated_absent_block_returns_none(self) -> None:
        from lhgp.acceptance.verdict import verdict_from_output

        assert verdict_from_output("no block here", output_truncated=False) is None

    def test_truncated_absent_block_raises_source_loss(self) -> None:
        """块在末尾、预算保头部：截断后解析不到 = 块被截掉，不是没写。"""
        import pytest

        from lhgp.acceptance.verdict import VerdictSourceLossError, verdict_from_output

        with pytest.raises(VerdictSourceLossError, match="truncated by budget"):
            verdict_from_output("noise without any block", output_truncated=True)

    def test_truncated_but_block_present_returns_verdict(self) -> None:
        """围栏在截断点之前：判定块自足，截断不影响它。"""
        from lhgp.acceptance.verdict import verdict_from_output

        parsed = verdict_from_output(GOOD_BLOCK + "tail lost...", output_truncated=True)
        assert parsed is not None
        assert parsed.verdict == "succeeded"

    def test_same_behavior_as_parse_verdict_block_when_not_truncated(self) -> None:
        from lhgp.acceptance.verdict import parse_verdict_block, verdict_from_output

        assert verdict_from_output(GOOD_BLOCK, output_truncated=False) == parse_verdict_block(
            GOOD_BLOCK
        )
