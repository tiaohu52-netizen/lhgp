"""适配器一致性场景（DESIGN §9、§12.1、§12.4、§14、§15.2）。

覆盖：
- 拒绝不静默降级（§9：编译失败默认拒接；§12.4 manifest 只是声明）
- deny_paths 越界与 ../ 路径穿越拒接（§14 威胁模型：归一化后比较）
- spawn 结构化 argv、cancel 宽限期升级 kill（§12.1）

fake 场景纯内存；subprocess 场景用 sys.executable 跑微型脚本，秒级完成。
对应 claim: refusal-never-degrades（quality/claims.json）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.base import AttemptInput, PreparedLaunch, PrepareRefusedError
from longtask.adapters.fake_executor import FAKE_MANIFEST, FakeAttemptScript, FakeExecutor
from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
from longtask.contracts.schema import AttemptRole, Enforcement

pytestmark = pytest.mark.conformance


def make_manifest(*, network: str = "unsupported") -> ExecutorManifest:
    """§8.1 codex 式 subprocess 执行器声明：cwd 绑定，无网络/进程沙箱。"""
    return ExecutorManifest(
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
                network=network,
                process="unsupported",
                enforcement=Enforcement.PARTIAL,
            ),
            acceptance_evidence=True,
        ),
        limits={"max_concurrent_attempts": 2},
    )


def make_input(
    workspace_root: str,
    hard_constraints: dict[str, Any] | None = None,
    *,
    attempt_id: str = "att-1",
    context_snapshot_path: str | None = None,
    task_prompt: str | None = None,
    max_output_bytes: int | None = None,
) -> AttemptInput:
    budget: dict[str, Any] = {"max_dispatches": 8}
    if max_output_bytes is not None:
        budget["max_output_bytes"] = max_output_bytes
    return AttemptInput(
        attempt_id=attempt_id,
        contract_id="lt-20260831-001",
        revision=1,
        lease_generation=1,
        role=AttemptRole.EXECUTOR,
        contract_snapshot={
            "objective": "一致性场景测试目标",
            "hard_constraints": hard_constraints if hard_constraints is not None else {},
            "context": {},
        },
        handover_path="handover.md",
        workspace_root=workspace_root,
        budget_remaining=budget,
        partition_id=None,
        context_snapshot_path=context_snapshot_path,
        task_prompt=task_prompt,
    )


def file_effects(root: str, deny: list[str] | None = None) -> dict[str, Any]:
    """§4 式 file_effects 硬约束。"""
    effects: dict[str, Any] = {"mode": "workspace-write", "workspace_root": root}
    if deny is not None:
        effects["deny_paths"] = deny
    return effects


def _child_env_allowlist() -> tuple[str, ...]:
    """子进程环境白名单：Windows 下 Python 启动依赖系统变量；其余平台按存在性过滤。"""
    return ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP")


class TestFakeExecutorRefusal:
    """§9/§12.4：声明 unsupported 的能力遇到对应硬约束 → 拒接，无静默降级。"""

    def test_unsupported_network_constraint_refuses_without_degrade(self, tmp_path: Path) -> None:
        executor = FakeExecutor()
        assert FAKE_MANIFEST.capabilities.sandbox.network == "unsupported"
        hard = {
            "file_effects": file_effects(str(tmp_path)),
            "network": {"mode": "deny"},
        }
        with pytest.raises(PrepareRefusedError, match="network"):
            executor.prepare(make_input(str(tmp_path), hard))
        # 拒接即未拉起：observe 找不到该 attempt，证明没有降级版 spawn
        with pytest.raises(KeyError):
            executor.observe("att-1")

    def test_unprovable_process_and_package_constraints_refuse(self, tmp_path: Path) -> None:
        executor = FakeExecutor()
        with pytest.raises(PrepareRefusedError, match="process"):
            executor.prepare(make_input(str(tmp_path), {"process": {"mode": "restricted"}}))
        with pytest.raises(PrepareRefusedError, match="package_install"):
            executor.prepare(make_input(str(tmp_path), {"package_install": {"mode": "deny"}}))

    def test_file_effects_mode_mismatch_refuses(self, tmp_path: Path) -> None:
        executor = FakeExecutor()
        hard = {"file_effects": {"mode": "read-only", "workspace_root": str(tmp_path)}}
        with pytest.raises(PrepareRefusedError, match="file_effects"):
            executor.prepare(make_input(str(tmp_path), hard))

    def test_required_context_without_snapshot_refuses(self, tmp_path: Path) -> None:
        """§9 context.policy：合同要求装配上下文但快照未物化 → 拒接。"""
        executor = FakeExecutor()
        input_ = AttemptInput(
            attempt_id="att-1",
            contract_id="lt-20260831-001",
            revision=1,
            lease_generation=1,
            role=AttemptRole.EXECUTOR,
            contract_snapshot={
                "objective": "测试目标",
                "hard_constraints": {},
                "context": {"required": True},
            },
            handover_path="handover.md",
            workspace_root=str(tmp_path),
            budget_remaining={"max_dispatches": 8},
        )
        with pytest.raises(PrepareRefusedError, match="context"):
            executor.prepare(input_)

    def test_prepare_refusal_script_short_circuits(self, tmp_path: Path) -> None:
        """剧本化拒接路径：prepare_refusal 优先于其他检查。"""
        executor = FakeExecutor(
            scripts={"att-x": FakeAttemptScript(prepare_refusal="剧本化拒接理由")}
        )
        with pytest.raises(PrepareRefusedError, match="剧本化拒接理由"):
            executor.prepare(make_input(str(tmp_path), attempt_id="att-x"))


class TestFakeExecutorScriptedLifecycle:
    """fake executor 的成功/失败/挂起/心跳/取消剧本（§15.2：验证协议错误路径）。"""

    def test_success_failure_and_collect(self) -> None:
        executor = FakeExecutor(
            scripts={
                "att-ok": FakeAttemptScript(stdout="done"),
                "att-bad": FakeAttemptScript(outcome="failed", returncode=3, stderr="boom"),
            }
        )
        launch = executor.prepare(make_input("/w", attempt_id="att-ok"))
        assert launch.enforcement == FAKE_MANIFEST.capabilities.sandbox.enforcement
        assert executor.spawn(make_input("/w", attempt_id="att-ok"), launch).startswith("fake:")
        assert executor.observe("att-ok")["state"] == "succeeded"
        result = executor.collect("att-ok")
        assert result["returncode"] == 0
        assert result["stdout"] == "done"

        executor.spawn(make_input("/w", attempt_id="att-bad"), launch)
        assert executor.observe("att-bad")["state"] == "failed"
        result = executor.collect("att-bad")
        assert result["returncode"] == 3
        assert result["stderr"] == "boom"

    def test_hang_heartbeat_and_cancel(self) -> None:
        executor = FakeExecutor(
            scripts={
                "att-hang": FakeAttemptScript(outcome="hang"),
                "att-silent": FakeAttemptScript(outcome="hang", heartbeat_silent=True),
                "att-reject": FakeAttemptScript(outcome="hang", cancel_accepted=False),
            }
        )
        launch = executor.prepare(make_input("/w", attempt_id="att-hang"))
        executor.spawn(make_input("/w", attempt_id="att-hang"), launch)
        observation = executor.observe("att-hang")
        assert observation["alive"] is True
        assert observation["heartbeat"] is True

        executor.cancel("att-hang", "测试取消")
        assert executor.observe("att-hang")["state"] == "cancelled"
        assert executor.collect("att-hang")["state"] == "cancelled"

        # 心跳中断：仍存活但 observe 报告无心跳（stale 线索，§14 崩溃恢复输入）
        executor.spawn(make_input("/w", attempt_id="att-silent"), launch)
        assert executor.observe("att-silent")["heartbeat"] is False

        # 剧本拒绝取消：如实报告 rejected，状态仍是 running
        executor.spawn(make_input("/w", attempt_id="att-reject"), launch)
        executor.cancel("att-reject", "测试取消")
        observation = executor.observe("att-reject")
        assert observation["state"] == "running"
        assert observation["cancel"] == "rejected"

    def test_cancel_of_finished_attempt_is_noop_not_cancelled(self) -> None:
        executor = FakeExecutor()
        launch = executor.prepare(make_input("/w"))
        executor.spawn(make_input("/w"), launch)
        executor.cancel("att-1", "迟到取消")
        assert executor.observe("att-1")["state"] == "succeeded"

    def test_duplicate_spawn_refused(self) -> None:
        executor = FakeExecutor()
        launch = executor.prepare(make_input("/w"))
        input_ = make_input("/w")
        executor.spawn(input_, launch)
        with pytest.raises(ValueError, match="重复 spawn"):
            executor.spawn(input_, launch)


class TestSubprocessPrepareRefusal:
    """§9 约束编译 + §14 路径穿越：翻不了的约束与越界路径一律拒接。"""

    def test_deny_path_inside_workspace_refused(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        hard = {"file_effects": file_effects(str(tmp_path), deny=[str(tmp_path / "secret")])}
        with pytest.raises(PrepareRefusedError, match="deny_paths"):
            adapter.prepare(make_input(str(tmp_path), hard))

    def test_deny_path_traversal_refused(self, tmp_path: Path) -> None:
        """../ 穿越变体：归一化后落回 workspace 内 → 拒接（DESIGN §14）。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        traversal = str(workspace / ".." / "workspace" / "secret")
        hard = {"file_effects": file_effects(str(workspace), deny=[traversal])}
        with pytest.raises(PrepareRefusedError, match="deny_paths"):
            adapter.prepare(make_input(str(workspace), hard))

    def test_deny_path_covering_workspace_refused(self, tmp_path: Path) -> None:
        """禁写区覆盖整个 workspace（自相矛盾）→ 拒接。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        hard = {"file_effects": file_effects(str(workspace), deny=[str(tmp_path) + "/**"])}
        with pytest.raises(PrepareRefusedError, match="deny_paths"):
            adapter.prepare(make_input(str(workspace), hard))

    def test_workspace_root_mismatch_refused(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        hard = {"file_effects": file_effects(str(tmp_path / "elsewhere"))}
        with pytest.raises(PrepareRefusedError, match="workspace_root 不一致"):
            adapter.prepare(make_input(str(tmp_path), hard))

    def test_relative_workspace_root_refused(self) -> None:
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        with pytest.raises(PrepareRefusedError, match="绝对路径"):
            adapter.prepare(make_input("relative/workspace"))

    def test_network_deny_without_manifest_policy_refused(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(
            make_manifest(network="unsupported"), launch=LaunchSpec(argv=(sys.executable,))
        )
        hard = {"network": {"mode": "deny"}}
        with pytest.raises(PrepareRefusedError, match=r"network\.mode=deny"):
            adapter.prepare(make_input(str(tmp_path), hard))

    def test_process_and_package_install_refused(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        with pytest.raises(PrepareRefusedError, match=r"process\.mode=restricted"):
            adapter.prepare(make_input(str(tmp_path), {"process": {"mode": "restricted"}}))
        with pytest.raises(PrepareRefusedError, match="package_install"):
            adapter.prepare(make_input(str(tmp_path), {"package_install": {"mode": "deny"}}))

    def test_read_only_file_effects_refused(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        hard = {"file_effects": {"mode": "read-only", "workspace_root": str(tmp_path)}}
        with pytest.raises(PrepareRefusedError, match=r"file_effects\.mode"):
            adapter.prepare(make_input(str(tmp_path), hard))

    def test_missing_launch_argv_refused(self, tmp_path: Path) -> None:
        """没配结构化 argv 就没有可拉起的声明 → 拒接（fail-closed）。"""
        adapter = SubprocessAdapter(make_manifest())
        assert adapter.health() is False
        with pytest.raises(PrepareRefusedError, match="launch argv 未配置"):
            adapter.prepare(make_input(str(tmp_path)))

    def test_prepare_returns_structured_launch(self, tmp_path: Path) -> None:
        argv = (sys.executable, "-c", "pass")
        adapter = SubprocessAdapter(
            make_manifest(), launch=LaunchSpec(argv=argv, env_allowlist=("PATH",))
        )
        assert adapter.health() is True
        hard = {"file_effects": file_effects(str(tmp_path))}
        launch = adapter.prepare(make_input(str(tmp_path), hard))
        assert launch.argv == argv
        assert launch.cwd is not None
        assert Path(launch.cwd).resolve() == tmp_path.resolve()
        assert launch.env_allowlist == ("PATH",)
        # cwd 绑定不是沙箱：实测只到 partial（DESIGN §12.4）
        assert launch.enforcement == Enforcement.PARTIAL


@pytest.mark.integration
class TestSubprocessSpawnLifecycle:
    """§12.1 结构化 spawn：sys.executable 微型脚本，秒级完成。"""

    def test_spawn_structured_argv_writes_artifact(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        script = 'import pathlib; pathlib.Path("artifact.txt").write_text("ok", encoding="utf-8")'
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, "-c", script),
                env_allowlist=_child_env_allowlist(),
            ),
        )
        hard = {"file_effects": file_effects(str(workspace))}
        launch = adapter.prepare(make_input(str(workspace), hard))
        session_ref = adapter.spawn(make_input(str(workspace), hard), launch)
        assert session_ref.startswith("subprocess:att-1:")

        result = adapter.collect("att-1")
        assert result["returncode"] == 0
        assert result["state"] == "succeeded"
        # 子进程确实以 workspace 为 cwd：artifact 落在工作区内
        assert (workspace / "artifact.txt").read_text(encoding="utf-8") == "ok"

    def test_spawn_appends_task_prompt_as_single_argv_tail(self, tmp_path: Path) -> None:
        """task_prompt 作为单个 argv 尾元素原样到达子进程（§12.1：无 shell 拼接面）。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        out_path = workspace / "argv.txt"
        script = (
            "import sys, pathlib;"
            "pathlib.Path(sys.argv[1]).write_text('|'.join(sys.argv[2:]), encoding='utf-8')"
        )
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, "-c", script, str(out_path)),
                env_allowlist=_child_env_allowlist(),
            ),
        )
        hard = {"file_effects": file_effects(str(workspace))}
        launch = adapter.prepare(make_input(str(workspace), hard))
        # 任务文本含 shell 元字符：整体作为单个参数传递，不拆分、不被解释
        prompt = "完成 X; run 'rm -rf /' && echo done"
        session_ref = adapter.spawn(make_input(str(workspace), hard, task_prompt=prompt), launch)
        assert session_ref.startswith("subprocess:att-1:")

        result = adapter.collect("att-1")
        assert result["state"] == "succeeded"
        assert out_path.read_text(encoding="utf-8") == prompt

    def test_spawn_blank_task_prompt_refused(self, tmp_path: Path) -> None:
        """空白 task_prompt 没有可交付的任务文本 -> 拒接，不拉空会话。"""
        adapter = SubprocessAdapter(
            make_manifest(), launch=LaunchSpec(argv=(sys.executable, "-c", "pass"))
        )
        launch = adapter.prepare(make_input(str(tmp_path)))
        with pytest.raises(PrepareRefusedError, match="task_prompt"):
            adapter.spawn(make_input(str(tmp_path), task_prompt="   "), launch)

    def test_cancel_terminates_hanging_process(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(60)"),
                env_allowlist=_child_env_allowlist(),
            ),
            grace_period_seconds=0.5,
        )
        launch = adapter.prepare(make_input(str(tmp_path)))
        adapter.spawn(make_input(str(tmp_path)), launch)
        assert adapter.observe("att-1")["alive"] is True

        adapter.cancel("att-1", "测试取消")
        assert adapter.observe("att-1")["alive"] is False
        result = adapter.collect("att-1")
        assert result["state"] == "cancelled"
        assert result["returncode"] != 0

    def test_collect_reports_that_output_was_sanitized(self, tmp_path: Path) -> None:
        """清洗改写了证据的字节，这件事必须在 collect 结果里可见（§14.2）。

        可观察性是保证的一部分：审计必须能区分「执行者的原始输出」与
        「协议改写过的输出」，否则清洗就是静默改写证据。
        """
        # 载荷 = ESC[31m + "red" + ESC[0m + U+202E + " evil" + LF。
        # 用 hex 构造：源码里不留不可见字符，review 时才读得出来。
        payload = bytes.fromhex("1b5b33316d7265641b5b306de280ae206576696c0a")
        script = "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))"
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, "-c", script, payload.hex()),
                env_allowlist=_child_env_allowlist(),
            ),
        )
        launch = adapter.prepare(make_input(str(tmp_path)))
        adapter.spawn(make_input(str(tmp_path)), launch)
        result = adapter.collect("att-1")
        stdout = str(result["stdout"])
        assert chr(27) not in stdout
        assert chr(0x202E) not in stdout
        assert "red evil" in stdout
        assert result["output_sanitized"] is True

    def test_collect_reports_clean_output_as_not_sanitized(self, tmp_path: Path) -> None:
        """旗子必须能区分真假——恒为 True 的旗子是假信号，比没有更糟。"""
        script = "import sys; sys.stdout.write('plain output')"
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, "-c", script), env_allowlist=_child_env_allowlist()
            ),
        )
        launch = adapter.prepare(make_input(str(tmp_path)))
        adapter.spawn(make_input(str(tmp_path)), launch)
        result = adapter.collect("att-1")
        assert result["output_sanitized"] is False

    def test_collect_reports_output_truncation(self, tmp_path: Path) -> None:
        """超出输出预算时截断必须可见，且保留字节不超过预算（§6.3 硬边界）。"""
        budget = 100
        script = "import sys; sys.stdout.write('x' * 4000)"
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, "-c", script), env_allowlist=_child_env_allowlist()
            ),
        )
        attempt_input = make_input(str(tmp_path), max_output_bytes=budget)
        launch = adapter.prepare(attempt_input)
        adapter.spawn(attempt_input, launch)
        result = adapter.collect("att-1")
        assert result["output_truncated"] is True
        assert len(str(result["stdout"])) <= budget
        assert result["output_sanitized"] is False

    def test_spawn_rejects_forged_launch_cwd(self, tmp_path: Path) -> None:
        """§14 适配器侧防线：伪造的 PreparedLaunch（cwd 越出 workspace）不能拉起。"""
        argv = (sys.executable, "-c", "pass")
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=argv))
        forged = PreparedLaunch(
            argv=argv,
            cwd=str(tmp_path / "outside"),
            env_allowlist=(),
            enforcement=Enforcement.PARTIAL,
        )
        with pytest.raises(PrepareRefusedError, match="越出 workspace_root"):
            adapter.spawn(make_input(str(tmp_path)), forged)

    def test_spawn_rejects_empty_argv(self, tmp_path: Path) -> None:
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        launch = adapter.prepare(make_input(str(tmp_path)))
        forged = PreparedLaunch(
            argv=(), cwd=launch.cwd, env_allowlist=(), enforcement=Enforcement.PARTIAL
        )
        with pytest.raises(PrepareRefusedError, match=r"launch\.argv 为空"):
            adapter.spawn(make_input(str(tmp_path)), forged)

    def test_unknown_attempt_observation_raises(self) -> None:
        adapter = SubprocessAdapter(make_manifest(), launch=LaunchSpec(argv=(sys.executable,)))
        with pytest.raises(KeyError):
            adapter.observe("never-spawned")


