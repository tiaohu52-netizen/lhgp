"""CLI 兼容性：管道排水防死锁 + harness 终态事件（v2/v3/v4 教训的根治）。

真实子进程验证两类兼容性缺口：
1. **管道死锁（真 bug 级）**：PIPE 约 64KB 缓冲写满 → 子进程 write 阻塞 →
   永不退出。后台 reader 线程持续排水，1MB 输出不卡死；
2. **终态时机（观察窗口问题的根治）**：harness 主进程与内部 worker 生命
   周期不对齐（cli-bridge 实测）——stdout 结构化事件
   {"event":"attempt/finished","outcome":...} 让「干完了」由 harness 主动
   声明：主进程还活着也能判终态、collect 不等退出码也能结算；
3. **成功语义**：CLI 软失败退 0——事件 outcome=failed 与退出码 0 矛盾
   时以事件为准并标注 exit_code_conflict。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from longtask.adapters.base import AttemptInput
from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
from longtask.contracts.schema import AttemptRole, Enforcement
from tests.wait_budget import budget

pytestmark = pytest.mark.conformance

# 1MB 输出（远超 64KB 管道缓冲）
BIG_OUTPUT = (
    "import sys\n"
    "for i in range(1024):\n"
    "    sys.stdout.write('x' * 1024)\n"
    "sys.stdout.flush()\n"
    "print('DONE')\n"
)

# harness 形态：先干活输出，写终态事件行，然后「收尾」sleep 很久
# （模拟 cli-bridge 主进程在 worker turn/end 之后才退出）
HARNESS_WITH_EVENT = (
    "import sys, time\n"
    "sys.stdout.write('working...\\n')\n"
    "sys.stdout.flush()\n"
    'sys.stdout.write(\'{"event":"attempt/finished","outcome":"succeeded"}\\n\')\n'
    "sys.stdout.flush()\n"
    "time.sleep(30)  # 模拟 harness 收尾期：主进程仍活着\n"
)

# 软失败形态：事件说 failed，但进程最后退 0
HARNESS_SOFT_FAIL = (
    "import sys\n"
    'sys.stdout.write(\'{"event":"attempt/finished","outcome":"failed"}\\n\')\n'
    "sys.stdout.flush()\n"
    "sys.exit(0)  # CLI 软失败也退 0\n"
)


def make_adapter() -> SubprocessAdapter:
    manifest = ExecutorManifest(
        executor_id="compat-cli",
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
    return SubprocessAdapter(
        manifest,
        launch=LaunchSpec(argv=(sys.executable, "-c"), env_allowlist=("PATH", "SYSTEMROOT")),
    )


def make_input(workspace: str, prompt: str) -> AttemptInput:
    return AttemptInput(
        attempt_id="att-c1",
        contract_id="lt-c1",
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


class TestPipeDrainNoDeadlock:
    def test_big_output_does_not_deadlock(self, tmp_path: Path) -> None:
        """1MB stdout 不卡死子进程（无排水时 write 满管道会永久阻塞）。"""
        adapter = make_adapter()
        input_ = make_input(str(tmp_path), BIG_OUTPUT)
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        try:
            deadline = time.monotonic() + budget(60.0)
            obs = adapter.observe("att-c1")
            while time.monotonic() < deadline:
                obs = adapter.observe("att-c1")
                if obs["state"] != "running":
                    break
                time.sleep(0.2)
            # 1MB 输出写完 + 进程正常退出：没死锁
            assert obs["state"] == "succeeded", f"state={obs}"
            collected = adapter.collect("att-c1")
            assert collected["returncode"] == 0
            # 输出完整回收（1MB + DONE；Windows 下 print 尾部是 \r\n）
            assert len(collected["stdout"]) >= 1024 * 1024
            assert collected["stdout"].rstrip("\r\n").endswith("DONE")
        finally:
            adapter.cancel("att-c1", "测试收尾")


class TestFinishedEvent:
    def test_event_declares_terminal_while_process_alive(self, tmp_path: Path) -> None:
        """终态事件已到、主进程仍活着（收尾 sleep 30s）→ observe 判 succeeded。

        这是 v2/v3/v4 观察窗口问题的根治形态：不等主进程退出。
        """
        adapter = make_adapter()
        input_ = make_input(str(tmp_path), HARNESS_WITH_EVENT)
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        try:
            deadline = time.monotonic() + budget(60.0)
            obs = {"state": "running"}
            while time.monotonic() < deadline:
                obs = adapter.observe("att-c1")
                if obs.get("finished_by_event"):
                    break
                time.sleep(0.2)
            # 事件判定生效：主进程仍活着（alive=True）但终态已定
            assert obs["finished_by_event"] is True
            assert obs["state"] == "succeeded"
            assert obs["alive"] is True  # 主进程还在 sleep(30) 收尾
            assert obs["event_outcome"] == "succeeded"
        finally:
            adapter.cancel("att-c1", "测试收尾")

    def test_collect_without_process_exit(self, tmp_path: Path) -> None:
        """终态事件已到 → collect 直接结算（不等主进程退出），输出完整。"""
        adapter = make_adapter()
        input_ = make_input(str(tmp_path), HARNESS_WITH_EVENT)
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        try:
            deadline = time.monotonic() + budget(60.0)
            while time.monotonic() < deadline:
                obs = adapter.observe("att-c1")
                if obs.get("finished_by_event"):
                    break
                time.sleep(0.2)
            collected = adapter.collect("att-c1")
            # 事件结算：退出码不可得（主进程没退）但终态明确
            assert collected["state"] == "succeeded"
            assert collected["finished_by_event"] is True
            assert collected["exit_code_known"] is False
            assert "working..." in collected["stdout"]
        finally:
            adapter.cancel("att-c1", "测试收尾")

    def test_soft_failure_exit_zero_event_wins(self, tmp_path: Path) -> None:
        """事件 outcome=failed + 退出码 0（CLI 软失败）→ failed 优先并标注矛盾。"""
        adapter = make_adapter()
        input_ = make_input(str(tmp_path), HARNESS_SOFT_FAIL)
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        deadline = time.monotonic() + budget(60.0)
        while time.monotonic() < deadline:
            obs = adapter.observe("att-c1")
            if obs["state"] != "running":
                break
            time.sleep(0.2)
        # 事件与退出码都到：事件赢
        assert obs["state"] == "failed"
        assert obs.get("exit_code_conflict") is True
        assert obs["returncode"] == 0
        collected = adapter.collect("att-c1")
        assert collected["state"] == "failed"

    def test_no_event_falls_back_to_exit_code(self, tmp_path: Path) -> None:
        """无事件行的 CLI（flag 型/普通命令）：退出码语义完全不变（零破坏）。"""
        adapter = make_adapter()
        input_ = make_input(str(tmp_path), "print('plain'); raise SystemExit(0)")
        prepared = adapter.prepare(input_)
        adapter.spawn(input_, prepared)
        deadline = time.monotonic() + budget(60.0)
        while time.monotonic() < deadline:
            obs = adapter.observe("att-c1")
            if obs["state"] != "running":
                break
            time.sleep(0.2)
        assert obs["state"] == "succeeded"
        assert "finished_by_event" not in obs
        collected = adapter.collect("att-c1")
        assert collected["returncode"] == 0
        assert "plain" in collected["stdout"]
