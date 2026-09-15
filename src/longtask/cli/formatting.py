"""CLI 共享格式化（DESIGN §10 §11.6）：合同列表详细输出 + u/ETA 派生。

只读客户端辅助：list --verbose 在不污染协议输出的前提下展示
紧迫度与 ETA——按 DESIGN §6.1 紧迫度公式（workload_initial_hours /
hours_left）即时计算。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from longtask.contracts.schema import ContractState, ContractView
from longtask.promoter.urgency import classify, urgency


def compute_u(view: ContractView, now: datetime) -> float | None:
    """派生紧迫度：workload / hours_left（DESIGN §6.1）。

    越 deadline 返回 None（§6.2：转仲裁，不走阶梯）。已终止状态（complete /
    cancelled / expired）也返回 None（不再调度）。
    """
    if view.state in (ContractState.COMPLETE, ContractState.CANCELLED, ContractState.EXPIRED):
        return None
    hours_left = (view.draft.deadline_at - now).total_seconds() / 3600.0
    if hours_left <= 0:
        return None
    return urgency(remaining_hours=view.draft.workload_initial_hours, hours_left=hours_left)


def format_eta(view: ContractView, now: datetime) -> str:
    """ETA 字符串：未来 → "in 2h 13m"；越 deadline → "past due 4m"；已 terminate → "—"。"""
    if view.state in (ContractState.COMPLETE, ContractState.CANCELLED, ContractState.EXPIRED):
        return "—"
    delta = view.draft.deadline_at - now
    if delta.total_seconds() <= 0:
        past = -int(delta.total_seconds())
        return f"past due {past // 60}m{past % 60}s" if past < 3600 else f"past due {past // 3600}h"
    s = int(delta.total_seconds())
    if s < 3600:
        return f"in {s // 60}m{s % 60}s"
    if s < 86400:
        return f"in {s // 3600}h{(s % 3600) // 60}m"
    return f"in {s // 86400}d{(s % 86400) // 3600}h"


_TIER_LABEL = {
    "QUEUED": "QUEUED ",
    "REMIND": "REMIND ",
    "STEER": "STEER  ",
    "RESPAWN": "RESPAWN",
    "PARALLEL": "PARALLE",
    "HAND_TO_USER": "HAND-US",
}


def render_contract_list_verbose(
    contracts: list[Any], *, min_u: float | None, now: datetime
) -> dict[str, Any]:
    """list --verbose 输出：单合同含 u、tier、eta、blocked_reason 字段。

    min_u 过滤掉 u < min_u 的合同（None 视作通过——含已 terminate 的）。
    返回 dict 与 CLI JSON 输出格式对齐（result 字段）。
    """
    items: list[dict[str, Any]] = []
    for view in contracts:
        # ContractView 与 SimpleNamespace 都允许属性访问——按 duck-typing
        contract_id = getattr(view, "contract_id", None)
        title = getattr(getattr(view, "draft", None), "title", None)
        state = getattr(view, "state", None)
        deadline_at = getattr(getattr(view, "draft", None), "deadline_at", None)
        workload = getattr(getattr(view, "draft", None), "workload_initial_hours", None)
        blocked_reason = getattr(view, "blocked_reason", None)
        blocked_reason_str = blocked_reason.value if blocked_reason else None

        u = compute_u(view, now)
        if min_u is not None and u is not None and u < min_u:
            continue
        tier = classify(u) if u is not None else None
        items.append(
            {
                "contract_id": contract_id,
                "title": title,
                "state": str(state.value)
                if state is not None and hasattr(state, "value")
                else str(state),
                "u": round(u, 3) if u is not None else None,
                "tier": _TIER_LABEL[tier.name] if tier else None,
                "eta": format_eta(view, now),
                "workload_hours": workload,
                "deadline_at": deadline_at.isoformat() if deadline_at else None,
                "blocked_reason": blocked_reason_str,
            }
        )
    return {"ok": True, "result": items, "min_u": min_u}


def now_utc() -> datetime:
    """共享 helper：mcp_server / cli 都要的 UTC 当前时刻。"""
    return datetime.now(UTC)


__all__ = [
    "compute_u",
    "format_eta",
    "now_utc",
    "render_contract_list_verbose",
]