class TestVerifierVerdictUnderBudget:
    """预算截断 vs 末尾判定块（SPEC §12.4 通道 2 的完整语义）。

    判定块约定在 stdout 末尾，输出预算保留头部：截断发生时块必丢失。
    这个事实必须**可区分地**暴露，不能退化成「verifier 没写」——
    载荷用临时脚本文件构造，避免 -c 的多层转义（本测试就是那样踩过坑的）。
    """

    def _write_script(self, workspace: Path, *, noise_lines: int) -> Path:
        script = workspace / "verifier.py"
        script.write_text(
            chr(10).join(
                [
                    "import sys",
                    *[f"sys.stdout.write('noise line {i}' + chr(10))" for i in range(noise_lines)],
                    "sys.stdout.write('```' + 'lhgp-verdict' + chr(10))",
                    'sys.stdout.write(\'{"verdict": "succeeded", "checks": '
                    '[{"check_id": "c1", "outcome": "pass"}]}\' + chr(10))',
                    "sys.stdout.write('```' + chr(10))",
                ]
            ),
            encoding="utf-8",
        )
        return script

    def test_budget_truncation_raises_verdict_source_loss(self, tmp_path: Path) -> None:
        """截断丢块 → VerdictSourceLossError，绝不静默当「没写」。"""
        from lhgp.acceptance.verdict import VerdictSourceLossError, verdict_from_output

        script = self._write_script(tmp_path, noise_lines=1000)
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, str(script)), env_allowlist=_child_env_allowlist()
            ),
        )
        attempt_input = make_input(str(tmp_path), max_output_bytes=4096)
        launch = adapter.prepare(attempt_input)
        adapter.spawn(attempt_input, launch)
        result = adapter.collect("att-1")
        assert result["output_truncated"] is True
        with pytest.raises(VerdictSourceLossError, match="truncated by budget"):
            verdict_from_output(str(result["stdout"]), output_truncated=True)

    def test_untruncated_verdict_still_parses(self, tmp_path: Path) -> None:
        """无预算（或块在截断点前）→ 正常解析，通道 2 不因守护而失效。"""
        from lhgp.acceptance.verdict import verdict_from_output

        script = self._write_script(tmp_path, noise_lines=10)
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, str(script)), env_allowlist=_child_env_allowlist()
            ),
        )
        attempt_input = make_input(str(tmp_path))
        launch = adapter.prepare(attempt_input)
        adapter.spawn(attempt_input, launch)
        result = adapter.collect("att-1")
        assert result["output_truncated"] is False
        verdict = verdict_from_output(str(result["stdout"]), output_truncated=False)
        assert verdict is not None
        assert verdict.verdict == "succeeded"

    def test_absent_block_without_truncation_stays_none(self, tmp_path: Path) -> None:
        """未截断且无块 → None 保留原语义（verifier 没写就是没写）。"""
        from lhgp.acceptance.verdict import verdict_from_output

        script = tmp_path / "plain.py"
        script.write_text("import sys\nsys.stdout.write('just work' + chr(10))\n", encoding="utf-8")
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, str(script)), env_allowlist=_child_env_allowlist()
            ),
        )
        attempt_input = make_input(str(tmp_path))
        launch = adapter.prepare(attempt_input)
        adapter.spawn(attempt_input, launch)
        result = adapter.collect("att-1")
        assert verdict_from_output(str(result["stdout"]), output_truncated=False) is None


