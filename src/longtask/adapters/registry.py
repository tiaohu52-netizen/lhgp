"""执行器注册表（DESIGN §8、§8.1、§8.2、§8.3、§12.4）。

把本机所有 Agent 应用抽象成可替换的执行资源池（DESIGN §1.1、§8）。
推动者从「已框定 ∧ 满足合同能力门槛 ∧ 有空闲并行额度」的成员中挑人，
按 cost_hint 从低到高分发；挑不中则进入 blocked(no-executor)（§8.3）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
from longtask.contracts.authority import to_dict as authority_to_dict
from longtask.contracts.schema import ContractDraft, Enforcement


def _strict_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    """Parse a registry boolean without truthiness coercion.

    Registry files are user-controlled configuration.  Values such as the
    string ``"false"`` must not silently enable an executor or capability;
    reject malformed values so the default-deny boundary remains intact.
    """
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


class CostHint(StrEnum):
    """执行器成本提示（DESIGN §8.1、§8.3）。

    分发决策按成本从低到高优先分发（LOW < MEDIUM < HIGH）。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def priority(self) -> int:
        """成本数值优先级，越小越优先。"""
        match self:
            case CostHint.LOW:
                return 1
            case CostHint.MEDIUM:
                return 2
            case CostHint.HIGH:
                return 3


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """结构化启动声明（DESIGN §8.1 registry 条目的 launch 字段）。

    argv 来自用户框定的注册表配置，是固定词表；不接收模型输出，
    不存在可拼接的 shell 字符串（DESIGN §12.1、§14 注入防线）。
    """

    argv: tuple[str, ...] = ()
    cwd: str | None = None  # None 表示绑定合同 workspace_root
    env_allowlist: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "env_allowlist": list(self.env_allowlist),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LaunchSpec:
        if not data:
            return cls()
        argv = tuple(str(x) for x in data.get("argv", ()))
        cwd = data.get("cwd")
        cwd_str = str(cwd) if cwd is not None else None
        env_allowlist = tuple(str(x) for x in data.get("env_allowlist", ()))
        return cls(argv=argv, cwd=cwd_str, env_allowlist=env_allowlist)


def _authority_bindings(
    contract: ContractDraft | dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], bool]:
    """提取合同的 authority 绑定表（SPEC §6.1、§6.3 条件 2）。

    返回 (allow_by_executor, deny_all)：
    - allow_by_executor: executor_id → {"models": set, "roles": set}；
    - deny_all: 合同是否声明了 authority.executors 绑定。True 时 match
      走 default-deny（不在 allow 列表即拒绝）；False（存量合同无绑定）
      保持旧行为——语义是「没有设防」，不是「全部拒绝」。

    dict 合同读 execution.authority（P1 前老形态）或 authority（SPEC §6.1）；
    dataclass 合同读 draft.authority（P2 起 authority 是独立字段）。
    """
    raw: Any
    if isinstance(contract, ContractDraft):
        raw = authority_to_dict(contract.authority)
    else:
        raw = contract.get("authority")
        if not isinstance(raw, dict) or not raw:
            execution = contract.get("execution")
            raw = execution.get("authority") if isinstance(execution, dict) else None

    allow: dict[str, dict[str, Any]] = {}
    bindings = raw.get("executors") if isinstance(raw, dict) else None
    if not isinstance(bindings, list):
        return allow, False
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        executor_id = str(binding.get("executor_id") or "").strip()
        if not executor_id:
            continue
        allow[executor_id] = {
            "models": {str(m) for m in binding.get("models") or ()},
            "roles": {str(r) for r in binding.get("roles") or ()},
        }
    # explicit_allow 本身就是一条安全边界：即使列表为空，也必须是空池，
    # 不能因为没有有效 binding 就退回开放模式。closed + 空列表继续保留
    # 存量合同的兼容语义（没有设防）。
    policy = str(raw.get("executor_policy", "closed")) if isinstance(raw, dict) else "closed"
    return allow, policy == "explicit_allow" or bool(allow)


def _models_overlap(allowed: set[str], available: tuple[str, ...]) -> bool:
    """判断合同模型 allowlist 与注册表模型声明是否有交集。"""
    if not allowed:
        return False
    if "*" in allowed or "*" in available:
        return True
    return bool(allowed.intersection(available))


