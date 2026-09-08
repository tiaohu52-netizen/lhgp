"""单次调度推进轮次（DESIGN §3.3、§6.2、§8.3、§11.3）。

run_daemon_tick 只做调度簿记：ticker 扫描、过期仲裁、紧迫度分档、
升级阶梯决策与逐候选分发串成一轮闭环；真实 attempt 的拉起与回收
在 cli/runner.py（执行桥接层），由 cli/daemon_loop.py 在每轮首尾驱动。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from lhgp.persistence.events_query import count_attempts_by_role, count_events
from lhgp.promoter.fairness import (
    ContractFairnessState,
    TickCapacityLedger,
    apply_fairness_order,
)
from longtask.acceptance.checks import RepairBrief
from longtask.adapters.base import ExecutorAdapter
from longtask.adapters.factory import build_adapter
from longtask.adapters.registry import ExecutorRegistry, RegistryEntry
from longtask.cli.dispatch import _dispatch_attempt
from longtask.contracts.schema import (
    AcceptanceStatus,
    BlockReason,
    ContractState,
    DeadlineStatus,
)
from longtask.contracts.state_machine import TERMINAL_STATES
from longtask.forecast.model import Forecast, build_deadline_snapshot
from longtask.persistence.attempts import count_running_by_executor
from longtask.persistence.decisions import set_next_decision_at
from longtask.persistence.events import EventType
from longtask.persistence.notifications import enqueue_notification
from longtask.persistence.projections import rebuild_projection
from longtask.persistence.store import (
    _notification_available_at,
    advance_goal,
    append_event,
    get_contract,
    get_events,
    get_goal,
    get_lease,
    list_contracts,
    update_contract_state,
)
from longtask.promoter.escalation import decide
from longtask.promoter.killswitch import is_kill_switch_active
from longtask.promoter.records import (
    _count_verifier_attempts,
    _estimate_stalled_from_attempts,
    _last_attempt_started_at,
    _last_event_at,
    _record_decision,
)
from longtask.promoter.urgency import UrgencyTier, classify, urgency
from longtask.scheduler.ticker import ClockEntry, ContractClock, is_overdue, run_tick


def run_daemon_tick(
    root: Path,
    conn: sqlite3.Connection,
    registry: ExecutorRegistry,
    now: datetime,
    emit_fn: Callable[[str], None] | None = None,
    adapter_factory: Callable[[RegistryEntry], ExecutorAdapter | None] | None = None,
) -> dict[str, Any]:
    """执行一次完整的调度推进轮次（DESIGN §3.3、§6.2、§8.3、§11.3）。

    处理流程：
    1. 检查全局 Kill Switch：若激活，跳过一切分发与加派；
    2. 查询库中所有非终态合同（DRAFTED, ACTIVE, PAUSED, BLOCKED, EXPIRED）；
    3. 运行 ticker 扫描判定 Deadline 越界与到点唤醒；
    4. 对过期合同执行仲裁迁移（-> EXPIRED）；
    5. 对 active 合同计算紧迫度并执行升级阶梯决策：
       - 若需要另起会话/加派，在已框定执行器池中按分发规则排序候选者；
       - 逐候选执行 prepare 探针（DESIGN §10 时序：prepare 先于租约）：
         拒接记录 dispatch/refused 事件并换下一个（DESIGN §9，绝不降级）；
       - 全部候选耗尽或无可匹配者，转入 blocked(no-executor)；
       - prepare 兑现者占领租约（旧租约心跳已断走回收路径，§7）并记录
         attempt/started 事件；
    6. 预算硬边界（§6.3）：已消耗 dispatch 数 = 事件流中 attempt/started
       计数，触顶即转档 5（blocked need-user），不再拉新会话；
    7. 自动同步更新物化文件投影（contract.yaml / lease.json / log.jsonl）。

    返回值带 attempts_started（contract_id/attempt_id/executor_id），供执行
    桥接层（AttemptRunner）真实拉起会话；本函数只做调度簿记（§3.3）。
    """
    events_emitted: list[str] = []

    def _emit(msg: str) -> None:
        events_emitted.append(msg)
        if emit_fn:
            emit_fn(msg)

    # 适配器工厂：未注入时用 kind 默认构造（DESIGN §12）
    factory = adapter_factory if adapter_factory is not None else build_adapter

    # 1. 检查 Kill Switch
    if is_kill_switch_active(root):
        _emit("daemon/kill-switch-active")
        return {
            "ok": True,
            "status": "halted_by_kill_switch",
            "events": events_emitted,
            "processed": 0,
            "attempts_started": [],
        }

    # Resolve verifier terminal evidence before computing dispatch decisions.
    # Otherwise a verifier that finished between ticks can leave the contract
    # looking active for one pass, causing an extra executor spawn before the
    # completion hook runs.
    _judge_verifier_outcomes(root, conn, now)

    # 2. 查询所有合同
    all_contracts = list_contracts(conn, limit=1000)
    clocks: list[ClockEntry] = []

    for c in all_contracts:
        if c.state in TERMINAL_STATES:
            continue
        deadline = c.draft.deadline_at
        wakeup = c.next_wakeup_at or (now + timedelta(seconds=60))
        clocks.append(
            ClockEntry(
                contract_id=c.contract_id,
                clock=ContractClock(
                    deadline_at=deadline,
                    next_wakeup_at=wakeup,
                    arbitrated_at=now if c.state == ContractState.EXPIRED else None,
                ),
            )
        )

    # 3. 运行 ticker 扫描
    updated_clocks = run_tick(now, clocks, emit=_emit)
    clock_map = {entry.contract_id: entry.clock for entry in updated_clocks}

    dispatched_count = 0
    expired_count = 0
    attempts_started: list[dict[str, str]] = []
    # E2 公平性：per-tick 容量记账 + 饥饿检测
    capacity_ledger = TickCapacityLedger()
    fairness_states: dict[str, ContractFairnessState] = {}
    dispatched_this_tick: set[str] = set()

    # P1：cross-tick 预算硬边界（C2 修复）——escalations_used 按 contract 累计。
    # 数据源：① 已派生的 verifier attempts（role=verifier 且 terminal）；② ESCALATION_STEERED 事件。
    # 二者均落库可审计；不再硬传 max_escalations 初值。
    escalations_used_by_contract: dict[str, int] = {}

    # P1：C3 修复：estimate_stalled 由 attempts 表真实判定——
    # "上一次 attempt 入库以来 u 值两次未下降"等价于"最近两次 attempt 同档/高档，
    # 且最近一次无 verifier 派生（verifier 派生代表已 §6.2 档 4 处理）"。
    # 这里给出最小近似：连续两次 attempt 同角色（executor）且未派生 verifier 即视为停滞。
    estimate_stalled_by_contract: dict[str, bool] = {}

    # E2 多合同公平调度：预扫 ACTIVE 合同的紧迫档位，主循环按档位降序
    # 处理（红/橙/黄/绿），同档位按 contract_id 稳定排序。此前按 ID 字典
    # 序遍历，低紧迫合同会先于高紧迫合同抢占执行器池。
    urgency_tier_by_contract: dict[str, int] = {}
    for c in all_contracts:
        if c.state != ContractState.ACTIVE:
            continue
        time_left = max(0.0, (c.draft.deadline_at - now).total_seconds() / 3600.0)
        remaining = _remaining_workload_hours(root, c)
        tier = classify(urgency(remaining, time_left))
        if tier is not None:
            urgency_tier_by_contract[c.contract_id] = int(tier)

    # 预扫一遍以建立两个映射
    for c in all_contracts:
        cid = c.contract_id
        if c.state in TERMINAL_STATES:
            continue
        verifier_count = _count_verifier_attempts(conn, cid)
        steer_count = count_events(
            conn, contract_id=cid, event_type=EventType.ESCALATION_STEERED.value
        )
        escalations_used_by_contract[cid] = verifier_count + steer_count
        estimate_stalled_by_contract[cid] = _estimate_stalled_from_attempts(conn, cid)

    # P1 review (2026-09-08, second round): auto-recover contracts
    # blocked purely on CAPACITY_FULL. These went BLOCKED because
    # every eligible executor was at its cap; as soon as any lease
    # is released, the next tick should retry them. Doing it here at
    # the start of every tick is O(blocked_set) and self-throttling
    # — if the cap is still full, the contract re-blocks immediately
    # and stops showing up here.
    woken_capacity: set[str] = set()
    for c in all_contracts:
        if c.state == ContractState.BLOCKED and c.blocked_reason == BlockReason.CAPACITY_FULL:
            from longtask.cli.dispatch import wake_blocked_capacity_full

            if wake_blocked_capacity_full(conn, c.contract_id, now):
                woken_capacity.add(c.contract_id)
    # Refresh the in-memory views for woken contracts so the main
    # loop sees them as ACTIVE this tick (the snapshot at the top
    # of run_daemon_tick is now stale for them).
    if woken_capacity:
        all_contracts = [get_contract(conn, c.contract_id) or c for c in all_contracts]

    ordered_contracts = sorted(
        (c for c in all_contracts if c.state != ContractState.ACTIVE),
        key=lambda c: c.contract_id,
    ) + sorted(
        (c for c in all_contracts if c.state == ContractState.ACTIVE),
        key=lambda c: (
            -urgency_tier_by_contract.get(c.contract_id, -1),
            c.contract_id,
        ),
    )

    # E2 公平性：饥饿合同提到最前（连续 >= starvation_ticks 个 tick 未派工）
    _fair_ids = apply_fairness_order(
        [c.contract_id for c in ordered_contracts],
        urgency_tier_by_contract,
        fairness_states,
    )
    _by_id = {c.contract_id: c for c in ordered_contracts}
    ordered_contracts = [_by_id[cid] for cid in _fair_ids if cid in _by_id]

    for c in ordered_contracts:
        cid = c.contract_id
        clock = clock_map.get(cid)
        if clock is None:
            continue

        # 4. 过期处理：未处于 EXPIRED 但 ticker 已标记过期
        if is_overdue(clock, now) and c.state != ContractState.EXPIRED:
            try:
                update_contract_state(
                    conn,
                    contract_id=cid,
                    new_state=ContractState.EXPIRED,
                    now=now,
                    deadline_status=DeadlineStatus.MISSED,
                    event_type=EventType.CONTRACT_EXPIRED,
                    event_payload={"arbitrated_at": now.isoformat()},
                    actor="daemon",
                )
                rebuild_projection(root, cid, conn)
                expired_count += 1
            except Exception as exc:
                _emit(f"error/expire-failed:{cid}:{exc}")
            continue

        # 5. 仅对 ACTIVE 状态合同进行推进
        if c.state != ContractState.ACTIVE:
            continue

        # 计算剩余时间与工作量
        time_left_hours = max(0.0, (c.draft.deadline_at - now).total_seconds() / 3600.0)
        remaining_hours = _remaining_workload_hours(root, c)
        u_val = urgency(remaining_hours, time_left_hours)
        u_tier = classify(u_val)

        # 风险红线通知：同一合同 revision 只入队一次，避免 daemon 每轮
        # 重复骚扰；合同修订后允许重新评估并再次通知。
        if (
            u_tier is not None
            and u_tier >= UrgencyTier.RESPAWN
            and "risk_red" in c.draft.attention.notify_on
        ):
            enqueue_notification(
                conn,
                idempotency_key=f"{cid}:risk-red:revision-{c.revision}",
                goal_id=c.goal_id,
                event_type="risk_red",
                channel="local",
                payload={
                    "contract_id": cid,
                    "revision": c.revision,
                    "urgency": u_val,
                    "remaining_hours": remaining_hours,
                    "time_left_hours": time_left_hours,
                },
                now=now,
                available_at=_notification_available_at(c.draft.attention, "risk_red", now),
            )

        active_lease = get_lease(conn, cid)
        lease_alive = active_lease.is_alive(now) if active_lease else False

        # 预算硬边界（DESIGN §6.3）：已消耗 dispatch = 事件流中 attempt/started 数
        # verifier 使用独立 verification_attempts_reserved 记账；不能把
        # 验收 attempt 计入 executor 的 max_dispatches，否则一次验收会
        # 吃掉修复机会（SPEC §12.4）。历史事件没有 role 时按 executor
        # 兼容，只有明确标记 verifier 的事件排除。
        started_events = count_attempts_by_role(
            conn,
            contract_id=cid,
            role="executor",
            states=("admitted", "running", "succeeded", "failed", "cancelled", "stale", "orphaned"),
        )
        budget_dispatches_left = max(0, c.draft.budget.max_dispatches - started_events)

        allow_parallel = (
            c.draft.execution.get("allow_parallel", False)
            if isinstance(c.draft.execution, dict)
            else False
        )
        decision = decide(
            u_tier,
            lease_alive=lease_alive,
            budget_dispatches_left=budget_dispatches_left,
            budget_escalations_left=max(
                0, c.draft.budget.max_escalations - escalations_used_by_contract.get(cid, 0)
            ),
            estimate_stalled=estimate_stalled_by_contract.get(cid, False),
            partitions_allowed=allow_parallel,
        )

        # P4：真实决策点计算 + 落库（不递增 revision，纯调度簿记）
        next_at = _compute_next_decision_at(
            c, now=now, lease=active_lease, decision_tier=decision.tier
        )
        # Do not postpone an already scheduled decision on every daemon tick.
        # Keep the persisted future point stable, while still allowing a new
        # risk signal (lease loss, urgency, or deadline cap) to move it earlier.
        persisted_next_at = c.next_decision_at
        if persisted_next_at is not None and persisted_next_at > now:
            next_at = persisted_next_at if next_at is None else min(persisted_next_at, next_at)
        if next_at is not None:
            set_next_decision_at(
                conn,
                contract_id=cid,
                when=next_at,
                now=now,
                reason=_next_decision_reason(decision.tier, lease_alive, budget_dispatches_left),
                goal_id=c.goal_id,
                contract_revision=c.revision,
            )

        # Deadline Decision Reliability v1：把本轮风险判断固化成不可变
        # snapshot。当前历史样本尚未接入校准器，因此明确标记 low/coarse，
        # 但仍给出保守 p50/p90、slack 和下一决策点，供 UI/恢复流程使用。
        remaining_minutes = remaining_hours * 60.0
        successful_minutes = _completed_attempt_durations(conn, c.goal_id, successful_only=True)
        # E1 校准：合同内主导执行器（成功样本 ≥2）的个人节奏优先于 goal
        # 池化——不同执行器速度不同，谁跑下一棒就用谁的先验；样本不足
        # 自动回退池化，再回退初始估计（低样本降级链不变）。
        grouped = _successful_durations_by_executor(conn, cid)
        if grouped:
            _dominant, dominant_samples = max(grouped.items(), key=lambda kv: len(kv[1]))
            if len(dominant_samples) >= 2:
                successful_minutes = dominant_samples
        if successful_minutes:
            ordered = sorted(successful_minutes)
            # nearest-rank：小样本时 p90 必须保守地落到更慢的样本，不能
            # 用 floor(n*0.9)-1 把 3 个样本的 p90 错取成中位数。
            p50_index = max(0, math.ceil(len(ordered) * 0.5) - 1)
            p90_index = max(0, math.ceil(len(ordered) * 0.9) - 1)
            forecast_p50 = ordered[p50_index] + 10.0
            forecast_p90 = ordered[p90_index] + 15.0
        else:
            forecast_p50 = remaining_minutes + 10.0
            forecast_p90 = forecast_p50 * 1.5
        forecast = Forecast(
            queue_minutes=0.0,
            startup_minutes=5.0,
            remaining_minutes=remaining_minutes,
            verification_minutes=5.0,
            retry_reserve_minutes=max(5.0, remaining_minutes * 0.15),
            safety_margin_minutes=1.0,
            forecast_p50_minutes=forecast_p50,
            forecast_p90_minutes=forecast_p90,
            p_finish=0.9 if forecast_p90 <= time_left_hours * 60.0 else 0.3,
        )
        snapshot = build_deadline_snapshot(
            forecast,
            computed_at=now,
            due_at=c.draft.deadline_at,
            next_decision_at=next_at,
            sample_count=len(successful_minutes),
            sample_durations_minutes=successful_minutes,
        )
        snapshot_payload = snapshot.to_dict()
        previous_snapshot: dict[str, Any] | None = None
        for event in reversed(get_events(conn, contract_id=cid)):
            if event.event_type == EventType.FORECAST_UPDATED:
                try:
                    value = json.loads(event.payload_json or "{}")
                except ValueError:
                    value = None
                if isinstance(value, dict):
                    previous_snapshot = value
                break
        # computed_at 只是观测时间，不应让同一份风险事实在每轮 tick
        # 刷屏；其余字段变化（尤其 risk/slack/next_decision_at）才是
        # 需要留下新证据的事实变化。
        previous_semantic = (
            _forecast_semantic_payload(previous_snapshot) if previous_snapshot is not None else None
        )
        current_semantic = _forecast_semantic_payload(snapshot_payload)
        if previous_semantic != current_semantic:
            append_event(
                conn,
                contract_id=cid,
                event_type=EventType.FORECAST_UPDATED,
                payload=snapshot_payload,
                now=now,
                actor="promoter",
                goal_id=c.goal_id,
                contract_revision=c.revision,
                role="promoter",
            )

        match decision.tier:
            case UrgencyTier.RESPAWN | UrgencyTier.PARALLEL:
                # workspace 排他（共同维护风险）：同 workspace 有其他合同的
                # 活租约 → 本轮延后。两个执行者并发写同一目录 = 未定义行为
                # （互相覆盖/读到半成品文件），绝不做静默并发写。
                holder = _workspace_holder_other_than(conn, c, now)
                if holder is not None:
                    append_event(
                        conn,
                        contract_id=cid,
                        event_type=EventType.DISPATCH_DEFERRED,
                        payload={
                            "reason": "workspace occupied by another live contract",
                            "workspace_root": holder["workspace_root"],
                            "holder_contract_id": holder["contract_id"],
                            "note": "serialised per workspace; retry next tick",
                        },
                        now=now,
                        actor="daemon",
                        goal_id=c.goal_id,
                        contract_revision=c.revision,
                        role="promoter",
                    )
                    _emit(f"promoter/deferred-workspace-busy:{cid}:held-by:{holder['contract_id']}")
                    continue
                # 挑选执行器（DESIGN §8.3），逐候选尝试（§9：拒接换下一个）
                # P1 review fix (2026-09-08): inject the per-executor running
                # count so the registry's max_concurrent_attempts gate is
                # actually enforced. Without this, every candidate reports
                # running=0 and a single executor with cap=1 can still be
                # handed two contracts in the same tick.
                running_attempts = count_running_by_executor(conn)
                # P1 review (2026-09-08, second round): when
                # match_candidates returns empty, distinguish "no
                # eligible candidate exists" (NO_EXECUTOR, terminal)
                # from "candidates exist but every one is cap-saturated"
                # (CAPACITY_FULL, recoverable). Calling match_candidates
                # a second time with an empty running_attempts map gives
                # the cap-free set cheaply — the registry's per-entry
                # eligibility checks (enabled, authority, capabilities)
                # are the same; only the cap gate differs.
                saturated_only = False
                candidates = registry.match_candidates(c.draft, running_attempts=running_attempts)
                if not candidates:
                    cap_free = registry.match_candidates(c.draft, running_attempts={})
                    if cap_free:
                        saturated_only = True
                started = _dispatch_attempt(
                    root=root,
                    conn=conn,
                    contract=c,
                    candidates=candidates,
                    now=now,
                    tier=decision.tier,
                    attempt_seq=cid[-4:],
                    adapter_factory=factory,
                    emit=_emit,
                )
                if started is not None:
                    dispatched_count += 1
                    attempts_started.append(started)
                    # E2 公平性记账
                    capacity_ledger.record_dispatch(cid)
                    dispatched_this_tick.add(cid)
                else:
                    # 区分两种 block:
                    # - NO_EXECUTOR: 没有合格候选（registry/authority/capability 都不通过）
                    # - CAPACITY_FULL: 合格候选存在但都 cap-saturated，可自愈
                    if saturated_only:
                        blocked_reason = BlockReason.CAPACITY_FULL
                        block_reason_text = (
                            "every eligible executor is at max_concurrent_attempts; "
                            "auto-retry when a lease is released"
                        )
                        block_emit = "promoter/blocked-capacity-full:{cid}"
                    else:
                        blocked_reason = BlockReason.NO_EXECUTOR
                        block_reason_text = "no dispatchable executor: none eligible or all refused"
                        block_emit = "promoter/blocked-no-executor:{cid}"
                    update_contract_state(
                        conn,
                        contract_id=cid,
                        new_state=ContractState.BLOCKED,
                        now=now,
                        blocked_reason=blocked_reason,
                        event_type=EventType.CONTRACT_BLOCKED,
                        event_payload={"reason": block_reason_text},
                        actor="daemon",
                    )
                    rebuild_projection(root, cid, conn)
                    _emit(block_emit.format(cid=cid))

            case UrgencyTier.HAND_TO_USER:
                update_contract_state(
                    conn,
                    contract_id=cid,
                    new_state=ContractState.BLOCKED,
                    now=now,
                    blocked_reason=BlockReason.NEED_USER,
                    event_type=EventType.CONTRACT_BLOCKED,
                    event_payload={"reason": decision.reason},
                    actor="daemon",
                )
                # 同步记一条 decisions（DESIGN §6 升级历史）
                _record_decision(
                    conn,
                    goal_id=c.goal_id,
                    contract_id=cid,
                    contract_revision=c.revision,
                    tier=u_tier,
                    decision_type="hand-to-user",
                    reason=decision.reason,
                    budget_dispatches_left=budget_dispatches_left,
                    budget_escalations_left=max(
                        0, c.draft.budget.max_escalations - escalations_used_by_contract.get(cid, 0)
                    ),
                    now=now,
                    actor="promoter",
                )
                rebuild_projection(root, cid, conn)
                _emit(f"promoter/blocked-need-user:{cid}")

            case UrgencyTier.REMIND:
                # P1：REMIND 冷却（DESIGN §10.5）——上次 REMIND 5 分钟内不重复。
                # 跨档判定：同一 tick 走到 STEER 分支则这里不重复落 REMIND 事件。
                last_remind = _last_event_at(conn, cid, EventType.ESCALATION_REMINDED)
                last_steer = _last_event_at(conn, cid, EventType.ESCALATION_STEERED)
                last_attempt = _last_attempt_started_at(conn, cid)
                cooldown_ok = last_remind is None or (now - last_remind) >= timedelta(minutes=5)
                cross_tier_ok = (
                    last_steer is None or last_attempt is None or last_steer < last_attempt
                )
                if cooldown_ok and cross_tier_ok:
                    append_event(
                        conn,
                        contract_id=cid,
                        event_type=EventType.ESCALATION_REMINDED,
                        payload={"reason": decision.reason},
                        now=now,
                        actor="daemon",
                        goal_id=c.goal_id,
                        contract_revision=c.revision,
                        role="promoter",
                    )
                    rebuild_projection(root, cid, conn)

            case UrgencyTier.STEER:
                # P1：STEER 跨档判定——同 tick 已记 REMIND 则跳过 STEER，避免重复事件。
                last_steer = _last_event_at(conn, cid, EventType.ESCALATION_STEERED)
                last_attempt = _last_attempt_started_at(conn, cid)
                if last_steer is None or last_attempt is None or last_steer < last_attempt:
                    append_event(
                        conn,
                        contract_id=cid,
                        event_type=EventType.ESCALATION_STEERED,
                        payload={"reason": decision.reason},
                        now=now,
                        actor="daemon",
                        goal_id=c.goal_id,
                        contract_revision=c.revision,
                        role="promoter",
                    )
                    rebuild_projection(root, cid, conn)

    return {
        "ok": True,
        "status": "completed",
        "events": events_emitted,
        "dispatched": dispatched_count,
        "expired": expired_count,
        "attempts_started": attempts_started,
    }


def _forecast_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return stable forecast facts for event deduplication.

    Slack is derived from ``now`` and drifts by fractions of a minute on
    every heartbeat even when the risk tier and forecast are unchanged.
    Round numeric observations to one decimal minute (six seconds) for
    comparison only; emitted snapshots retain full precision.
    """

    semantic: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "computed_at":
            continue
        semantic[key] = round(value, 1) if isinstance(value, float) else value
    return semantic


