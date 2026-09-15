"""{task} 占位符（DESIGN §12.1 任务文本位置）测试。

dogfood v4 教训：各 CLI 参数语法不同——cli-bridge 是位置参数（尾元素追加可用），
flag 值型 CLI 是 -p 的值（尾元素被当子命令名报错）。占位符把「prompt 插在哪」
变成注册表配置数据：argv 含一个 {task} → 原位替换；无占位符 → 尾元素
追加（向后兼容）。手写包装器从此不需要。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from longtask.adapters.base import AttemptInput
from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
from longtask.adapters.subprocess_adapter import (
    TASK_PLACEHOLDER,
    LaunchSpec,
    SubprocessAdapter,
)
from longtask.contracts.schema import AttemptRole, Enforcement
from tests.wait_budget import budget

pytestmark = pytest.mark.conformance

NOW_ARGV = ("not-a-real-cli", "{task}")


def make_manifest() -> ExecutorManifest:
    return ExecutorManifest(
        executor_id="flag-cli",
        adapter_version="0.1.0a0",
        transport="subprocess",
        capabilities=Capabilities(
            spawn=True,
            observe=True,
            cancel=True,
            notify=False,
            followup=False,
            steer=False,
            interrupt=True,
            context="optional",
            sandbox=SandboxCapability(
                file_effects="workspace-write",
                network="unsupported",
                process="unsupported",
                enforcement=Enforcement.PARTIAL,
            ),
            acceptance_evidence=True,
        ),
    )


def make_input(workspace: str, *, prompt: str | None = "写收条") -> AttemptInput:
    return AttemptInput(
        attempt_id="att-t1",
        contract_id="lt-t1",
        revision=1,
        lease_generation=1,
        role=AttemptRole.EXECUTOR,
        contract_snapshot={
            "objective": "测试",
            "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
            "context": {},
        },
        handover_path="handover.md",
        workspace_root=workspace,
        budget_remaining={},
        task_prompt=prompt,
    )


class TestPlaceholderPositioning:
    def test_placeholder_replaced_in_place(self, tmp_path: Path) -> None:
        """argv 中的 {task} 原位替换成 prompt——flag 值型 CLI 的形态。"""
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(argv=("fake-cli", "-m", "m1", "-p", TASK_PLACEHOLDER)),
        )
        # 用 sys.executable 验证真实子进程收到的参数形态：
        # argv = [python, -c, dumper, {task}] → dumper 收 argv[1]=替换后的 prompt
        script = "import sys; open('argv.txt','w',encoding='utf-8').write(repr(sys.argv[1:]))"
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(argv=(sys.executable, "-c", script, TASK_PLACEHOLDER)),
        )
        input_ = make_input(str(tmp_path), prompt="HELLO_PROMPT")
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        import time

        deadline = time.monotonic() + budget(45.0)
        while time.monotonic() < deadline:
            if (tmp_path / "argv.txt").is_file():
                break
            time.sleep(0.1)
        got = (tmp_path / "argv.txt").read_text(encoding="utf-8")
        # {task} 被原位替换为 prompt——不是尾元素追加（否则会出现两个）
        assert got == "['HELLO_PROMPT']"
        adapter.cancel("att-t1", "收尾")
        adapter.collect("att-t1")

    def test_no_placeholder_appends_at_tail(self, tmp_path: Path) -> None:
        """无占位符：尾元素追加（cli-bridge 等位置参数 CLI 的既有形态，零破坏）。"""
        script = "import sys; open('argv.txt','w',encoding='utf-8').write(repr(sys.argv[1:]))"
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(argv=(sys.executable, "-c", script)),
        )
        input_ = make_input(str(tmp_path), prompt="TAIL_PROMPT")
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        import time

        deadline = time.monotonic() + budget(45.0)
        while time.monotonic() < deadline:
            if (tmp_path / "argv.txt").is_file():
                break
            time.sleep(0.1)
        got = (tmp_path / "argv.txt").read_text(encoding="utf-8")
        # prompt 作为 argv 尾元素到达（-c 的脚本参数后）
        assert got == "['TAIL_PROMPT']"
        adapter.cancel("att-t1", "收尾")
        adapter.collect("att-t1")

    def test_multiple_placeholders_refused(self, tmp_path: Path) -> None:
        """两个 {task} → 歧义，prepare 拒接（fail-closed）。"""
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(argv=("cli", "-p", TASK_PLACEHOLDER, "--x", TASK_PLACEHOLDER)),
        )
        input_ = make_input(str(tmp_path))
        with pytest.raises(Exception, match="占位符"):
            adapter.prepare(input_)

    def test_placeholder_without_prompt_left_as_is(self, tmp_path: Path) -> None:
        """无 task_prompt（如探针）→ {task} 原样留在 argv（prepare 已校验形状）。"""
        script = "import sys; open('argv.txt','w',encoding='utf-8').write(repr(sys.argv[1:]))"
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(argv=(sys.executable, "-c", script, TASK_PLACEHOLDER)),
        )
        input_ = make_input(str(tmp_path), prompt=None)
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        import time

        deadline = time.monotonic() + budget(45.0)
        while time.monotonic() < deadline:
            if (tmp_path / "argv.txt").is_file():
                break
            time.sleep(0.1)
        got = (tmp_path / "argv.txt").read_text(encoding="utf-8")
        assert "{task}" in got  # 未替换，如实透传
        adapter.cancel("att-t1", "收尾")
        adapter.collect("att-t1")

    def test_placeholder_is_fixed_vocabulary(self) -> None:
        """占位符是固定词表常量，不是模型可控数据。"""
        assert TASK_PLACEHOLDER == "{task}"

    def test_context_snapshot_path_passed_via_env(self, tmp_path: Path) -> None:
        """P1 review fix: SubprocessAdapter must hand the context snapshot
        path to the child process so the default executor can actually
        read the freshly built active.md (directives, handover, contract
        anchor). Without this, the snapshot is built and the cursor
        advances, but nothing on the executor side ever consumes it.

        The snapshot path is passed via the ``LHGP_CONTEXT_SNAPSHOT_PATH``
        environment variable — the same channel used for the per-attempt
        session token (``LHGP_SESSION_TOKEN``) — so no argv slot is
        burned and shell injection is impossible."""

        script = (
            "import os; open('env.txt','w',encoding='utf-8')"
            ".write(os.environ.get('LHGP_CONTEXT_SNAPSHOT_PATH', 'MISSING'))"
        )
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(argv=(sys.executable, "-c", script)),
        )
        snapshot_path = str(tmp_path / "context" / "att-t1" / "active.md")
        input_ = make_input(str(tmp_path), prompt="env-test")
        # AttemptInput doesn't expose context_snapshot_path as a public
        # field the test fixture knows about; use dataclasses.replace.
        import dataclasses

        input_ = dataclasses.replace(input_, context_snapshot_path=snapshot_path)
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        import time

        deadline = time.monotonic() + budget(45.0)
        while time.monotonic() < deadline:
            if (tmp_path / "env.txt").is_file():
                break
            time.sleep(0.1)
        got = (tmp_path / "env.txt").read_text(encoding="utf-8")
        assert got == snapshot_path
        adapter.cancel("att-t1", "收尾")
        adapter.collect("att-t1")

    def test_no_snapshot_path_no_env_leak(self, tmp_path: Path) -> None:
        """No context_snapshot_path (probe dispatch, no-with_context) → the
        ``LHGP_CONTEXT_SNAPSHOT_PATH`` env var is absent. Pinned so a
        future refactor can't quietly inject a stale snapshot path into
        probes that intentionally bypass context materialization."""

        script = (
            "import os; open('env.txt','w',encoding='utf-8')"
            ".write('PRESENT' if 'LHGP_CONTEXT_SNAPSHOT_PATH' in os.environ else 'ABSENT')"
        )
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(argv=(sys.executable, "-c", script)),
        )
        input_ = make_input(str(tmp_path), prompt="probe")
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        import time

        deadline = time.monotonic() + budget(45.0)
        while time.monotonic() < deadline:
            if (tmp_path / "env.txt").is_file():
                break
            time.sleep(0.1)
        got = (tmp_path / "env.txt").read_text(encoding="utf-8")
        assert got == "ABSENT"
        adapter.cancel("att-t1", "收尾")
        adapter.collect("att-t1")