def sandbox_capability_from_dict(data: dict[str, Any]) -> SandboxCapability:
    """从字典解析 SandboxCapability（DESIGN §12.4）。"""
    raw_enf = data.get("enforcement", "none")
    enf = (
        Enforcement(str(raw_enf)) if raw_enf in Enforcement._value2member_map_ else Enforcement.NONE
    )
    return SandboxCapability(
        file_effects=str(data.get("file_effects", "unsupported")),
        network=str(data.get("network", "unsupported")),
        process=str(data.get("process", "unsupported")),
        enforcement=enf,
    )


def capabilities_from_dict(data: dict[str, Any]) -> Capabilities:
    """从字典解析 Capabilities（DESIGN §12.4）。"""
    raw_sandbox = data.get("sandbox", {})
    return Capabilities(
        spawn=_strict_bool(data, "spawn", True),
        observe=_strict_bool(data, "observe", True),
        cancel=_strict_bool(data, "cancel", True),
        notify=_strict_bool(data, "notify", False),
        followup=_strict_bool(data, "followup", False),
        steer=_strict_bool(data, "steer", False),
        interrupt=_strict_bool(data, "interrupt", False),
        context=str(data.get("context", "optional")),
        sandbox=sandbox_capability_from_dict(raw_sandbox),
        acceptance_evidence=_strict_bool(data, "acceptance_evidence", False),
    )


def _declared_model_from_argv(argv: tuple[str, ...]) -> str | None:
    """从 launch.argv 解析声明模型：支持 ``--model X`` 与 ``--model=X``。"""
    for index, arg in enumerate(argv):
        if arg == "--model":
            if index + 1 < len(argv):
                value = str(argv[index + 1]).strip()
                if value:
                    return value
        elif arg.startswith("--model="):
            value = arg[len("--model=") :].strip()
            if value:
                return value
    return None


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """执行器注册表条目（DESIGN §8.1 registry.yaml）。"""

    id: str
    kind: str  # bridge | subprocess
    launch: LaunchSpec
    capabilities: Capabilities
    limits: dict[str, int] = field(default_factory=dict)
    cost_hint: CostHint = CostHint.MEDIUM
    enabled: bool = False  # 用户框定开关，默认全部 false（DESIGN §8.2）
    models: tuple[str, ...] = ("*",)  # 可由该 CLI 配置/启动的模型族
    declared_model: str | None = None  # launch.argv 里声明的模型（--model X / --model=X）

    def effective_model(self) -> str:
        """派工记录用模型：argv 声明优先，否则 models 首个非通配项，否则 "*"。

        记录必须与「实际执行」同源——CLI 实际跑的模型写在 launch.argv，
        记录若另取 models 列表首项，审计看到的就是配置副本而非事实。
        """
        if self.declared_model:
            return self.declared_model
        return next((m for m in self.models if m != "*"), "*")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "launch": self.launch.to_dict(),
            "capabilities": {
                "spawn": self.capabilities.spawn,
                "observe": self.capabilities.observe,
                "cancel": self.capabilities.cancel,
                "notify": self.capabilities.notify,
                "followup": self.capabilities.followup,
                "steer": self.capabilities.steer,
                "interrupt": self.capabilities.interrupt,
                "context": self.capabilities.context,
                "sandbox": {
                    "file_effects": self.capabilities.sandbox.file_effects,
                    "network": self.capabilities.sandbox.network,
                    "process": self.capabilities.sandbox.process,
                    "enforcement": (
                        self.capabilities.sandbox.enforcement.value
                        if isinstance(self.capabilities.sandbox.enforcement, Enforcement)
                        else str(self.capabilities.sandbox.enforcement)
                    ),
                },
                "acceptance_evidence": self.capabilities.acceptance_evidence,
            },
            "limits": dict(self.limits),
            "cost_hint": self.cost_hint.value,
            "enabled": self.enabled,
            "models": list(self.models),
            "declared_model": self.declared_model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegistryEntry:
        raw_id = str(data.get("id", "")).strip()
        if not raw_id:
            raise ValueError("registry entry requires non-empty 'id'")
        kind = str(data.get("kind", "subprocess"))
        launch = LaunchSpec.from_dict(data.get("launch"))
        capabilities = capabilities_from_dict(data.get("capabilities", {}))
        limits = {str(k): int(v) for k, v in data.get("limits", {}).items()}
        raw_cost = str(data.get("cost_hint", "medium")).lower()
        cost_hint = (
            CostHint(raw_cost) if raw_cost in CostHint._value2member_map_ else CostHint.MEDIUM
        )
        enabled = _strict_bool(data, "enabled", False)
        raw_models = data.get("models", ("*",))
        models = tuple(str(model).strip() for model in raw_models if str(model).strip())
        if not models:
            models = ("*",)
        declared_model = _declared_model_from_argv(launch.argv)
        # 声明模型一致性（fail loudly）：argv 里写死的模型必须被 models 白名单
        # 覆盖；反之，models 具体化却不在 argv 声明模型时，记录的模型会与
        # 实际执行的模型脱节——宁可拒绝条目，也不产出「看着对、跑得偏」的账。
        if declared_model is not None:
            if models != ("*",) and declared_model not in models:
                raise ValueError(
                    f"registry entry {raw_id!r}: launch.argv declares --model "
                    f"{declared_model!r} which is not in models {list(models)}"
                )
        elif models != ("*",):
            raise ValueError(
                f"registry entry {raw_id!r}: models {list(models)} requires an explicit "
                "--model in launch.argv (or models ['*'] when the runtime chooses the model)"
            )
        return cls(
            id=raw_id,
            kind=kind,
            launch=launch,
            capabilities=capabilities,
            limits=limits,
            cost_hint=cost_hint,
            enabled=enabled,
            models=models,
            declared_model=declared_model,
        )

    def to_manifest(self, adapter_version: str = "0.1.0") -> ExecutorManifest:
        """导出为执行器接入声明 manifest（DESIGN §12.4）。"""
        return ExecutorManifest(
            executor_id=self.id,
            adapter_version=adapter_version,
            transport=self.kind,
            capabilities=self.capabilities,
            limits=dict(self.limits),
        )