def _workspace_holder_other_than(
    conn: sqlite3.Connection,
    contract: Any,
    now: datetime,
) -> dict[str, str] | None:
    """workspace 排他判定（共同维护风险防护）。

    合同 workspace_root 被另一个**持有活租约**的合同占用时返回占用者信息：
    {contract_id, workspace_root}。自己的租约不冲突（同合同的重派有租约
    fencing 兜底）；未声明 workspace 或无人占用返回 None。

    活租约 = heartbeat_at + timeout 内（与 decide() 的 lease_alive 同口径）。
    死租约不算占用——心跳断了说明持有者已停止推进，回收路径会接管。
    """
    workspace = _contract_workspace(contract)
    if not workspace:
        return None
    normalized = _norm_workspace(workspace)
    for other in list_contracts(conn, limit=1000):
        if other.contract_id == contract.contract_id:
            continue
        if other.state not in (ContractState.ACTIVE, ContractState.BLOCKED):
            continue
        other_ws = _norm_workspace(_contract_workspace(other))
        if other_ws != normalized:
            continue
        lease = get_lease(conn, other.contract_id)
        if lease is not None and lease.is_alive(now):
            return {
                "contract_id": other.contract_id,
                "workspace_root": workspace,
            }
    return None


def _contract_workspace(contract: Any) -> str:
    """从 ContractView/ContractDraft 提取 workspace_root（未声明返回空串）。"""
    draft = getattr(contract, "draft", contract)
    hard = draft.hard_constraints or {}
    file_effects = hard.get("file_effects")
    if isinstance(file_effects, dict):
        root = file_effects.get("workspace_root")
        if isinstance(root, str) and root.strip():
            return root
    return ""


