"""Read-side aggregations for humans and models: brief, board, stats.

All three are pure reads over the authoritative store; none of them mutate
anything.  They exist so an agent taking over a session (or a human checking
in) can answer "what is the state and what did other agents do" with one
call instead of stitching events by hand.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from lhgp.contracts.state_machine import TERMINAL_STATES
from lhgp.persistence.events_query import get_latest_forecast_snapshot
from lhgp.persistence.store import get_contract


def _risk_of(conn: sqlite3.Connection, contract_id: str) -> dict[str, Any]:
    snap = get_latest_forecast_snapshot(conn, contract_id=contract_id)
    if not snap:
        return {"risk": "unknown", "confidence": "none"}
    payload = snap
    return {
        "risk": payload.get("risk", "unknown"),
        "confidence": payload.get("confidence", "unknown"),
        "slack_p50_minutes": payload.get("slack_p50_minutes"),
        "next_decision_at": payload.get("next_decision_at"),
    }


def build_board(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    include_terminal: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """One row per contract: identity, state, risk, budget, next decision.

    Rows are ordered by risk severity then deadline so the top of the board
    is where attention should go first.
    """
    rows = conn.execute(
        "SELECT contract_id, title, state, deadline_status, blocked_reason,"
        " deadline_at, next_decision_at, budget_json"
        " FROM contracts ORDER BY contract_id LIMIT ?",
        (limit,),
    ).fetchall()
    board: list[dict[str, Any]] = []
    for row in rows:
        state = row[2]
        if not include_terminal and state in {s.value for s in TERMINAL_STATES}:
            continue
        budget: dict[str, Any] = {}
        try:
            import json

            budget = json.loads(row[7] or "{}")
        except (ValueError, TypeError):
            budget = {}
        risk = _risk_of(conn, row[0])
        board.append(
            {
                "contract_id": row[0],
                "title": row[1],
                "state": state,
                "deadline_status": row[3],
                "blocked_reason": row[4],
                "deadline_at": row[5],
                "next_decision_at": row[6],
                "risk": risk.get("risk"),
                "confidence": risk.get("confidence"),
                "max_dispatches": budget.get("max_dispatches"),
            }
        )
    order = {"red": 0, "orange": 1, "yellow": 2, "green": 3, "unknown": 4}
    board.sort(key=lambda item: (order.get(str(item.get("risk")), 9), str(item["deadline_at"])))
    return board


def build_brief(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Handover brief for one contract: what a fresh agent needs to continue.

    Sections: identity/state, deadline risk, budget posture, latest attempt
    outcome, latest verifier verdict, blocked notifications, and pointers to
    the on-disk handover/projection files.
    """
    contract = get_contract(conn, contract_id)
    if contract is None:
        return {"found": False, "contract_id": contract_id}
    risk = _risk_of(conn, contract_id)
    attempts = conn.execute(
        "SELECT attempt_id, role, executor_id, model_id, state, error_class,"
        " admitted_at, terminal_at, return_code"
        " FROM attempts WHERE contract_id = ? ORDER BY admitted_at DESC LIMIT 5",
        (contract_id,),
    ).fetchall()
    latest_attempt = [
        {
            "attempt_id": a[0],
            "role": a[1],
            "executor_id": a[2],
            "model_id": a[3],
            "state": a[4],
            "error_class": a[5],
            "return_code": a[8],
        }
        for a in attempts[:1]
    ]
    verifier_rows = conn.execute(
        "SELECT event_type, payload_json, created_at FROM events"
        " WHERE contract_id = ? AND role = 'verifier'"
        " AND event_type IN ('attempt/succeeded', 'attempt/failed')"
        " ORDER BY event_id DESC LIMIT 1",
        (contract_id,),
    ).fetchone()
    latest_verifier: dict[str, Any] | None = None
    if verifier_rows is not None:
        import json

        try:
            payload = json.loads(verifier_rows[1] or "{}")
        except ValueError:
            payload = {}
        latest_verifier = {
            "event_type": verifier_rows[0],
            "at": verifier_rows[2],
            "reason": payload.get("reason"),
        }
    notifications = conn.execute(
        "SELECT payload_json, created_at FROM notification_outbox"
        " ORDER BY notification_id DESC LIMIT 3"
    ).fetchall()
    return {
        "found": True,
        "contract_id": contract_id,
        "goal_id": contract.goal_id,
        "state": contract.state.value,
        "revision": contract.revision,
        "blocked_reason": (contract.blocked_reason.value if contract.blocked_reason else None),
        "deadline_at": contract.draft.deadline_at.isoformat(),
        "deadline_status": contract.deadline_status.value,
        "risk": risk,
        "budget": {
            "max_dispatches": contract.draft.budget.max_dispatches,
            "verification_reserved": (contract.draft.budget.verification_attempts_reserved),
        },
        "recent_attempts": [
            {
                "attempt_id": a[0],
                "role": a[1],
                "executor_id": a[2],
                "state": a[4],
                "error_class": a[5],
            }
            for a in attempts
        ],
        "latest_attempt": latest_attempt[0] if latest_attempt else None,
        "latest_verifier": latest_verifier,
        "recent_notifications": [
            {
                "at": n[1],
            }
            for n in notifications
        ],
        "handover_hint": f"contracts/{contract_id}/handover.md",
        "generated_at": now.isoformat(),
    }