def check_capability_match(
    entry: RegistryEntry,
    contract: ContractDraft | dict[str, Any],
) -> tuple[bool, list[str]]:
    """检查执行器是否满足合同的能力门槛（DESIGN §8.2、§9）。

    返回 (is_match, list_of_reasons)。若 is_match 为 False，reasons 包含不满足项。
    """
    reasons: list[str] = []
    if isinstance(contract, ContractDraft):
        hard_constraints = contract.hard_constraints
        execution = contract.execution
        context_cfg = contract.context
    else:
        hard_constraints = contract.get("hard_constraints", {})
        execution = contract.get("execution", {})
        context_cfg = contract.get("context", {})

    caps = entry.capabilities

    # 1. 检查 execution.required_capabilities（DESIGN §4、§8.2）
    req_caps = execution.get("required_capabilities", []) if isinstance(execution, dict) else []
    for req in req_caps:
        match req:
            case "spawn":
                if not caps.spawn:
                    reasons.append("executor does not support spawn")
            case "observe":
                if not caps.observe:
                    reasons.append("executor does not support observe")
            case "cancel":
                if not caps.cancel:
                    reasons.append("executor does not support cancel")
            case "notify":
                if not caps.notify:
                    reasons.append("executor does not support notify")
            case "followup":
                if not caps.followup:
                    reasons.append("executor does not support followup")
            case "steer":
                if not caps.steer:
                    reasons.append("executor does not support steer")
            case "interrupt":
                if not caps.interrupt:
                    reasons.append("executor does not support interrupt")
            case "acceptance" | "acceptance_evidence":
                if not caps.acceptance_evidence:
                    reasons.append("executor does not support acceptance evidence")
            case "context":
                # 合同要求装配上下文，执行器不能声明不支持
                if caps.context not in ("required", "optional"):
                    reasons.append("executor does not support context mounting")
            case other:
                # 未知能力要求，无法满足
                reasons.append(f"unknown required capability: {other}")

    # 2. 检查 context.required（DESIGN §3.2、§4、§9）
    if (
        isinstance(context_cfg, dict)
        and context_cfg.get("required") is True
        and caps.context not in ("required", "optional")
    ):
        reasons.append("contract requires context mounting, but executor cannot provide it")

    # 3. 检查硬约束与沙箱兼容性（DESIGN §9 约束编译表）
    if isinstance(hard_constraints, dict):
        file_effects = hard_constraints.get("file_effects", {})
        file_mode = (
            file_effects.get("mode") if isinstance(file_effects, dict) else str(file_effects)
        )
        if file_mode in ("workspace-write", "read-only"):
            if caps.sandbox.file_effects == "unsupported":
                reasons.append(f"executor sandbox does not support file_effects mode '{file_mode}'")
            elif file_mode == "workspace-write" and caps.sandbox.file_effects != "workspace-write":
                reasons.append(
                    f"executor sandbox file_effects '{caps.sandbox.file_effects}' "
                    f"cannot satisfy '{file_mode}'"
                )

        network = hard_constraints.get("network", {})
        net_mode = network.get("mode") if isinstance(network, dict) else str(network)
        if net_mode == "deny" and caps.sandbox.network != "deny":
            reasons.append(
                "contract requires network.mode=deny, executor has no independent network denial"
            )

        process = hard_constraints.get("process", {})
        proc_mode = process.get("mode") if isinstance(process, dict) else str(process)
        if proc_mode in ("restricted", "deny") and caps.sandbox.process not in (
            "restricted",
            "deny",
        ):
            reasons.append(
                f"contract requires process mode '{proc_mode}', executor does not support it"
            )

    return len(reasons) == 0, reasons