def _norm_workspace(workspace: str) -> str:
    """workspace 归一化比较键：真实物理路径 + 大小写折叠 + 正斜杠。

    安全审查 调度-C3：只小写盘符挡不住 Windows 大小写不敏感路径
    （D:/Data 与 d:/data 同一目录），junction/符号链接指向同一物理目录
    的两个 workspace 也能绕过排他。路径存在时解析 realpath（大小写按
    真实卷形态返回），整个键再 casefold；不存在时退回字符串归一化。
    """
    text = workspace.strip().replace("\\", "/").rstrip("/")
    try:
        resolved = os.path.realpath(text)
        text = resolved.replace("\\", "/").rstrip("/")
    except OSError:
        pass
    return text.casefold()


def _judge_verifier_outcomes(root: Path, conn: sqlite3.Connection, now: datetime) -> None:
    """verifier attempt 终态裁决（DESIGN §5.2）：成功 -> complete，失败 -> 退回 active。

    只看 verifier role 的终态事件（payload 含 role=verifier）：
    - attempt/succeeded 且未写过 contract/completed -> 合同转 complete
      （verifier 证据落 event payload）；
    - attempt/failed -> 合同退回 active（紧迫度重算），
      并清掉上次 verifier 派生的 isolation 状态。
    """
    for contract in list_contracts(conn, limit=1000):
        if contract.state not in (ContractState.ACTIVE,):
            continue
        last_verifier_state: str | None = None
        last_verifier_payload: dict[str, Any] = {}
        verifier_attempt_id: str | None = None
        for event in get_events(conn, contract_id=contract.contract_id):
            payload_text = event.payload_json or ""
            payload = _safe_json(payload_text)
            # 只接受当前修订、明确标注 verifier 的终态事件。旧版事件没有
            # role 字段时保留 actor=model/verifier 的兼容回退，但不再做
            # 任意字符串子串匹配，避免 executor 输出误触发完成。
            is_verifier = event.role == "verifier" or (
                event.role is None
                and payload.get("role") == "verifier"
                and event.actor in ("model", "verifier")
            )
            if not is_verifier:
                continue
            if event.contract_revision is not None and event.contract_revision != contract.revision:
                continue
            if str(event.event_type) == EventType.ATTEMPT_SUCCEEDED.value:
                last_verifier_state = "succeeded"
                last_verifier_payload = payload
                verifier_attempt_id = event.attempt_id
            elif str(event.event_type) == EventType.ATTEMPT_FAILED.value:
                last_verifier_state = "failed"
                last_verifier_payload = payload
                verifier_attempt_id = event.attempt_id

        if last_verifier_state is None or verifier_attempt_id is None:
            continue
        if last_verifier_state == "succeeded":
            if contract.acceptance_status == AcceptanceStatus.PASSED:
                continue
            append_event(
                conn,
                contract_id=contract.contract_id,
                event_type=EventType.CONTRACT_COMPLETED,
                payload={
                    "verifier": verifier_attempt_id,
                    "evidence": last_verifier_payload,
                },
                now=now,
                actor="verifier",
            )
            update_contract_state(
                conn,
                contract_id=contract.contract_id,
                new_state=ContractState.COMPLETE,
                now=now,
                acceptance_status=AcceptanceStatus.PASSED,
                deadline_status=(
                    DeadlineStatus.MET
                    if now <= contract.draft.deadline_at
                    else DeadlineStatus.MISSED
                ),
            )
            rebuild_projection(root, contract.contract_id, conn)
            _advance_goal_after_verified_contract(conn, contract, now)
        else:  # failed
            # P5 修复闭环（SPEC §12.4）：verifier 失败不退回裸 active，
            # 而是把失败原因结构化成 RepairBrief 写进 handover.md——
            # 下一轮 attempt 的 task_prompt/active.md 自动携带
            # 「哪些 check 没过 + 建议怎么修」，repair 才有上下文。
            brief = _repair_brief_from(verifier_attempt_id, last_verifier_payload)
            _write_repair_brief(root, contract, verifier_attempt_id, brief)
            append_event(
                conn,
                contract_id=contract.contract_id,
                event_type=EventType.CONTRACT_BLOCKED,
                payload={
                    "verifier": verifier_attempt_id,
                    "evidence": last_verifier_payload,
                    "reason": "verifier rejected (§5.2): repair brief written to handover",
                    "repair_brief": brief.to_dict(),
                },
                now=now,
                actor="verifier",
            )
            update_contract_state(
                conn,
                contract_id=contract.contract_id,
                new_state=ContractState.ACTIVE,
                now=now,
                acceptance_status=AcceptanceStatus.FAILED,
            )
            rebuild_projection(root, contract.contract_id, conn)