@pytest.mark.integration
class TestStdoutChannelEncoding:
    """SPEC §12.4「通道编码」：stdout 协议通道 MUST 为 UTF-8。

    回归（修复前必红）：适配器一直按 UTF-8 解码子进程 stdout，却从不保证
    子进程按 UTF-8 输出。中文 Windows 上解释器默认 cp936，非 ASCII 证据
    （verifier 的 details、含中文的路径）会被 errors="replace" 静默打成
    U+FFFD——损坏不可逆且不报错，只在断言恰好漏过时表现为「跑通了但证据
    是乱码」。断言「无替换符」而非只断言子串，是为了让任何一种编码错配
    都会红，而不只是碰巧撞上的那一种。
    """

    DETAILS = "交付物存在且含标记"

    def test_non_ascii_verdict_details_survive_the_pipe(self, tmp_path: Path) -> None:
        from lhgp.acceptance.verdict import parse_verdict_block

        check_id = "file-exists:result.txt"
        # 与 examples/local-no-model/checker.py 同形：中文说明 + ensure_ascii=False
        verdict = {
            "verdict": "succeeded",
            "checks": [
                {
                    "check_id": check_id,
                    "outcome": "pass",
                    "source": "ws/result.txt",
                    "details": self.DETAILS,
                }
            ],
        }
        script = tmp_path / "checker.py"
        script.write_text(
            "import json\n"
            "print('核验完成。')\n"
            "print('```lhgp-verdict')\n"
            f"print(json.dumps({verdict!r}, ensure_ascii=False))\n"
            "print('```')\n",
            encoding="utf-8",
        )
        adapter = SubprocessAdapter(
            make_manifest(),
            launch=LaunchSpec(
                argv=(sys.executable, str(script)), env_allowlist=_child_env_allowlist()
            ),
        )
        attempt_input = make_input(str(tmp_path))
        launch = adapter.prepare(attempt_input)
        adapter.spawn(attempt_input, launch)
        result = adapter.collect("att-1")
        assert result["returncode"] == 0
        stdout = str(result["stdout"])
        assert "\ufffd" not in stdout, f"stdout 出现替换符，编码错配未被拦下：{stdout!r}"
        assert self.DETAILS in stdout
        parsed = parse_verdict_block(stdout)
        assert parsed is not None
        assert parsed.checks[check_id]["details"] == self.DETAILS
