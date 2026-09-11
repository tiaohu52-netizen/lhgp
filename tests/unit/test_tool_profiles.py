"""MCP 工具面 profile（`longtask.mcp_profiles` + `_dispatch` 接入）。

三层：
1. 声明一致性：每个 role profile 引用的工具都真实存在、只收正名；
2. 行为：tools/list 过滤 + tools/call 越 profile 拒调（fail-closed）；
3. 默认不变：不设置环境变量 = legacy 全量，既有安装零变化。
"""

from __future__ import annotations

import pytest

from longtask.mcp_profiles import (
    DEFAULT_PROFILE,
    EXECUTOR_TOOLS,
    OPERATOR_TOOLS,
    PLANNER_TOOLS,
    VERIFIER_TOOLS,
    ProfileError,
    profile_for_environment,
)
from longtask.mcp_server import TOOLS, _dispatch

pytestmark = pytest.mark.real_entry


def _ctx(profile_name: str, profile_tools: tuple[str, ...]) -> dict[str, object]:
    return {"profile_name": profile_name, "profile_tools": profile_tools}


class TestProfileDeclarations:
    def test_every_role_tool_exists_in_the_registry(self) -> None:
        for name in ("executor", "verifier", "planner", "operator"):
            _, tools = profile_for_environment({"LHGP_MCP_PROFILE": name})
            unknown = [t for t in tools if t not in TOOLS]
            assert unknown == [], f"profile {name} 引用了不存在的工具: {unknown}"

    def test_role_profiles_contain_no_legacy_aliases(self) -> None:
        """角色清单只收正名——别名轨是迁移兼容面，新配置不扩大它的暴露。"""
        for name in ("executor", "verifier", "planner", "operator", "full"):
            _, tools = profile_for_environment({"LHGP_MCP_PROFILE": name})
            aliases = [t for t in tools if t.startswith("longtask_")]
            assert aliases == [], f"profile {name} 含兼容别名: {aliases}"

    def test_executor_cannot_see_principal_gates(self) -> None:
        """执行者拿不到批准权：能看见就会去试，每次尝试都是注定失败的往返。"""
        _, tools = profile_for_environment({"LHGP_MCP_PROFILE": "executor"})
        for gate in ("lhgp_approve_goal", "lhgp_update_goal", "lhgp_user_confirm_spec_verdict"):
            assert gate not in tools

    def test_verifier_cannot_mutate_contracts(self) -> None:
        _, tools = profile_for_environment({"LHGP_MCP_PROFILE": "verifier"})
        for mutating in ("lhgp_approve_goal", "lhgp_update_goal", "lhgp_prepare_goal"):
            assert mutating not in tools

    def test_operator_can_reach_the_signature_gate(self) -> None:
        """签字门必须有正名可达——这是清掉 longtask_* 别名轨的前提。"""
        _, tools = profile_for_environment({"LHGP_MCP_PROFILE": "operator"})
        assert "lhgp_user_confirm_spec_verdict" in tools

    def test_full_is_all_canonical_only(self) -> None:
        _, tools = profile_for_environment({"LHGP_MCP_PROFILE": "full"})
        assert all(t.startswith("lhgp_") for t in tools)
        assert len(tools) == sum(1 for t in TOOLS if t.startswith("lhgp_"))

    def test_unknown_profile_is_rejected_not_silently_defaulted(self) -> None:
        with pytest.raises(ProfileError, match="unknown LHGP_MCP_PROFILE"):
            profile_for_environment({"LHGP_MCP_PROFILE": "admin"})


class TestDispatchBehaviour:
    def test_tools_list_is_filtered_to_the_profile(self) -> None:
        name, tools = profile_for_environment({"LHGP_MCP_PROFILE": "executor"})
        resp = _dispatch(_ctx(name, tools), "tools/list", {}, 1)
        listed = [t["name"] for t in resp["result"]["tools"]]
        assert sorted(listed) == sorted(tools)
        assert len(listed) == len(set(listed))  # 无重复条目

    def test_call_outside_profile_is_refused(self) -> None:
        """隐藏必须是真的不可达：看不见的工具按名字硬调也要拒。"""
        name, tools = profile_for_environment({"LHGP_MCP_PROFILE": "executor"})
        resp = _dispatch(
            _ctx(name, tools),
            "tools/call",
            {"name": "lhgp_approve_goal", "arguments": {"contract_id": "lt-x"}},
            2,
        )
        assert "error" in resp
        assert "outside this server's profile" in resp["error"]["message"]

    def test_unprofiled_context_refuses_to_serve(self) -> None:
        """绕过 serve_stdio 直呼 _dispatch：宁可报错不静默全量。"""
        with pytest.raises(ProfileError, match="profile not initialised"):
            _dispatch({"conn": None}, "tools/list", {}, 1)

    def test_initialize_advertises_the_profile(self) -> None:
        name, tools = profile_for_environment({"LHGP_MCP_PROFILE": "verifier"})
        resp = _dispatch(_ctx(name, tools), "initialize", {}, 1)
        assert resp["result"]["profile"] == "verifier"


class TestDefaultUnchanged:
    def test_unset_environment_yields_legacy_full_surface(self) -> None:
        """升级纪律：不配置的既有安装一次也不变（52 个工具全量）。"""
        name, tools = profile_for_environment({})
        assert name == DEFAULT_PROFILE
        assert sorted(tools) == sorted(TOOLS)

    def test_blank_environment_value_yields_legacy(self) -> None:
        name, _ = profile_for_environment({"LHGP_MCP_PROFILE": "  "})
        assert name == "legacy"


class TestProfileSizesStaySmall:
    """角色面必须显著小于全量——这是整个改动的意义，钉住防回弹。"""

    @pytest.mark.parametrize(
        ("profile", "tools"),
        [
            ("executor", EXECUTOR_TOOLS),
            ("verifier", VERIFIER_TOOLS),
            ("planner", PLANNER_TOOLS),
        ],
    )
    def test_role_profiles_are_a_fraction_of_the_full_surface(
        self, profile: str, tools: tuple[str, ...]
    ) -> None:
        """executor / verifier / planner 是模型侧角色：必须显著小于全量。

        operator 不在此列——它是人的管理面，本来就接近 full；它的价值在
        「不含兼容别名」，由 test_role_profiles_contain_no_legacy_aliases 钉。
        """
        assert len(tools) <= len(TOOLS) // 2 + 6, (
            f"profile {profile} 膨胀到 {len(tools)}/{len(TOOLS)}；"
            "角色面的意义就是显著小于全量，超过一半应重新划分角色"
        )

    def test_operator_profile_drops_only_legacy_aliases(self) -> None:
        """operator = full 减去别名轨；它必须仍能覆盖 full 的全部正名。

        比较用 ``sorted`` 而不是 ``set``：set 会把重复条目抹平，
        于是「清单里有重复」这个缺陷对断言完全不可见（``lhgp_stats``
        就曾在 OPERATOR_TOOLS 里出现两次而这条测试一直是绿的）。
        """
        assert sorted(OPERATOR_TOOLS) == sorted(t for t in TOOLS if t.startswith("lhgp_"))

    def test_no_profile_lists_a_tool_twice(self) -> None:
        """重复条目是纯负担：tools/list 会返回两条同名工具，收窄也白算一次。"""
        for name in ("executor", "verifier", "planner", "operator"):
            _, tools = profile_for_environment({"LHGP_MCP_PROFILE": name})
            dupes = sorted({t for t in tools if tools.count(t) > 1})
            assert dupes == [], f"profile {name} 重复列出: {dupes}"