def _safe_json(text: str) -> dict[str, Any]:
    import json as _json

    try:
        result = _json.loads(text)
    except ValueError:
        return {}
    if isinstance(result, dict):
        return result
    return {}


def _successful_durations_by_executor(
    conn: sqlite3.Connection, contract_id: str
) -> dict[str, list[float]]:
    """成功 executor attempt 的实际时长按执行器分组（E1 校准采样）。

    校准的粒度是「谁来跑」：同一合同由不同执行器接力时，各自的历史节奏
    才是下次 forecast 该用的先验。样本不足的执行器自动回退池化/初始估计。
    """
    rows = conn.execute(
        "SELECT executor_id, started_at, terminal_at FROM attempts"
        " WHERE contract_id = ? AND role = 'executor' AND state = 'succeeded'"
        " AND started_at IS NOT NULL AND terminal_at IS NOT NULL",
        (contract_id,),
    ).fetchall()
    grouped: dict[str, list[float]] = {}
    for executor_id, started_at, terminal_at in rows:
        try:
            seconds = (
                datetime.fromisoformat(str(terminal_at)) - datetime.fromisoformat(str(started_at))
            ).total_seconds()
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            grouped.setdefault(executor_id or "unknown", []).append(seconds / 60.0)
    return grouped


def _completed_attempt_durations(
    conn: sqlite3.Connection,
    goal_id: str,
    *,
    successful_only: bool = False,
) -> list[float]:
    """Return ended executor durations, optionally restricted to successes."""
    state_values = (
        ("succeeded",)
        if successful_only
        else (
            "succeeded",
            "failed",
            "cancelled",
            "stale",
            "orphaned",
        )
    )
    if successful_only:
        query = (
            "SELECT started_at, terminal_at FROM attempts "
            "WHERE goal_id = ? AND role = 'executor' AND state IN (?) "
            "AND started_at IS NOT NULL AND terminal_at IS NOT NULL"
        )
    else:
        query = (
            "SELECT started_at, terminal_at FROM attempts "
            "WHERE goal_id = ? AND role = 'executor' "
            "AND state IN (?, ?, ?, ?, ?) "
            "AND started_at IS NOT NULL AND terminal_at IS NOT NULL"
        )
    rows = conn.execute(query, (goal_id, *state_values)).fetchall()
    durations: list[float] = []
    for started_at, terminal_at in rows:
        try:
            seconds = (
                datetime.fromisoformat(str(terminal_at)) - datetime.fromisoformat(str(started_at))
            ).total_seconds()
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            durations.append(seconds / 60.0)
    return durations


