"""fake executor（DESIGN §15.2 Developer Preview 必备）。

用途：不拉起任何真实进程，验证协议错误路径与崩溃恢复——
拒接、fencing、租约回收、预算触顶、Deadline 仲裁。
测试纪律（CONTRIBUTING）：conformance 场景只许用它，不许 mock 被测对象。

纯内存实现：无子进程、无网络、无文件系统、无墙钟。
行为由 FakeAttemptScript 逐 attempt 脚本化（成功/失败/挂起/心跳中断/拒接），
prepare 同时按 FAKE_MANIFEST 声明的能力做约束检查——声明 unsupported 的
能力遇到对应合同硬约束时拒接，绝不静默降级（DESIGN §9、§12.4）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from longtask.adapters.base import (
    AttemptInput,
    ExecutorAdapter,
    PreparedLaunch,
    PrepareRefusedError,
)
from longtask.adapters.handles import RECOVERY_NONRECOVERABLE, ExternalRunHandle
from longtask.adapters.manifest import (
    Capabilities,
    ExecutorManifest,
    SandboxCapability,
)
from longtask.contracts.schema import AttemptState, Enforcement

FAKE_MANIFEST = ExecutorManifest(
    executor_id="fake-executor",
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
    limits={"max_concurrent_attempts": 4},
)

_OUTCOMES = frozenset({"succeeded", "failed", "hang"})


@dataclass(frozen=True, slots=True)
class FakeAttemptScript:
    """单次 attempt 的脚本化行为（默认：成功、退出码 0、可取消）。

    - outcome：succeeded / failed / hang（挂起，模拟卡死会话）
    - heartbeat_silent：hang 时模拟心跳中断（observe 报告无心跳 → stale 线索）
    - prepare_refusal：非空则 prepare 以该理由拒接（剧本化拒接路径）
    """

    outcome: str = "succeeded"
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    cancel_accepted: bool = True
    heartbeat_silent: bool = False
    prepare_refusal: str | None = None

    def validate(self) -> None:
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"未知 outcome（只许 succeeded/failed/hang）: {self.outcome!r}")


class FakeExecutor(ExecutorAdapter):
    """行为可脚本化的假执行器，驱动 §14 各保证场景。

    能力以 FAKE_MANIFEST 为准（诚实声明：network/process 均 unsupported，
    enforcement=partial）。合同硬约束超出声明能力 → prepare 拒接；
    不满足剧本的取消请求被如实拒绝，observe 里仍报告真实状态。
    """

    def __init__(
        self,
        scripts: Mapping[str, FakeAttemptScript] | None = None,
        default_script: FakeAttemptScript | None = None,
        reattachable_runs: Mapping[str, str] | None = None,
    ) -> None:
        """构造假执行器。

        reattachable_runs：external_run_id → session_locator 映射，模拟
        「守护进程重启后仍联系得到的外部 run」。默认为空 —— 纯内存适配器
        重启后本来就无法证明任何东西，reattach 必须失败并走 orphan grace；
        想走通 reattach 分支必须显式声明，不白给能力（§9 绝不静默降级）。
        """
        self._scripts: dict[str, FakeAttemptScript] = dict(scripts) if scripts else {}
        for script in self._scripts.values():
            script.validate()
        self._default_script = default_script if default_script is not None else FakeAttemptScript()
        self._default_script.validate()
        self._reattachable: dict[str, str] = dict(reattachable_runs or {})
        self._spawned: set[str] = set()
        self._cancelled: set[str] = set()
        self._cancel_rejected: set[str] = set()
        self._collected: set[str] = set()

    @property
    def id(self) -> str:
        return FAKE_MANIFEST.executor_id

    def describe(self) -> ExecutorManifest:
        return FAKE_MANIFEST

    def health(self) -> bool:
        return True

    def prepare(self, input_: AttemptInput) -> PreparedLaunch:
        """按剧本与声明能力检查合同约束；翻不了 → PrepareRefusedError（拒接）。"""
        script = self._script_for(input_.attempt_id)
        if script.prepare_refusal is not None:
            raise PrepareRefusedError(f"capability missing: {script.prepare_refusal}")
        reasons: list[str] = []
        hard = input_.contract_snapshot.get("hard_constraints", {})
        if not isinstance(hard, dict):
            raise PrepareRefusedError("capability missing: hard_constraints 必须是对象")
        sandbox = FAKE_MANIFEST.capabilities.sandbox
        file_effects = hard.get("file_effects")
        if isinstance(file_effects, dict):
            mode = file_effects.get("mode")
            if mode is not None and mode != sandbox.file_effects:
                reasons.append(
                    f"file_effects.mode={mode!r} 与声明能力 {sandbox.file_effects!r} 不符"
                )
        network = hard.get("network")
        if (
            isinstance(network, dict)
            and network.get("mode") == "deny"
            and sandbox.network != "deny"
        ):
            reasons.append(f"network.mode=deny 但 manifest 声明 network={sandbox.network!r}")
        process = hard.get("process")
        if (
            isinstance(process, dict)
            and process.get("mode") == "restricted"
            and sandbox.process != "restricted"
        ):
            reasons.append(f"process.mode=restricted 但 manifest 声明 process={sandbox.process!r}")
        package_install = hard.get("package_install")
        if isinstance(package_install, dict) and package_install.get("mode") == "deny":
            # manifest 能力字段表里没有 package_install 一栏：无从声明即无法证明
            reasons.append("package_install.mode=deny 无法证明：manifest 无对应能力声明")
        context = input_.contract_snapshot.get("context")
        if (
            isinstance(context, dict)
            and context.get("required") is True
            and not input_.context_snapshot_path
        ):
            reasons.append("context.required=true 但 AttemptInput 未携带 context_snapshot_path")
        if reasons:
            raise PrepareRefusedError("capability missing: " + "; ".join(reasons))
        return PreparedLaunch(
            argv=("fake-executor",),
            cwd=input_.workspace_root or None,
            env_allowlist=(),
            # 与 manifest 声明一致：fake 的实际能力就是它声明的能力
            enforcement=sandbox.enforcement,
            context_snapshot_path=input_.context_snapshot_path,
        )

    def spawn(self, input_: AttemptInput, launch: PreparedLaunch) -> str:
        """登记 attempt 为已拉起（纯内存），返回伪 session_ref。"""
        if input_.attempt_id in self._spawned:
            raise ValueError(f"attempt 已拉起，拒绝重复 spawn: {input_.attempt_id}")
        self._spawned.add(input_.attempt_id)
        return f"fake:{input_.attempt_id}"

    def run_handle(self, attempt_id: str) -> ExternalRunHandle | None:
        """持久返回句柄（§11.3）。

        纯内存适配器：spawn 期间能给出句柄，但 recovery_strategy 如实声明
        nonrecoverable —— 进程一退就再也联系不上，reconcile 不许假装能重绑。
        """
        if attempt_id not in self._spawned:
            return None
        return ExternalRunHandle(
            external_run_id=f"fake-run-{attempt_id}",
            session_locator=attempt_id,
            recovery_strategy=RECOVERY_NONRECOVERABLE,
            capability_snapshot={"transport": "fake", "observe": "in-memory"},
            process_identity={},
        )

    def reattach(self, handle: ExternalRunHandle) -> bool:
        """按句柄重新绑定（§11.3 分支 1）；只有显式声明过的 run 才认。"""
        locator = self._reattachable.get(handle.external_run_id)
        if locator is None or locator != handle.session_locator:
            return False
        self._spawned.add(handle.session_locator)
        return True

    def observe(self, attempt_id: str) -> dict[str, object]:
        """观察脚本化状态：运行中/已收尾/已取消，附心跳线索。"""
        script = self._require_script(attempt_id)
        if attempt_id in self._cancelled:
            return {
                "state": AttemptState.CANCELLED.value,
                "alive": False,
                "returncode": None,
            }
        if script.outcome == "hang":
            observation: dict[str, object] = {
                "state": AttemptState.RUNNING.value,
                "alive": True,
                "returncode": None,
                "heartbeat": not script.heartbeat_silent,
            }
            if attempt_id in self._cancel_rejected:
                observation["cancel"] = "rejected"
            return observation
        state = AttemptState.SUCCEEDED if script.outcome == "succeeded" else AttemptState.FAILED
        return {"state": state.value, "alive": False, "returncode": script.returncode}

    def cancel(self, attempt_id: str, reason: str) -> None:
        """按剧本取消挂起中的 attempt；拒绝取消时如实记录，不伪装成功。"""
        script = self._require_script(attempt_id)
        if attempt_id in self._cancelled:
            return  # 幂等：已取消
        if script.outcome != "hang" or not script.cancel_accepted:
            # 已按剧本收尾，或剧本声明拒绝取消：无可取消/拒绝，保留原状态
            self._cancel_rejected.add(attempt_id)
            return
        self._cancelled.add(attempt_id)

    def collect(self, attempt_id: str) -> dict[str, object]:
        """回收脚本化结果；挂起/已取消的 attempt 如实报告尚无结果。"""
        script = self._require_script(attempt_id)
        self._collected.add(attempt_id)
        if attempt_id in self._cancelled:
            return {
                "state": AttemptState.CANCELLED.value,
                "returncode": None,
                "stdout": "",
                "stderr": "",
            }
        if script.outcome == "hang":
            return {
                "state": AttemptState.RUNNING.value,
                "returncode": None,
                "stdout": "",
                "stderr": "",
            }
        state = AttemptState.SUCCEEDED if script.outcome == "succeeded" else AttemptState.FAILED
        return {
            "state": state.value,
            "returncode": script.returncode,
            "stdout": script.stdout,
            "stderr": script.stderr,
        }

    def _script_for(self, attempt_id: str) -> FakeAttemptScript:
        """取 attempt 剧本；未单独剧本化的 attempt 走默认剧本。"""
        return self._scripts.get(attempt_id, self._default_script)

    def _require_script(self, attempt_id: str) -> FakeAttemptScript:
        """只对已 spawn 的 attempt 提供观察/取消/回收；未知 attempt 报 KeyError。"""
        if attempt_id not in self._spawned:
            raise KeyError(f"未知 attempt: {attempt_id}")
        return self._script_for(attempt_id)


__all__ = ["FAKE_MANIFEST", "FakeAttemptScript", "FakeExecutor", "PrepareRefusedError"]
