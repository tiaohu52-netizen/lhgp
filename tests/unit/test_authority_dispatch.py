"""合同级执行器授权（SPEC §6.1 authority、§6.3 条件 2）单元测试。

「远期目标的权限」的分发面强制：
- 合同声明 authority.executors 绑定 → default-deny：不在 allow 列表的
  执行器/角色不入候选（§6.3：新注册执行器默认不得自动加入既有合同）；
- 绑定的 roles 决定该执行器能当 executor 还是 verifier；
- 绑定的 models 只认显式列表或 "*" 通配；
- 存量合同（无绑定）语义是「没有设防」，不是「全部拒绝」——保持旧行为。

对应产品愿景第 2 条：用户设立目标时选择哪些 CLI/模型能被唤起。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.contracts.schema import ContractDraft, Enforcement

pytestmark = pytest.mark.unit


def make_entry(
    executor_id: str,
    *,
    cost_hint: CostHint = CostHint.MEDIUM,
    enabled: bool = False,
) -> RegistryEntry:
    caps = Capabilities(
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
    )
    return RegistryEntry(
        id=executor_id,
        kind="subprocess",
        launch=LaunchSpec(argv=("codex", "exec"), cwd=None, env_allowlist=("API_KEY",)),
        capabilities=caps,
        limits={"max_concurrent_attempts": 1},
        cost_hint=cost_hint,
        enabled=enabled,
    )


def build_registry() -> ExecutorRegistry:
    """三个 enabled 执行器：a（low）、b（medium）、c（high）。"""
    reg = ExecutorRegistry()
    reg.register(make_entry("exec-a", cost_hint=CostHint.LOW, enabled=True))
    reg.register(make_entry("exec-b", cost_hint=CostHint.MEDIUM, enabled=True))
    reg.register(make_entry("exec-c", cost_hint=CostHint.HIGH, enabled=True))
    return reg


def contract_with_authority(authority: dict[str, Any]) -> dict[str, Any]:
    return {
        "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        "authority": authority,
    }


class TestAuthorityFilter:
    def test_binding_allowlist_is_default_deny(self) -> None:
        """合同只 allow exec-b → 其余 enabled 执行器全部不入候选。"""
        reg = build_registry()
        contract = contract_with_authority(
            {
                "executor_policy": "explicit_allow",
                "executors": [
                    {
                        "executor_id": "exec-b",
                        "models": ["m-1"],
                        "roles": ["executor"],
                    }
                ],
            }
        )
        assert [c.id for c in reg.match_candidates(contract)] == ["exec-b"]

    def test_unbound_contract_keeps_legacy_open_behavior(self) -> None:
        """存量合同（无 authority 绑定）：没有设防 ≠ 全部拒绝。"""
        reg = build_registry()
        contract = {"hard_constraints": {"file_effects": {"mode": "workspace-write"}}}
        assert [c.id for c in reg.match_candidates(contract)] == [
            "exec-a",
            "exec-b",
            "exec-c",
        ]

    def test_empty_executors_list_is_not_a_fence(self) -> None:
        """authority 存在但 executors 空：视为未设防（不能把所有合同搞死）。"""
        reg = build_registry()
        contract = contract_with_authority({"executor_policy": "closed", "executors": []})
        assert len(reg.match_candidates(contract)) == 3

    def test_role_verifier_excluded_when_roles_only_executor(self) -> None:
        """roles=['executor']：requested_role='verifier' 时该执行器被拒。"""
        reg = build_registry()
        contract = contract_with_authority(
            {
                "executors": [
                    {"executor_id": "exec-a", "models": ["m"], "roles": ["executor"]},
                    {"executor_id": "exec-b", "models": ["m"], "roles": ["verifier"]},
                ]
            }
        )
        # verifier 派发视角：只有 exec-b 合法
        assert [c.id for c in reg.match_candidates(contract, requested_role="verifier")] == [
            "exec-b"
        ]
        # executor 派发视角：只有 exec-a 合法
        assert [c.id for c in reg.match_candidates(contract, requested_role="executor")] == [
            "exec-a"
        ]

    def test_dataclass_contract_authority_respected(self) -> None:
        """ContractDraft（内部路径）的 authority 同样被强制。"""
        from datetime import UTC, datetime

        from longtask.contracts.authority import Authority, AuthorityBinding
        from longtask.contracts.schema import Acceptance, Budget

        draft = ContractDraft(
            title="授权测试合同",
            objective="验证 dataclass 路径的授权过滤",
            deadline_at=datetime(2026, 9, 8, 18, 0, 0, tzinfo=UTC),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="测试", checks=("通过",)),
            workload_initial_hours=2.0,
            budget=Budget(
                max_dispatches=5,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=60,
                max_output_bytes=1048576,
            ),
            authority=Authority(
                executor_policy="explicit_allow",
                executors=(
                    AuthorityBinding(executor_id="exec-c", models=("m",), roles=("executor",)),
                ),
            ),
        )
        reg = build_registry()
        assert [c.id for c in reg.match_candidates(draft)] == ["exec-c"]

    def test_legacy_execution_authority_dict_respected(self) -> None:
        """P1 前老形态：authority 挂在 execution 下也要被读到。"""
        reg = build_registry()
        contract = {
            "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
            "execution": {
                "authority": {
                    "executors": [{"executor_id": "exec-a", "models": ["m"], "roles": ["executor"]}]
                }
            },
        }
        assert [c.id for c in reg.match_candidates(contract)] == ["exec-a"]

    def test_unknown_executor_in_binding_is_ignored(self) -> None:
        """绑定指向不存在的执行器：不报错，候选为空（如实没人在 allow 里）。"""
        reg = build_registry()
        contract = contract_with_authority(
            {"executors": [{"executor_id": "exec-nope", "models": ["m"], "roles": ["executor"]}]}
        )
        assert reg.match_candidates(contract) == []

    def test_disabled_executor_still_never_dispatched(self) -> None:
        """授权与全局开关独立：allow 了但 registry 关着 → 仍不派（§6.3 条件 1）。"""
        reg = ExecutorRegistry()
        reg.register(make_entry("exec-a", enabled=False))
        contract = contract_with_authority(
            {"executors": [{"executor_id": "exec-a", "models": ["m"], "roles": ["executor"]}]}
        )
        assert reg.match_candidates(contract) == []


class TestVerifierDispatchAuthority:
    """verifier 派发（runner._dispatch_verifier）视角的授权一致性。"""

    def test_verifier_blocked_records_hand_to_user(self, tmp_path: Path) -> None:
        """合同只给 exec-a 授权 executor 角色 → verifier 无候选 → 如实记事件。

        与 dispatch 侧 blocked(no-executor) 语义一致：不静默、不降级。
        """
        from datetime import UTC, datetime

        from longtask.adapters.fake_executor import FakeExecutor
        from longtask.cli.runner import AttemptRunner
        from longtask.contracts.authority import Authority, AuthorityBinding
        from longtask.contracts.schema import Acceptance, Budget, ContractState
        from longtask.persistence.events import EventType
        from longtask.persistence.store import (
            StoreConfig,
            connect,
            ensure_schema,
            get_events,
            save_contract,
            update_contract_state,
        )

        now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            ensure_schema(conn)
            draft = ContractDraft(
                title="verifier 授权测试",
                objective="验证 verifier 派发也受授权约束",
                deadline_at=now.replace(hour=20),
                hard_constraints={},
                acceptance=Acceptance(standard="测试", checks=("通过",)),
                workload_initial_hours=2.0,
                budget=Budget(
                    max_dispatches=5,
                    max_escalations=2,
                    max_concurrent_attempts=1,
                    max_attempt_minutes=60,
                    max_output_bytes=1048576,
                ),
                authority=Authority(
                    executor_policy="explicit_allow",
                    executors=(
                        # 只有 exec-a，且只有 executor 角色——没有谁能当 verifier
                        AuthorityBinding(executor_id="exec-a", models=("m",), roles=("executor",)),
                    ),
                ),
            )
            save_contract(conn, draft, contract_id="lt-auth-v1", now=now)
            update_contract_state(
                conn, contract_id="lt-auth-v1", new_state=ContractState.ACTIVE, now=now
            )

            registry = ExecutorRegistry()
            registry.register(make_entry("exec-a", enabled=True))
            registry.register(make_entry("exec-b", enabled=True))
            runner = AttemptRunner(tmp_path, conn, registry)
            runner._adapters["exec-a"] = FakeExecutor()
            runner._adapters["exec-b"] = FakeExecutor()

            # exec-b 全局 enabled 且能力匹配，但合同没授权任何 verifier 角色
            dispatched = runner._dispatch_verifier(
                now, contract_id="lt-auth-v1", executor_id="exec-a"
            )
            assert dispatched is False
            types = [str(e.event_type) for e in get_events(conn, contract_id="lt-auth-v1")]
            assert EventType.ESCALATION_HANDED_TO_USER.value in types
        finally:
            conn.close()