class ExecutorRegistry:
    """执行器注册表（DESIGN §8、§8.1、§8.2、§8.3）。

    管理本机已注册执行器池，并提供能力匹配与分发决策。
    """

    def __init__(self, entries: list[RegistryEntry] | None = None) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        if entries:
            for entry in entries:
                self._entries[entry.id] = entry

    def register(self, entry: RegistryEntry) -> None:
        """注册或更新执行器条目（DESIGN §8.1）。"""
        self._entries[entry.id] = entry

    def unregister(self, executor_id: str) -> bool:
        """注销执行器条目。若存在返回 True，否则返回 False。"""
        return self._entries.pop(executor_id, None) is not None

    def get(self, executor_id: str) -> RegistryEntry | None:
        """获取执行器条目。"""
        return self._entries.get(executor_id)

    def set_enabled(self, executor_id: str, enabled: bool) -> bool:
        """设置执行器开启/关闭开关（DESIGN §8.2）。"""
        entry = self._entries.get(executor_id)
        if entry is None:
            return False
        if entry.enabled == enabled:
            return True
        # ``replace`` 而非手工重建：手工构造曾漏传 ``models``，于是任何一次
        # enable/disable 都把该执行器的模型白名单重置为 ``("*",)``，静默
        # 破坏合同 authority 的模型绑定（``match_candidates`` 用 models 做
        # default-deny 校验）。replace 让字段增删结构上不可能再漏。
        self._entries[executor_id] = replace(entry, enabled=enabled)
        return True

    def list_entries(self, *, enabled_only: bool = False) -> list[RegistryEntry]:
        """列出已注册执行器（按 id 排序）。"""
        result = [e for e in self._entries.values() if not enabled_only or e.enabled]
        return sorted(result, key=lambda x: x.id)

    def match_candidates(
        self,
        contract: ContractDraft | dict[str, Any],
        running_attempts: dict[str, int] | None = None,
        *,
        requested_role: str = "executor",
    ) -> list[RegistryEntry]:
        """筛选并按分发规则排序候选执行器（DESIGN §8.2、§8.3、SPEC §6.3）。

        规则：
        1. 必须已框定开启（enabled=True，§6.3 条件 1 globally_enabled）；
        2. 必须被合同显式授权（§6.3 条件 2 contract_explicitly_allows）：
           合同 authority.executors 声明了绑定 → 只允许绑定覆盖的
           executor/role 入候选（default-deny：不在 allow 列表即拒绝）；
           models 按 binding 校验（"*" 通配仅 Principal 显式选择）。
           未声明任何绑定的存量合同保持旧行为（全部 enabled 候选）——
           语义是「没有设防」而不是「全部拒绝」，SPEC §6.3 的
           default-deny 指的是设了 authority 的合同；
        3. 必须满足合同能力门槛（capabilities 匹配）；
        4. 必须有空闲并发额度（running < max_concurrent_attempts）；
        5. 排序：cost_hint 优先级（low < medium < high）、运行中任务数升序、id 字典序。
        """
        attempts_map = running_attempts or {}
        candidates: list[tuple[int, int, str, RegistryEntry]] = []
        allow_by_executor, deny_all = _authority_bindings(contract)

        for entry in self._entries.values():
            if not entry.enabled:
                continue

            if deny_all:
                binding = allow_by_executor.get(entry.id)
                if binding is None or requested_role not in binding["roles"]:
                    # §6.3 条件 2：合同设了 allow 列表而该执行器/角色不在其中
                    continue
                if not _models_overlap(binding["models"], entry.models):
                    continue

            matched, _ = check_capability_match(entry, contract)
            if not matched:
                continue

            max_concurrent = entry.limits.get("max_concurrent_attempts", 1)
            running = attempts_map.get(entry.id, 0)
            if running >= max_concurrent:
                continue

            candidates.append((entry.cost_hint.priority, running, entry.id, entry))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in candidates]

    def snapshot_for_admission(
        self,
        *,
        contract: ContractDraft | dict[str, Any] | None = None,
        running_attempts: dict[str, int] | None = None,
        requested_role: str = "executor",
        requested_model: str = "*",
    ) -> list[dict[str, Any]]:
        """为 admission offer 提供执行器快照（DESIGN §10.4 第 1 项）。

        不应用 §8.3 的 capability 筛选——交给 admission/eligibility.evaluate
        用 authority 决策；这里只把「事实」列出（enabled、并发、capability
        是否覆盖合同）供 7 条件判定。**事实层与策略层分离**：
        1. capability_satisfied = check_capability_match(entry, contract)
        2. constraint_enforcement_proven = entry.manifest.enforcement != "none"
        3. concurrency_available = running < max_concurrent_attempts
        4. globally_enabled = entry.enabled
        5. budget_available 留给 caller 注入（事件流计数）；本快照默认 True
        """
        attempts_map = running_attempts or {}
        snapshot: list[dict[str, Any]] = []
        for entry in self._entries.values():
            capability_satisfied = True
            if contract is not None:
                capability_satisfied, _ = check_capability_match(entry, contract)
            max_concurrent = entry.limits.get("max_concurrent_attempts", 1)
            running = attempts_map.get(entry.id, 0)
            try:
                enforcement = entry.capabilities.sandbox.enforcement.value
            except AttributeError:
                enforcement = "none"
            snapshot.append(
                {
                    "executor_id": entry.id,
                    # Admission is a fact snapshot: preserve the user's
                    # explicit registry switch instead of claiming every
                    # executor is globally enabled.
                    "enabled": entry.enabled,
                    "concurrency_available": running < max_concurrent,
                    "capability_satisfied": capability_satisfied,
                    "constraint_enforcement_proven": enforcement != "none",
                    "budget_available": True,
                    "verifier_independent": True,
                    "requested_model": requested_model,
                    "models": list(entry.models),
                    "requested_role": requested_role,
                }
            )
        return snapshot

    def select_candidate(
        self,
        contract: ContractDraft | dict[str, Any],
        running_attempts: dict[str, int] | None = None,
    ) -> RegistryEntry | None:
        """选择最佳候选执行器（DESIGN §8.3）。若无匹配返回 None。"""
        candidates = self.match_candidates(contract, running_attempts=running_attempts)
        return candidates[0] if candidates else None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典格式（对齐 DESIGN §8.1 registry.yaml 的 agents 列表）。"""
        return {"agents": [entry.to_dict() for entry in self.list_entries()]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutorRegistry:
        """从字典还原注册表（DESIGN §8.1）。"""
        agents_data = data.get("agents", [])
        entries = [RegistryEntry.from_dict(d) for d in agents_data]
        return cls(entries=entries)

    def to_json(self, indent: int = 2) -> str:
        """导出为 JSON 字符串。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> ExecutorRegistry:
        """从 JSON 字符串解析注册表。"""
        return cls.from_dict(json.loads(text))

    def save_to_file(self, path: Path) -> None:
        """持久化保存注册表至文件（支持 .json 或格式化存储）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load_from_file(cls, path: Path) -> ExecutorRegistry:
        """从文件加载注册表，若文件不存在返回空注册表。"""
        if not path.is_file():
            return cls()
        return cls.from_json(path.read_text(encoding="utf-8"))
