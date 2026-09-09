"""SubprocessAdapter 重绑生命周期与句柄持久化（SPEC §11.3）。

覆盖：
- spawn 后 run_handle 给出「pid + 启动时间」身份与 reattach 策略；
- 重启模拟：新适配器实例按持久句柄 reattach → 观察存活（管道已丢）；
- pid 复用 / 缺启动时间 → 拒绝重绑（身份不可证明）；
- _DetachedProcess 观察语义：活着 running / 不在 = failed 且退出码不可得；
- detached collect 如实报错，绝不编退出码。

真实子进程（sys.executable sleep），秒级完成；「重启」用第二个适配器
实例模拟——原 Popen 已不在后者内存里，这正是跨进程重启的现场。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from longtask.adapters.handles import (
    RECOVERY_REATTACH,
    ExternalRunHandle,
)
from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
from longtask.contracts.schema import AttemptState, Enforcement
from tests.wait_budget import budget

pytestmark = pytest.mark.integration

SLEEP_30 = "import time; time.sleep(30)"


def make_adapter() -> SubprocessAdapter:
    manifest = ExecutorManifest(
        executor_id="test-cli",
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
    return SubprocessAdapter(manifest, launch=LaunchSpec(argv=(sys.executable, "-c")))


def spawn_sleeping(adapter: SubprocessAdapter, attempt_id: str, workspace: Path) -> None:
    """拉起 sleep(30) 子进程：测试期间保持存活。"""
    from longtask.adapters.base import AttemptInput

    input_ = AttemptInput(
        attempt_id=attempt_id,
        contract_id=f"lt-{attempt_id}",
        revision=1,
        lease_generation=1,
        role="executor",
        contract_snapshot={
            "objective": "测试",
            "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        },
        handover_path="handover.md",
        workspace_root=str(workspace),
        budget_remaining={},
        task_prompt=SLEEP_30,
    )
    prepared = adapter.prepare(input_)
    adapter.spawn(input_, prepared)


class TestRunHandle:
    def test_spawn_returns_handle_with_pid_and_start_time(self, tmp_path: Path) -> None:
        """spawn 成功 → run_handle 持久给出身份四元组（§11.3 MUST）。"""
        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h1", tmp_path)
        try:
            handle = adapter.run_handle("att-h1")
            assert handle is not None
            assert handle.recovery_strategy == RECOVERY_REATTACH
            assert handle.session_locator == "att-h1"
            pid = handle.process_identity.get("pid")
            start = handle.process_identity.get("start_time")
            assert isinstance(pid, (int, float)) and pid > 0
            # 启动时间必须真的取到了——只有 pid 的句柄过不了 reattach
            assert start is not None
            # capability_snapshot 如实声明重绑后能力边界
            assert handle.capability_snapshot["collect"] == "unavailable"
            assert handle.capability_snapshot["observe"] == "liveness"
        finally:
            adapter.cancel("att-h1", "测试收尾")
            adapter.collect("att-h1")

    def test_run_handle_unknown_attempt_is_none(self) -> None:
        adapter = make_adapter()
        assert adapter.run_handle("att-never") is None


class TestReattach:
    def test_same_run_reattaches_and_observes_alive(self, tmp_path: Path) -> None:
        """分支 1 前提：重启后（新实例）能确认同一 run 活着。"""
        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h2", tmp_path)
        try:
            handle = adapter.run_handle("att-h2")
            assert handle is not None
            # 模拟守护进程重启：全新适配器实例，无内存 Popen
            reborn = make_adapter()
            assert reborn.reattach(handle) is True
            observation = reborn.observe("att-h2")
            assert observation["state"] == AttemptState.RUNNING.value
            assert observation["alive"] is True
            # 管道已丢：退出码不可得必须如实声明
            assert observation["exit_code_known"] is False
        finally:
            adapter.cancel("att-h2", "测试收尾")
            adapter.collect("att-h2")

    def test_reattach_dead_run_binds_and_settles_terminal(self, tmp_path: Path) -> None:
        """外部 run 已终止：身份证明通过仍绑定（True），observe 报终态。"""
        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h3", tmp_path)
        handle = adapter.run_handle("att-h3")
        assert handle is not None
        adapter.cancel("att-h3", "测试收尾")
        adapter.collect("att-h3")
        reborn = make_adapter()
        # 身份证明通过即绑定：不因「已终止」拒绝——那会把确认的终态
        # 降级成「状态未知」，白烧一轮 orphan grace
        assert reborn.reattach(handle) is True
        observation = reborn.observe("att-h3")
        assert observation["state"] == AttemptState.FAILED.value
        assert observation["exit_code_known"] is False

    def test_reattach_pid_reuse_detected(self, tmp_path: Path) -> None:
        """pid 被复用（启动时间对不上）→ 拒绝重绑，绝不认错 run。"""
        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h4", tmp_path)
        try:
            handle = adapter.run_handle("att-h4")
            assert handle is not None
            forged = ExternalRunHandle(
                external_run_id=handle.external_run_id,
                session_locator="att-h4",
                recovery_strategy=RECOVERY_REATTACH,
                capability_snapshot=handle.capability_snapshot,
                # 声称是 1 小时前启动的同 pid：pid 复用现场
                process_identity={
                    "pid": handle.process_identity["pid"],
                    "start_time": time.time() - 3600.0,
                },
            )
            assert make_adapter().reattach(forged) is False
        finally:
            adapter.cancel("att-h4", "测试收尾")
            adapter.collect("att-h4")

    def test_reattach_without_start_time_refused(self, tmp_path: Path) -> None:
        """只有 pid：PID 不得单独作为身份真相 → 拒绝。"""
        handle = ExternalRunHandle(
            external_run_id="123",
            session_locator="att-h5",
            recovery_strategy=RECOVERY_REATTACH,
            process_identity={"pid": 123},
        )
        assert make_adapter().reattach(handle) is False

    def test_reattach_missing_pid_refused(self) -> None:
        handle = ExternalRunHandle(
            external_run_id="x",
            session_locator="att-h6",
            recovery_strategy=RECOVERY_REATTACH,
            process_identity={},
        )
        assert make_adapter().reattach(handle) is False

    def test_reattach_bad_pid_type_refused(self) -> None:
        handle = ExternalRunHandle(
            external_run_id="x",
            session_locator="att-h7",
            recovery_strategy=RECOVERY_REATTACH,
            process_identity={"pid": "not-a-number", "start_time": 1.0},
        )
        assert make_adapter().reattach(handle) is False


class TestDetachedObservation:
    def test_detached_run_exit_observed_as_failed_no_exit_code(self, tmp_path: Path) -> None:
        """重绑后进程退出：确认非同一 run/已终止 → failed 且退出码不可得。"""
        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h8", tmp_path)
        handle = adapter.run_handle("att-h8")
        assert handle is not None
        reborn = make_adapter()
        assert reborn.reattach(handle) is True
        # 让外部 run 自行终止
        from longtask.adapters.processes import terminate_pid

        assert terminate_pid(int(handle.process_identity["pid"])) is True
        deadline = time.monotonic() + budget(45.0)
        while time.monotonic() < deadline:
            obs = reborn.observe("att-h8")
            if obs["state"] != AttemptState.RUNNING.value:
                break
            time.sleep(0.2)
        assert obs["state"] == AttemptState.FAILED.value
        assert obs["returncode"] is None
        assert obs["exit_code_known"] is False
        assert obs["error_class"] == "external-run-exit-unrecoverable"
        # The original adapter still owns the Popen wrapper; collect closes its
        # streams even though the process was terminated out-of-band.
        adapter.collect("att-h8")

    def test_detached_collect_raises_not_fabricates(self, tmp_path: Path) -> None:
        """detached collect：管道已丢，如实抛错，绝不编退出码或空输出。"""
        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h9", tmp_path)
        try:
            handle = adapter.run_handle("att-h9")
            assert handle is not None
            reborn = make_adapter()
            assert reborn.reattach(handle) is True
            with pytest.raises(RuntimeError, match="unrecoverable"):
                reborn.collect("att-h9")
        finally:
            adapter.cancel("att-h9", "测试收尾")
            adapter.collect("att-h9")

    def test_detached_cancel_terminates_best_effort(self, tmp_path: Path) -> None:
        """重绑进程取消：尽力 terminate；退出后按 cancelled 结算；幂等不抛错。"""
        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h10", tmp_path)
        try:
            handle = adapter.run_handle("att-h10")
            assert handle is not None
            reborn = make_adapter()
            assert reborn.reattach(handle) is True
            reborn.cancel("att-h10", "测试取消")
            deadline = time.monotonic() + budget(45.0)
            while time.monotonic() < deadline:
                obs = reborn.observe("att-h10")
                if obs["state"] != AttemptState.RUNNING.value:
                    break
                time.sleep(0.2)
            # cancel 登记在案：终止后如实报 cancelled（用户主动行为，非 failed）
            assert obs["state"] == AttemptState.CANCELLED.value
            # 幂等：再取消不抛错
            reborn.cancel("att-h10", "重复取消")
        finally:
            adapter.cancel("att-h10", "测试收尾")
            adapter.collect("att-h10")


class TestLiveProcessIdentity:
    def test_spawned_pid_identity_round_trip(self, tmp_path: Path) -> None:
        """spawn 记录的身份能通过 identity_matches（同一进程判定）。"""
        from longtask.adapters.processes import identity_matches

        adapter = make_adapter()
        spawn_sleeping(adapter, "att-h11", tmp_path)
        try:
            handle = adapter.run_handle("att-h11")
            assert handle is not None
            pid = int(handle.process_identity["pid"])
            start = float(handle.process_identity["start_time"])
            assert identity_matches(pid, start) is True
        finally:
            adapter.cancel("att-h11", "测试收尾")
            adapter.collect("att-h11")
