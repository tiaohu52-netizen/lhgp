"""MCP 工具面 profile：按调用方角色收窄暴露的工具集（SPEC §19.3、DESIGN §11）。

51 个工具全量挂进宿主的 tools/list 是实测过的负担（约 34 KB / 粗估 8.5K token，
其中 37% 是兼容别名重复），而且执行者会话能看见 `approve` / `update_goal` 这类
Principal-only 入口（模型调用会被 AUTH_FAILED，但**能看见就会去试**，每次尝试都是
一次注定失败的往返和一段被浪费的上下文）。

profile 是**服务端声明**，不是提示词约定：

- ``tools/list`` 只返回 profile 内的工具；
- ``tools/call`` 对 profile 外的工具一律拒调（fail-closed）——隐藏必须是真的
  不可达，否则「看不见」只是装饰，模型仍可凭名字硬调；
- 默认 profile = ``legacy``（全量 51 条）：不配置的既有安装**一次也不变**，
  这是升级纪律（CONTRIBUTING：不破坏存量）。

选择方式（先环境变量后默认）::

    LHGP_MCP_PROFILE=executor|verifier|planner|operator|full|legacy

profile 内容是对**运行事实**的描述，不是建议：执行者拿不到批准权
（``approve``/``update_goal``/``user_confirm`` 全不在 executor profile 里），
核验者拿不到写回之外的任何变更面。
"""

from __future__ import annotations

from longtask.mcp_server import TOOLS

# 可用 profile 名单。新增 profile 必须同步 DESIGN §11 与 SKILL 的决策表。
PROFILE_NAMES = ("executor", "verifier", "planner", "operator", "full", "legacy")

DEFAULT_PROFILE = "legacy"

# 每个角色的工具清单只收**正名**：别名轨（longtask_*）是迁移兼容面，新配置
# 不应该再扩大它的暴露。正名清单里每个名字都必须真实存在（漂移由
# test_tool_profiles 钉住），不存在的名字在加载时即报错——宁可不启动，
# 不静默少暴露一个工具。
EXECUTOR_TOOLS = (
    "lhgp_health",
    "lhgp_doctor",
    "lhgp_get_contract",
    "lhgp_attempt_status",
    "lhgp_write_back",
    "lhgp_attach_executor",
    "lhgp_resume_attempt",
    "lhgp_send_message",
    "lhgp_inbox",
    "lhgp_notifications",
    "lhgp_interrupt_attempt",
)
VERIFIER_TOOLS = (
    "lhgp_health",
    "lhgp_get_contract",
    "lhgp_attempt_status",
    "lhgp_write_back",
    "lhgp_brief",
    "lhgp_send_message",
    "lhgp_inbox",
)
PLANNER_TOOLS = (
    "lhgp_health",
    "lhgp_list_goals",
    "lhgp_get_goal",
    "lhgp_next_goal_action",
    "lhgp_goal_contract_draft",
    "lhgp_prepare_goal",
    "lhgp_submit_plan",
    "lhgp_plan_signoff",
    "lhgp_propose_plan",
    "lhgp_request_verification",
    "lhgp_get_contract",
    "lhgp_list_contracts",
)
OPERATOR_TOOLS = (
    "lhgp_health",
    "lhgp_doctor",
    "lhgp_list_executors",
    "lhgp_list_contracts",
    "lhgp_get_contract",
    "lhgp_trace",
    "lhgp_portfolio",
    "lhgp_board",
    "lhgp_brief",
    "lhgp_stats",
    "lhgp_deadline_report",
    "lhgp_notifications",
    "lhgp_inbox",
    "lhgp_attempt_status",
    "lhgp_interrupt_attempt",
    "lhgp_request_verification",
    "lhgp_approve_goal",
    "lhgp_update_goal",
    "lhgp_advance_goal",
    "lhgp_user_confirm_spec_verdict",
    "lhgp_prepare_goal",
    "lhgp_list_goals",
    "lhgp_get_goal",
    "lhgp_next_goal_action",
    "lhgp_goal_contract_draft",
    "lhgp_submit_plan",
    "lhgp_plan_signoff",
    "lhgp_submit_evaluation",
    "lhgp_compute_diff",
    "lhgp_evolve_templates",
    "lhgp_send_message",
    "lhgp_propose_plan",
    "lhgp_stats",
    # 人不在执行者会话里干活：write_back / attach_executor / resume_attempt
    # 是执行者的写回通道，operator 面不需要（要干预走 CLI）。
    "lhgp_write_back",
    "lhgp_attach_executor",
    "lhgp_resume_attempt",
)

_ROLE_PROFILES: dict[str, tuple[str, ...]] = {
    "executor": EXECUTOR_TOOLS,
    "verifier": VERIFIER_TOOLS,
    "planner": PLANNER_TOOLS,
    "operator": OPERATOR_TOOLS,
    # full：全部正名、不含兼容别名。
    "full": tuple(sorted(name for name in TOOLS if name.startswith("lhgp_"))),
    # legacy：历史行为，全量（含 17 个 longtask_* 兼容别名）。
    "legacy": tuple(sorted(TOOLS)),
}


class ProfileError(Exception):
    """profile 名不合法或清单引用了不存在的工具——fail-closed。"""


def _validate(name: str, tools: tuple[str, ...]) -> None:
    if name not in ("full", "legacy"):
        missing = [t for t in tools if t not in TOOLS]
        if missing:
            raise ProfileError(
                f"profile {name!r} references unknown tools: {missing}; "
                "profiles must track the real registry (SPEC §19.3)"
            )


def resolve_profile(name: str | None) -> tuple[str, tuple[str, ...]]:
    """环境变量取值 → (profile 名, 工具名元组)。

    未知 profile 名直接抛 :class:`ProfileError`，绝不静默回落到默认——
    配置写错而系统照常起来，用户会以为什么都在正常运转。
    ``None``（未设置）走 DEFAULT_PROFILE。
    """
    if name is None or not name.strip():
        name = DEFAULT_PROFILE
    name = name.strip().lower()
    if name not in PROFILE_NAMES:
        raise ProfileError(f"unknown LHGP_MCP_PROFILE {name!r}; expected one of {PROFILE_NAMES}")
    tools = _ROLE_PROFILES[name]
    _validate(name, tools)
    return name, tools


def profile_for_environment(environ: dict[str, str]) -> tuple[str, tuple[str, ...]]:
    """从环境字典解析 profile（dict 注入便于测试，不读真环境）。"""
    return resolve_profile(environ.get("LHGP_MCP_PROFILE"))


__all__ = [
    "DEFAULT_PROFILE",
    "EXECUTOR_TOOLS",
    "OPERATOR_TOOLS",
    "PLANNER_TOOLS",
    "PROFILE_NAMES",
    "VERIFIER_TOOLS",
    "ProfileError",
    "profile_for_environment",
    "resolve_profile",
]
