"""P5 acceptance checks 单元测试。"""

from __future__ import annotations

import pytest

from lhgp.acceptance.evaluator import _command_timeout_seconds
from longtask.acceptance.checks import (
    CheckKind,
    CheckSpec,
    RepairBrief,
)
from longtask.acceptance.evaluator import evaluate_check
from longtask.contracts.acceptance import Acceptance

pytestmark = pytest.mark.unit


class TestCheckKind:
    def test_seven_kinds(self) -> None:
        # CheckKind has grown since the original 7; current count is 15.
        # We pin the count here so a future accidental addition is visible.
        assert len(list(CheckKind)) == 15

    def test_values_lower_kebab(self) -> None:
        for k in CheckKind:
            assert k.value == k.value.lower()
            assert " " not in k.value


class TestCheckSpec:
    def test_to_dict_mandatory_default_true(self) -> None:
        s = CheckSpec(kind=CheckKind.FILE_EXISTS, target="result.txt")
        d = s.to_dict()
        assert d["kind"] == "file-exists"
        assert d["target"] == "result.txt"
        assert d["mandatory"] is True
        assert d["args"] == {}

    def test_to_dict_optional(self) -> None:
        s = CheckSpec(
            kind=CheckKind.COMMAND_EXIT_ZERO,
            target="pytest",
            args={"args": ["-q"]},
            mandatory=False,
        )
        d = s.to_dict()
        assert d["mandatory"] is False
        assert d["args"] == {"args": ["-q"]}

    def test_from_dict_round_trips_typed_check(self) -> None:
        original = CheckSpec(kind=CheckKind.FILE_EXISTS, target="dist/app.js")
        assert CheckSpec.from_dict(original.to_dict()) == original

    def test_from_dict_rejects_non_boolean_mandatory(self) -> None:
        with pytest.raises(TypeError, match="mandatory must be a boolean"):
            CheckSpec.from_dict(
                {
                    "kind": "file-exists",
                    "target": "dist/app.js",
                    "mandatory": "false",
                }
            )

    @pytest.mark.parametrize("target", [True, "", "   ", None])
    def test_from_dict_rejects_invalid_target(self, target: object) -> None:
        with pytest.raises(TypeError, match="target must be a non-empty string"):
            CheckSpec.from_dict({"kind": "file-exists", "target": target})

    def test_from_dict_rejects_non_object_args(self) -> None:
        with pytest.raises(TypeError, match="args must be an object"):
            CheckSpec.from_dict({"kind": "file-exists", "target": "result.txt", "args": []})

    def test_acceptance_validates_typed_check(self) -> None:
        acceptance = Acceptance(
            standard="all artifacts",
            checks=(CheckSpec(kind=CheckKind.FILE_EXISTS, target="result.txt"),),
        )
        assert acceptance.validate() == []


class TestRepairBrief:
    def test_default_empty(self) -> None:
        b = RepairBrief()
        d = b.to_dict()
        assert d == {
            "failed_checks": [],
            "context_pointer": "",
            "retry_strategy": "respawn",
            "notes": [],
        }

    def test_with_payload(self) -> None:
        b = RepairBrief(
            failed_checks=("file-exists:result.txt",),
            context_pointer=".workbuddy/handover/lt-x.md",
            retry_strategy="swap_executor",
            notes=("尝试过 exec-a, exec-b 均失败",),
        )
        d = b.to_dict()
        assert d["failed_checks"] == ["file-exists:result.txt"]
        assert d["retry_strategy"] == "swap_executor"
        assert d["notes"] == ["尝试过 exec-a, exec-b 均失败"]


class TestEvaluateCheck:
    def test_file_exists_and_hash_are_deterministic(self, tmp_path) -> None:
        target = tmp_path / "result.txt"
        target.write_text("hello", encoding="utf-8")
        exists = evaluate_check(
            CheckSpec(kind=CheckKind.FILE_EXISTS, target="result.txt"), workspace_root=tmp_path
        )
        hashed = evaluate_check(
            CheckSpec(
                kind=CheckKind.FILE_CONTENT_MATCHES,
                target="result.txt",
                args={"contains": "ell"},
            ),
            workspace_root=tmp_path,
        )
        assert exists.outcome == "pass"
        assert hashed.outcome == "pass"

    def test_path_escape_fails_closed(self, tmp_path) -> None:
        result = evaluate_check(
            CheckSpec(kind=CheckKind.FILE_EXISTS, target="../outside"), workspace_root=tmp_path
        )
        assert result.outcome == "fail"

    def test_command_uses_structured_argv(self, tmp_path) -> None:
        result = evaluate_check(
            CheckSpec(
                kind=CheckKind.COMMAND_EXIT_ZERO,
                target="python",
                args={"argv": ["-c", "raise SystemExit(0)"]},
            ),
            workspace_root=tmp_path,
        )
        assert result.outcome == "pass"

    def test_command_missing_in_daemon_path_is_undetermined_with_guidance(self, tmp_path) -> None:
        """SPEC §12.1 环境契约：守护进程 PATH 找不到解释器 → undetermined
        （不是 fail）且 details 给出可行动指引——「跑不了」与「跑挂了」
        是不同的事实（dogfood v5 发现 5）。"""
        result = evaluate_check(
            CheckSpec(
                kind=CheckKind.COMMAND_EXIT_ZERO,
                target="no-such-interpreter-xyz",
            ),
            workspace_root=tmp_path,
        )
        assert result.outcome == "undetermined"
        assert "command not found in daemon PATH" in result.details
        assert "absolute interpreter path" in result.details
        assert "verifier verdict block" in result.details

    def test_command_target_resolves_relative_to_workspace_root(self, tmp_path) -> None:
        """SPEC §12.1 target 约定：相对 workspace_root 解析，不带重复
        workspace 前缀（dogfood v5 发现 6 的协议侧事实）。"""
        (tmp_path / "artifact.py").write_text("x = 1\n", encoding="utf-8")
        direct = evaluate_check(
            CheckSpec(kind=CheckKind.FILE_EXISTS, target="artifact.py"),
            workspace_root=tmp_path,
        )
        assert direct.outcome == "pass"
        # 双层前缀（把 workspace 名再写一遍）在 workspace_root 下不存在
        doubled = evaluate_check(
            CheckSpec(kind=CheckKind.FILE_EXISTS, target="ws/artifact.py"),
            workspace_root=tmp_path,
        )
        assert doubled.outcome == "fail"
        assert "artifact does not exist" in doubled.details

    def test_command_timeout_is_bounded_and_reported_as_undetermined(self, tmp_path) -> None:
        """慢验收命令不能阻塞 daemon，也不能伪造 pass/fail。"""
        result = evaluate_check(
            CheckSpec(
                kind=CheckKind.COMMAND_EXIT_ZERO,
                target="python",
                args={
                    "argv": ["-c", "import time; time.sleep(0.2)"],
                    "timeout_seconds": 0.1,
                },
            ),
            workspace_root=tmp_path,
        )
        assert result.outcome == "undetermined"
        assert "timed out" in result.details.lower()

    @pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
    def test_command_timeout_rejects_non_finite_or_boolean_values(self, value: object) -> None:
        spec = CheckSpec(
            kind=CheckKind.COMMAND_EXIT_ZERO, target="python", args={"timeout_seconds": value}
        )
        assert _command_timeout_seconds(spec, None) == 60.0