def _advance_goal_after_verified_contract(
    conn: sqlite3.Connection, contract: Any, now: datetime
) -> None:
    """Advance a stage only when its bound contract has verifier evidence."""
    goal = get_goal(conn, contract.goal_id)
    if goal is None or not isinstance(goal.get("plan"), dict):
        return
    stages = goal["plan"].get("stages")
    if not isinstance(stages, list):
        return
    bound = next(
        (
            stage
            for stage in stages
            if isinstance(stage, dict) and stage.get("contract_id") == contract.contract_id
        ),
        None,
    )
    if bound is None:
        return
    current = goal.get("progress", {}).get("current")
    stage_id = str(bound.get("id", ""))
    if current is not None and str(current) != stage_id:
        return
    try:
        advance_goal(
            conn,
            goal_id=contract.goal_id,
            complete_stage=stage_id,
            now=now,
            expected_revision=int(goal["revision"]),
            actor="verifier",
        )
    except Exception:
        # Contract completion is authoritative; Goal progress can be retried
        # safely on the next read/advance without hiding verifier evidence.
        return


def _remaining_workload_hours(root: Path, contract: Any) -> float:
    """读取最近一次可信 handover 估计，避免每轮重置为初始工作量。"""
    initial = float(contract.draft.workload_initial_hours)
    handover_path = root / "contracts" / contract.contract_id / "handover.md"
    if not handover_path.is_file():
        return initial
    try:
        from longtask.persistence.projections import parse_handover_markdown

        data, violations = parse_handover_markdown(handover_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return initial
    if data is None or violations or not data.source_attempt_id.strip():
        return initial
    return max(0.0, float(data.estimate_remaining_hours))


# ── P4：next_decision_at 计算（SPEC §9、§10）──
# 决策点是「下一次调度层必须重新审视这份合同的最早时刻」。纯函数，
# 时间/租约注入，无 IO——落库由 set_next_decision_at 负责。

# 各决策档的复核间隔：档位越高（越紧迫），复核越勤
_TIER_RECHECK_MINUTES: dict[UrgencyTier, float] = {
    UrgencyTier.QUEUED: 60.0,
    UrgencyTier.REMIND: 15.0,
    UrgencyTier.STEER: 10.0,
    UrgencyTier.RESPAWN: 5.0,
    UrgencyTier.PARALLEL: 5.0,
    UrgencyTier.HAND_TO_USER: 30.0,
}
# 租约健康时的复核间隔：持有者在推进，只须盯租约到期前的续约/回收窗口
_LEASE_RECHECK_MINUTES = 5.0
# 决策点不得晚于 deadline（过了 deadline 就没有决策可做了）
_DEADLINE_HARD_CAP = timedelta(seconds=1)


def _compute_next_decision_at(
    contract: Any,
    *,
    now: datetime,
    lease: Any,
    decision_tier: UrgencyTier | None,
) -> datetime | None:
    """计算该合同的下一个决策点（P4，SPEC §9）。

    取三个信号的最小值（最早需要回头看的时间）：
    1. 租约到期点：租约死了才谈接管/重派——到期前一刻必须回头看；
    2. 档位复核点：紧迫度档位决定复核节奏（QUEUED 1h / RESPAWN 5m）；
    3. deadline：越界仲裁是不可错过的事件，硬上限。

    租约活着时档位被 cap 在 REMIND，但租约到期点才是真正的决策点
    ——不是按档位傻等。deadline 永远封顶（决策点晚于 deadline 无意义）。
    """
    deadline = contract.draft.deadline_at
    candidates: list[datetime] = []

    if lease is not None:
        lease_expiry = lease.heartbeat_at + lease.timeout
        if lease_expiry > now:
            candidates.append(lease_expiry)
    if decision_tier is not None:
        recheck_minutes = _TIER_RECHECK_MINUTES.get(decision_tier, 15.0)
        candidates.append(now + timedelta(minutes=recheck_minutes))
    if deadline > now:
        # 当距离截止不足安全边际时，立即唤醒；不能把过去时刻写入调度簿。
        candidates.append(max(now, deadline - _DEADLINE_HARD_CAP))

    if not candidates:
        return None
    return min(candidates)


def _next_decision_reason(
    decision_tier: UrgencyTier | None,
    lease_alive: bool,
    budget_dispatches_left: int,
) -> str:
    """决策点归因（落进事件 payload，审计可读）。"""
    if budget_dispatches_left < 1:
        return "dispatch budget exhausted: only user action can move this contract"
    if lease_alive:
        return "lease healthy: re-check at lease expiry or tier recheck, whichever first"
    if decision_tier is None:
        return "past deadline: arbitration owns this contract"
    return f"u-tier {int(decision_tier)}: re-check at tier cadence"


# ── P5：verifier 失败 → RepairBrief → handover（SPEC §12.4 修复闭环）──


def _repair_brief_from(
    verifier_attempt_id: str,
    evidence: dict[str, Any],
) -> RepairBrief:
    """从 verifier 失败证据提炼 RepairBrief（§12.4）。

    evidence 的形态由 verifier 写回决定（失败 check 列表 / 失败原因 /
    stdout 尾部）；此处做忠实提炼，不发明没写过的失败项。
    """
    failed: list[str] = []
    raw_failed = evidence.get("failed_checks")
    if isinstance(raw_failed, list):
        failed = [str(c) for c in raw_failed]
    reasons = evidence.get("fail_reasons")
    if not failed and isinstance(reasons, list):
        failed = [str(r) for r in reasons]
    notes: list[str] = []
    if evidence.get("reason"):
        notes.append(str(evidence["reason"]))
    stderr = evidence.get("stderr")
    if stderr:
        notes.append(str(stderr))
    return RepairBrief(
        failed_checks=tuple(failed),
        context_pointer=str(evidence.get("context_pointer") or ""),
        retry_strategy="respawn",
        notes=tuple(notes[:3]),  # 提示性尾部，不淹没交接
    )


def _write_repair_brief(
    root: Path,
    contract: Any,
    verifier_attempt_id: str,
    brief: RepairBrief,
) -> None:
    """把 RepairBrief 融进 handover.md（§12.4 修复上下文传递）。

    handover 的 remaining/next_action 换成「修什么/怎么修」——下轮
    attempt 的 task_prompt 附言与 active.md 快照自动携带（§4.1 通道，
    无需新机制）。写失败如实抛 OSError：修复上下文丢失不该被静默。
    """
    from longtask.persistence.projections import HandoverData, parse_handover_markdown

    cdir = root / "contracts" / contract.contract_id
    handover_path = cdir / "handover.md"
    prev_stage = "repair"
    estimate = contract.draft.workload_initial_hours / 2.0
    completed: tuple[str, ...] = ()
    if handover_path.is_file():
        try:
            data, _violations = parse_handover_markdown(handover_path.read_text(encoding="utf-8"))
        except OSError:
            data = None
        if data is not None:
            completed = data.completed_evidence
            estimate = max(0.25, data.estimate_remaining_hours / 2.0)
    failed_lines = tuple(f"修复验收失败项：{c}" for c in brief.failed_checks) or (
        "按 verifier 证据修复未通过的验收项",
    )
    note_lines = tuple(f"备注：{n}" for n in brief.notes)
    next_action = brief.context_pointer or "按 verifier 失败证据修复，再交验收"
    data = HandoverData(
        current_stage=prev_stage,
        completed_evidence=completed,
        remaining=failed_lines,
        estimate_remaining_hours=estimate,
        next_action=next_action,
        constraints_digest=json.dumps(contract.draft.hard_constraints, ensure_ascii=False),
        source_attempt_id=verifier_attempt_id,
        open_risks=note_lines,
    )
    handover_path.parent.mkdir(parents=True, exist_ok=True)
    handover_path.write_text(data.format_markdown(), encoding="utf-8")