def build_stats(
    conn: sqlite3.Connection,
    *,
    contract_id: str | None = None,
) -> dict[str, Any]:
    """Cost accounting: dispatch/verifier wall time and outcome distribution.

    Reads the attempts table only (no event scan).  Wall time uses
    admitted_at -> terminal_at; attempts without a terminal state are
    counted as open.
    """
    # contract_id 来自受控调用方（CLI/MCP 校验后），非用户自由文本；
    # 两条常量语句避免动态 SQL 拼接。
    if contract_id:
        rows = conn.execute(
            "SELECT role, executor_id, model_id, state, admitted_at, terminal_at,"
            " return_code, usage_json FROM attempts WHERE contract_id = ?",
            (contract_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, executor_id, model_id, state, admitted_at, terminal_at,"
            " return_code, usage_json FROM attempts"
        ).fetchall()
    by_role: dict[str, int] = {}
    by_executor: dict[str, int] = {}
    by_state: dict[str, int] = {}
    wall_seconds: list[float] = []
    return_codes: dict[str, int] = {}
    usage_totals: dict[str, float] = {}
    for (
        role,
        executor_id,
        model_id,
        state,
        admitted_at,
        terminal_at,
        return_code,
        usage_json,
    ) in rows:
        by_role[role] = by_role.get(role, 0) + 1
        exec_key = executor_id or (model_id or "unknown")
        by_executor[exec_key] = by_executor.get(exec_key, 0) + 1
        by_state[state] = by_state.get(state, 0) + 1
        if admitted_at and terminal_at:
            try:
                from datetime import datetime as _dt

                start = _dt.fromisoformat(admitted_at)
                end = _dt.fromisoformat(terminal_at)
                wall_seconds.append(max(0.0, (end - start).total_seconds()))
            except ValueError:
                pass
        if return_code is not None:
            key = "zero" if return_code == 0 else "nonzero"
            return_codes[key] = return_codes.get(key, 0) + 1
        if usage_json:
            _accumulate_usage(usage_totals, usage_json)
    wall_seconds.sort()
    stats: dict[str, Any] = {
        "scope": contract_id or "all",
        "total_attempts": len(rows),
        "by_role": by_role,
        "by_executor": by_executor,
        "by_state": by_state,
        "return_codes": return_codes,
    }
    if wall_seconds:
        # 下中位：小样本确定性优先（p50 语义由调用方解释）
        stats["wall_seconds_p50"] = wall_seconds[(len(wall_seconds) - 1) // 2]
        stats["wall_seconds_max"] = wall_seconds[-1]
    if usage_totals:
        # 消耗台账（§11.3）：执行者自报值的合计。自报是**下界**（压缩/缓存
        # 刷新可能使真实消耗更高），此处只做透明合计，不做任何换算或外推。
        stats["usage_totals"] = usage_totals
    return stats


def _accumulate_usage(totals: dict[str, float], usage_json: str) -> None:
    """把一行 usage_json 累进台账；形状不合规的存量数据跳过（不抛、不猜）。"""
    import json

    try:
        parsed = json.loads(usage_json)
    except ValueError:
        return
    if not isinstance(parsed, dict):
        return
    for key, value in parsed.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            continue
        totals[key] = totals.get(key, 0) + float(value)


def contract_is_terminal(conn: sqlite3.Connection, contract_id: str) -> bool:
    contract = get_contract(conn, contract_id)
    return contract is not None and contract.state in TERMINAL_STATES


__all__ = [
    "build_board",
    "build_brief",
    "build_stats",
    "contract_is_terminal",
]
