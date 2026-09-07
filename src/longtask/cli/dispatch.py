"""逐候选执行器分发（DESIGN §8.3、§9、§10）。

prepare 探针先于租约 CAS（§10 时序：prepare → 租约 CAS → spawn）；
拒接记录 dispatch/refused 事件并换下一个（§9，绝不降级）。
依赖执行桥接层（cli/runner.py）构造 AttemptInput，故属 cli 层。

Plan gate（resilient-execution plan_mode）：当 contract.draft.context
显式声明 ``"gate": "plan"`` 时，dispatch 必须等到收到一条
PLAN_APPROVED 事件（且该事件晚于最新一次 PLAN_REJECTED）才放行
DISPATCHING -> RUNNING。这条强制让任何绕过 ``plan submit`` /
``tool_submit_plan`` 的派发路径在 runner 边界被拦住，避免 gate
被旁路。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from longtask.adapters.base import ExecutorAdapter, PrepareRefusedError
from longtask.adapters.registry import RegistryEntry
from longtask.cli.runner import build_attempt_input
from longtask.contracts.schema import ContractView
from longtask.persistence.events import EventType
from longtask.persistence.projections import rebuild_projection
from longtask.persistence.store import acquire_lease, append_event, get_lease, reclaim_lease
from longtask.promoter.records import _record_attempt
from longtask.promoter.urgency import UrgencyTier

# Plan gate: how far back to look for a PLAN_APPROVED that hasn't been
# superseded by a PLAN_REJECTED. 24h is generous for a planning cycle but
# short enough that a stale approval doesn't pin a contract forever.
PLAN_GATE_LOOKBACK_SECONDS = 24 * 60 * 60


def _has_recent_plan_approval(
    conn: sqlite3.Connection,
    contract_id: str,
    now: datetime,
) -> bool:
    """Return True iff the contract has a PLAN_APPROVED event in the
    last :data:`PLAN_GATE_LOOKBACK_SECONDS` that is not superseded by a
    later PLAN_REJECTED.

    The check is pure SQL: we look up the most recent plan verdict
    (approved or rejected) in the lookback window and require it to be
    ``plan/approved``. If there is no plan verdict at all in the
    window, the gate is not satisfied — the writer side (``plan
    submit`` / ``tool_submit_plan``) must have been called first.
    """
    cutoff_iso = (now - timedelta(seconds=PLAN_GATE_LOOKBACK_SECONDS)).isoformat()
    row = conn.execute(
        "SELECT event_type FROM events "
        "WHERE contract_id = ? "
        "AND event_type IN (?, ?) "
        "AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (
            contract_id,
            EventType.PLAN_APPROVED,
            EventType.PLAN_REJECTED,
            cutoff_iso,
        ),
    ).fetchone()
    if row is None:
        return False
    return str(row[0]) == EventType.PLAN_APPROVED


def _plan_gate_required(contract: ContractView) -> bool:
    """Opt-in flag: ``context.gate == "plan"`` enables the gate.

    Default off so existing contracts without a plan keep working. A
    contract author who wants the gate enabled sets
    ``context = {"gate": "plan", ...}`` in the draft.
    """
    ctx = contract.draft.context
    return isinstance(ctx, dict) and ctx.get("gate") == "plan"


def _dispatch_attempt(
    *,
    root: Path,
    conn: sqlite3.Connection,
    contract: ContractView,
    candidates: list[RegistryEntry],
    now: datetime,
    tier: UrgencyTier,
    attempt_seq: str,
    adapter_factory: Callable[[RegistryEntry], ExecutorAdapter | None],
    emit: Callable[[str], None],
) -> dict[str, str] | None:
    """逐候选分发（DESIGN §8.3、§9）：prepare 兑现才占租约，拒接记录事件换下一个。

    prepare 探针在租约获取之前（DESIGN §10 时序：prepare → 租约 CAS → spawn）；
    成功返回 {"contract_id", "attempt_id", "executor_id"} 供执行桥接层拉起，
    全部候选耗尽（含拒接）返回 None，由调用方转 blocked(no-executor)。
    存在心跳已断的旧租约时走回收路径（lease/reclaimed，DESIGN §7）。
    """
    cid = contract.contract_id
    draft = contract.draft

    # Plan gate: when the contract opts in (context.gate == "plan"), the
    # dispatch must observe a recent PLAN_APPROVED event before any
    # executor is contacted. Without this, the gate could be bypassed
    # by any path that doesn't go through ``plan submit`` /
    # ``tool_submit_plan``.
    if _plan_gate_required(contract) and not _has_recent_plan_approval(conn, cid, now):
        append_event(
            conn,
            contract_id=cid,
            event_type=EventType.DISPATCH_REFUSED,
            payload={
                "reason": "plan gate: no recent PLAN_APPROVED event",
                "gate": "plan",
            },
            now=now,
            actor="daemon",
            goal_id=contract.goal_id,
            contract_revision=contract.revision,
            role="promoter",
        )
        rebuild_projection(root, cid, conn)
        emit(f"promoter/dispatch-refused:{cid}:plan-gate")
        return None

    attempt_prefix = f"att-{now.strftime('%Y%m%d%H%M%S')}-{attempt_seq}"
    attempt_id = attempt_prefix
    sequence = 1
    while conn.execute(
        "SELECT 1 FROM attempts WHERE attempt_id = ? LIMIT 1", (attempt_id,)
    ).fetchone():
        attempt_id = f"{attempt_prefix}-{sequence}"
        sequence += 1
    active_lease = get_lease(conn, cid)
    expected_gen = active_lease.generation if active_lease else 0
    probe_input = build_attempt_input(
        root, conn, contract, attempt_id, now, with_context=False
    )  # 探针不物化快照：租约未占，§10 时序

    def _record_refusal(executor_id: str, reason: str) -> None:
        append_event(
            conn,
            contract_id=cid,
            event_type=EventType.DISPATCH_REFUSED,
            payload={"executor_id": executor_id, "reason": reason},
            now=now,
            actor="daemon",
            goal_id=contract.goal_id,
            contract_revision=contract.revision,
            role="promoter",
        )
        rebuild_projection(root, cid, conn)
        emit(f"promoter/dispatch-refused:{cid}:{executor_id}")

    for entry in candidates:
        adapter = adapter_factory(entry)
        if adapter is None:
            _record_refusal(entry.id, f"注册表 kind={entry.kind!r} 没有可构造的适配器")
            continue
        try:
            adapter.prepare(probe_input)
        except PrepareRefusedError as exc:
            _record_refusal(entry.id, str(exc))
            continue
        selected_model = next((model for model in entry.models if model != "*"), "*")
        lease_payload = {
            "executor_id": entry.id,
            "model": selected_model,
            "urgency_tier": int(tier),
        }
        if active_lease is not None:
            # 心跳已断的旧租约：先回收再接管（lease/reclaimed，DESIGN §7）
            reclaim_lease(
                conn,
                contract_id=cid,
                expected_generation=expected_gen,
                heartbeat_at=now,
                timeout=timedelta(minutes=draft.budget.max_attempt_minutes),
                new_holder_attempt_id=attempt_id,
                actor="daemon",
                reason="heartbeat timeout before redispatch",
                payload=lease_payload,
                role="promoter",
                contract_revision=contract.revision,
            )
        else:
            acquire_lease(
                conn,
                contract_id=cid,
                holder_attempt_id=attempt_id,
                expected_generation=expected_gen,
                heartbeat_at=now,
                timeout=timedelta(minutes=draft.budget.max_attempt_minutes),
                actor="daemon",
                payload=lease_payload,
                role="promoter",
                contract_revision=contract.revision,
            )
        append_event(
            conn,
            contract_id=cid,
            attempt_id=attempt_id,
            event_type=EventType.ATTEMPT_STARTED,
            payload={
                "executor_id": entry.id,
                "model": selected_model,
                "tier": int(tier),
                "role": "executor",
                "contract_revision": contract.revision,
            },
            now=now,
            actor="daemon",
            goal_id=contract.goal_id,
            contract_revision=contract.revision,
            role="executor",
        )
        # P1：写入 attempts 实体行（DESIGN §7 attempt 轴、C1/C3 修复依据）
        _record_attempt(
            conn,
            goal_id=contract.goal_id,
            contract_id=contract.contract_id,
            attempt_id=attempt_id,
            contract_revision=contract.revision,
            role="executor",
            executor_id=entry.id,
            model_id=selected_model,
            state="admitted",
            admitted_at=now,
            updated_at=now,
        )
        rebuild_projection(root, cid, conn)
        emit(f"promoter/dispatched:{cid}:{entry.id}")
        return {
            "contract_id": cid,
            "attempt_id": attempt_id,
            "executor_id": entry.id,
            "model": selected_model,
        }
    return None
