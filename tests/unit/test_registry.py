"""执行器注册表单元测试（DESIGN §8、§8.1、§8.2、§8.3、§12.4）。

测试覆盖：
1. CostHint 排序优先级与枚举解析；
2. LaunchSpec 序列化与反序列化；
3. RegistryEntry 创建、默认开关（enabled=False）、Manifest 导出；
4. ExecutorRegistry 注册、注销、启用/禁用开关；
5. check_capability_match 能力门槛匹配；
6. match_candidates / select_candidate 分发决策排序；
7. 字典与 JSON 文件持久化。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from longtask.adapters.factory import build_adapter
from longtask.adapters.fake_executor import FakeExecutor
from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
    check_capability_match,
)
from longtask.adapters.subprocess_adapter import SubprocessAdapter
from longtask.contracts.schema import Enforcement

pytestmark = pytest.mark.unit


def make_sandbox(
    file_effects: str = "workspace-write",
    network: str = "deny",
    process: str = "restricted",
    enforcement: Enforcement = Enforcement.PARTIAL,
) -> SandboxCapability:
    return SandboxCapability(
        file_effects=file_effects,
        network=network,
        process=process,
        enforcement=enforcement,
    )


def make_caps(
    spawn: bool = True,
    observe: bool = True,
    cancel: bool = True,
    notify: bool = False,
    followup: bool = False,
    steer: bool = False,
    interrupt: bool = False,
    context: str = "optional",
    sandbox: SandboxCapability | None = None,
    acceptance_evidence: bool = True,
) -> Capabilities:
    return Capabilities(
        spawn=spawn,
        observe=observe,
        cancel=cancel,
        notify=notify,
        followup=followup,
        steer=steer,
        interrupt=interrupt,
        context=context,
        sandbox=sandbox or make_sandbox(),
        acceptance_evidence=acceptance_evidence,
    )


def make_entry(
    executor_id: str,
    *,
    kind: str = "subprocess",
    cost_hint: CostHint = CostHint.MEDIUM,
    enabled: bool = False,
    max_concurrent_attempts: int = 1,
    capabilities: Capabilities | None = None,
) -> RegistryEntry:
    return RegistryEntry(
        id=executor_id,
        kind=kind,
        launch=LaunchSpec(argv=("codex", "exec"), cwd=None, env_allowlist=("API_KEY",)),
        capabilities=capabilities or make_caps(),
        limits={"max_concurrent_attempts": max_concurrent_attempts},
        cost_hint=cost_hint,
        enabled=enabled,
    )


class TestCostHint:
    def test_priority_order(self) -> None:
        assert CostHint.LOW.priority < CostHint.MEDIUM.priority < CostHint.HIGH.priority

    def test_values(self) -> None:
        assert CostHint("low") is CostHint.LOW
        assert CostHint("medium") is CostHint.MEDIUM
        assert CostHint("high") is CostHint.HIGH


class TestLaunchSpec:
    def test_to_dict_and_from_dict(self) -> None:
        spec = LaunchSpec(
            argv=("cli-bridge", "--headless"),
            cwd="/workspace",
            env_allowlist=("cli-bridge_CONFIG",),
        )
        d = spec.to_dict()
        assert d == {
            "argv": ["cli-bridge", "--headless"],
            "cwd": "/workspace",
            "env_allowlist": ["cli-bridge_CONFIG"],
        }
        restored = LaunchSpec.from_dict(d)
        assert restored == spec

    def test_from_dict_none_or_empty(self) -> None:
        assert LaunchSpec.from_dict(None) == LaunchSpec()
        assert LaunchSpec.from_dict({}) == LaunchSpec()


class TestRegistryEntry:
    def test_default_disabled(self) -> None:
        entry = make_entry("agent-1")
        assert not entry.enabled

    def test_to_dict_and_from_dict(self) -> None:
        entry = make_entry(
            "codex-main", cost_hint=CostHint.LOW, enabled=True, max_concurrent_attempts=2
        )
        d = entry.to_dict()
        assert d["id"] == "codex-main"
        assert d["cost_hint"] == "low"
        assert d["enabled"] is True
        assert d["limits"]["max_concurrent_attempts"] == 2

        restored = RegistryEntry.from_dict(d)
        assert restored.id == "codex-main"
        assert restored.cost_hint == CostHint.LOW
        assert restored.enabled is True
        assert restored.limits == {"max_concurrent_attempts": 2}
        assert restored.capabilities.spawn is True

    def test_from_dict_missing_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-empty 'id'"):
            RegistryEntry.from_dict({"id": ""})

    def test_from_dict_rejects_non_boolean_enabled(self) -> None:
        with pytest.raises(TypeError, match="enabled must be a boolean"):
            RegistryEntry.from_dict({"id": "agent-1", "enabled": "false"})

    def test_to_manifest(self) -> None:
        entry = make_entry("agent-bridge", kind="bridge")
        manifest = entry.to_manifest(adapter_version="0.2.0")
        assert manifest.executor_id == "agent-bridge"
        assert manifest.adapter_version == "0.2.0"
        assert manifest.transport == "bridge"


class TestCapabilitiesParsing:
    def test_rejects_non_boolean_capability(self) -> None:
        with pytest.raises(TypeError, match="spawn must be a boolean"):
            RegistryEntry.from_dict({"id": "agent-1", "capabilities": {"spawn": "false"}})


class TestCapabilityMatch:
    def test_all_matched(self) -> None:
        entry = make_entry(
            "e1", capabilities=make_caps(spawn=True, observe=True, cancel=True, context="required")
        )
        contract_dict = {
            "execution": {"required_capabilities": ["spawn", "observe", "cancel", "context"]},
            "hard_constraints": {
                "file_effects": {"mode": "workspace-write"},
                "network": {"mode": "deny"},
                "process": {"mode": "restricted"},
            },
            "context": {"required": True},
        }
        matched, reasons = check_capability_match(entry, contract_dict)
        assert matched
        assert reasons == []

    def test_missing_required_capability(self) -> None:
        entry = make_entry("e1", capabilities=make_caps(steer=False))
        contract_dict = {
            "execution": {"required_capabilities": ["spawn", "steer"]},
        }
        matched, reasons = check_capability_match(entry, contract_dict)
        assert not matched
        assert any("steer" in r for r in reasons)

    def test_sandbox_network_mismatch(self) -> None:
        sandbox = make_sandbox(network="unsupported")
        entry = make_entry("e1", capabilities=make_caps(sandbox=sandbox))
        contract_dict = {
            "hard_constraints": {"network": {"mode": "deny"}},
        }
        matched, reasons = check_capability_match(entry, contract_dict)
        assert not matched
        assert any("network" in r for r in reasons)

    def test_sandbox_file_effects_mismatch(self) -> None:
        sandbox = make_sandbox(file_effects="unsupported")
        entry = make_entry("e1", capabilities=make_caps(sandbox=sandbox))
        contract_dict = {
            "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        }
        matched, reasons = check_capability_match(entry, contract_dict)
        assert not matched
        assert any("file_effects" in r for r in reasons)


class TestExecutorRegistry:
    def test_register_and_get(self) -> None:
        reg = ExecutorRegistry()
        entry = make_entry("agent-1")
        reg.register(entry)
        assert reg.get("agent-1") == entry
        assert reg.get("non-existent") is None

    def test_unregister(self) -> None:
        reg = ExecutorRegistry()
        entry = make_entry("agent-1")
        reg.register(entry)
        assert reg.unregister("agent-1") is True
        assert reg.get("agent-1") is None
        assert reg.unregister("agent-1") is False

    def test_set_enabled(self) -> None:
        reg = ExecutorRegistry()
        reg.register(make_entry("agent-1", enabled=False))
        assert reg.get("agent-1") is not None
        assert not reg.get("agent-1").enabled  # type: ignore[union-attr]

        assert reg.set_enabled("agent-1", True) is True
        assert reg.get("agent-1").enabled  # type: ignore[union-attr]

        assert reg.set_enabled("agent-1", True) is True
        assert reg.set_enabled("unknown", True) is False

    def test_set_enabled_preserves_every_other_field(self) -> None:
        """开关执行器只能改 enabled——不得顺带重置任何其它字段。

        回归：``set_enabled`` 曾手工重建 ``RegistryEntry`` 且漏传 ``models``，
        于是任何一次 enable/disable 都把模型白名单重置为 ``("*",)``，
        静默破坏合同 authority 的 default-deny 模型绑定
        （``match_candidates`` 用 ``models`` 判定候选合法性）。
        """
        entry = RegistryEntry(
            id="agent-models",
            kind="subprocess",
            launch=LaunchSpec(argv=("codex", "exec"), cwd=None, env_allowlist=("API_KEY",)),
            capabilities=make_caps(),
            limits={"max_concurrent_attempts": 3},
            cost_hint=CostHint.HIGH,
            enabled=False,
            models=("gpt-5.6-sol", "gpt-5.6-terra"),
        )
        reg = ExecutorRegistry()
        reg.register(entry)

        assert reg.set_enabled("agent-models", True) is True
        after_enable = reg.get("agent-models")
        assert after_enable is not None
        assert after_enable.models == ("gpt-5.6-sol", "gpt-5.6-terra")
        assert after_enable.enabled is True
        # 除 enabled 外逐字段等价（防未来新增字段时再次漏传）
        assert replace(after_enable, enabled=False) == entry

        assert reg.set_enabled("agent-models", False) is True
        after_disable = reg.get("agent-models")
        assert after_disable is not None
        assert after_disable.models == ("gpt-5.6-sol", "gpt-5.6-terra")
        assert after_disable == entry

    def test_list_entries(self) -> None:
        reg = ExecutorRegistry()
        reg.register(make_entry("b-agent", enabled=False))
        reg.register(make_entry("a-agent", enabled=True))
        reg.register(make_entry("c-agent", enabled=True))

        all_entries = reg.list_entries(enabled_only=False)
        assert [e.id for e in all_entries] == ["a-agent", "b-agent", "c-agent"]

        enabled_entries = reg.list_entries(enabled_only=True)
        assert [e.id for e in enabled_entries] == ["a-agent", "c-agent"]

    def test_match_and_select_candidate(self) -> None:
        reg = ExecutorRegistry()
        # 1. 未开启的 agent 不入池
        reg.register(make_entry("e-disabled", cost_hint=CostHint.LOW, enabled=False))
        # 2. 开启但能力不匹配
        unsupported_sb = make_sandbox(file_effects="unsupported")
        reg.register(
            make_entry(
                "e-nomatch",
                cost_hint=CostHint.LOW,
                enabled=True,
                capabilities=make_caps(sandbox=unsupported_sb),
            )
        )
        # 3. 开启且匹配，高成本
        reg.register(
            make_entry("e-high", cost_hint=CostHint.HIGH, enabled=True, max_concurrent_attempts=2)
        )
        # 4. 开启且匹配，低成本
        reg.register(
            make_entry("e-low", cost_hint=CostHint.LOW, enabled=True, max_concurrent_attempts=1)
        )
        # 5. 开启且匹配，中成本
        reg.register(
            make_entry("e-med", cost_hint=CostHint.MEDIUM, enabled=True, max_concurrent_attempts=2)
        )

        contract_dict = {
            "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        }

        # 初始选择：低成本优先
        selected = reg.select_candidate(contract_dict)
        assert selected is not None
        assert selected.id == "e-low"

        # e-low 并发占满（running=1 >= max=1），转为选择 e-med
        candidates = reg.match_candidates(contract_dict, running_attempts={"e-low": 1})
        assert [c.id for c in candidates] == ["e-med", "e-high"]

        # e-low, e-med 均占满，转为选择 e-high
        selected = reg.select_candidate(contract_dict, running_attempts={"e-low": 1, "e-med": 2})
        assert selected is not None
        assert selected.id == "e-high"

        # 全部占满，无候选者
        selected = reg.select_candidate(
            contract_dict, running_attempts={"e-low": 1, "e-med": 2, "e-high": 2}
        )
        assert selected is None

    def test_serialization_and_file_io(self, tmp_path: Path) -> None:
        reg = ExecutorRegistry()
        reg.register(make_entry("codex-1", cost_hint=CostHint.LOW, enabled=True))
        reg.register(make_entry("cli-bridge-1", cost_hint=CostHint.MEDIUM, enabled=False))

        file_path = tmp_path / "registry.json"
        reg.save_to_file(file_path)
        assert file_path.is_file()

        loaded = ExecutorRegistry.load_from_file(file_path)
        assert len(loaded.list_entries()) == 2
        assert loaded.get("codex-1") is not None
        assert loaded.get("codex-1").enabled  # type: ignore[union-attr]
        assert loaded.get("cli-bridge-1") is not None
        assert not loaded.get("cli-bridge-1").enabled  # type: ignore[union-attr]

    def test_admission_snapshot_preserves_enabled_switch(self) -> None:
        reg = ExecutorRegistry(
            [
                make_entry("enabled", enabled=True),
                make_entry("disabled", enabled=False),
            ]
        )

        snapshot = {item["executor_id"]: item for item in reg.snapshot_for_admission()}

        assert snapshot["enabled"]["enabled"] is True
        assert snapshot["disabled"]["enabled"] is False


class TestBuildAdapter:
    """kind → 适配器默认构造（DESIGN §12）：调度层只经公开协议，不感知具体实现。"""

    def test_subprocess_entry_builds_subprocess_adapter(self) -> None:
        entry = make_entry("codex-1", enabled=True)
        adapter = build_adapter(entry)
        assert isinstance(adapter, SubprocessAdapter)
        assert adapter.id == "codex-1"

    def test_fake_entry_builds_fake_executor(self) -> None:
        entry = make_entry("fake-1", kind="fake", enabled=True)
        adapter = build_adapter(entry)
        assert isinstance(adapter, FakeExecutor)

    def test_unknown_kind_returns_none(self) -> None:
        """未知 kind 返回 None：分发侧按拒接处理（DESIGN §9 fail-closed，不猜）。"""
        entry = make_entry("ghost-1", kind="bridge", enabled=True)
        assert build_adapter(entry) is None
