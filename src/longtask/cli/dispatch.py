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

P1 fix（2026-09-08 review）：``wake_blocked_after_plan_approval`` 让
曾经因为 gate 拒接而陷入 BLOCKED(NO_EXECUTOR) 的合同，在计划被接
受后立刻重新转 ACTIVE 并把 next_decision_at 拉到当前时刻；否则
下次 tick 仍会因 state=BLOCKED 跳过它（tick 只处理 ACTIVE 状态
的合同）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from lhgp.contracts.contract_view import ContractState
from lhgp.contracts.plan import _extract_check_identifiers
from longtask.adapters.base import ExecutorAdapter, PrepareRefusedError
from longtask.adapters.registry import ExecutorRegistry, RegistryEntry
from longtask.cli.runner import build_attempt_input
from longtask.contracts.schema import ContractView
from longtask.persistence.events import EventType
from longtask.persistence.projections import rebuild_projection
from longtask.persistence.store import (
    acquire_lease,
    append_event,
    get_contract,
    get_lease,
    reclaim_lease,
)
from longtask.promoter.records import _record_attempt
from longtask.promoter.urgency import UrgencyTier

# Plan gate: how far back to look for a PLAN_APPROVED that hasn't been
# superseded by a PLAN_REJECTED. 24h is generous for a planning cycle but
# short enough that a stale approval doesn't pin a contract forever.
PLAN_GATE_LOOKBACK_SECONDS = 24 * 60 * 60


def wake_blocked_after_plan_approval(
    conn: sqlite3.Connection,
    contract_id: str,
    now: datetime,
) -> bool:
    """Re-activate a contract that was blocked waiting for a plan.

    Returns True iff a state transition was actually performed. The
    contract is only woken when:

    - it currently sits in :data:`ContractState.BLOCKED`
    - and its ``blocked_reason`` is :data:`BlockReason.NO_EXECUTOR`
      (set by the gate refusal path: "plan gate: no recent
      PLAN_APPROVED event"). Other blocked reasons (lease-dead,
      budget-exhausted, need-user, etc.) are out of scope — a plan
      approval cannot rescue them.

    On wake-up the contract is moved back to ACTIVE (clearing the
    blocked_reason) and ``next_decision_at`` is pinned to ``now`` so
    the next tick picks it up immediately instead of waiting for the
    old scheduling point. A dedicated ``CONTRACT_UNBLOCKED`` event is
    appended for audit so the transition is visible in the event log
    alongside the PLAN_APPROVED that triggered it.
    """
    from lhgp.contracts.contract_view import BlockReason

    view = get_contract(conn, contract_id)
    if view is None:
        return False
    if view.state != ContractState.BLOCKED:
        return False
    if view.blocked_reason != BlockReason.NO_EXECUTOR:
        return False

    # Direct UPDATE rather than update_contract_state: the wake is a
    # bookkeeping transition, not a contract-content change. Bumping
    # the revision here would invalidate the just-recorded
    # PLAN_APPROVED's contract_revision binding (the gate checks
    # revision equality between the event and the current contract).
    # We still record a dedicated event for audit so the transition
    # is visible in the event log.
    #
    # P1 review (2026-09-08): the read-then-write was vulnerable to a
    # TOCTOU race — a concurrent user cancellation could complete
    # between the read above and the UPDATE below, and this helper
    # would silently overwrite a cancelled state back to active.
    # Fix: include ``state`` and ``revision`` in the WHERE clause so
    # the UPDATE is a no-op if the contract is no longer in the
    # expected (BLOCKED, revision=N) state. We then read the row
    # again and only emit the unblocked event when the UPDATE
    # actually changed a row.
    from lhgp.persistence.schema import transaction

    with transaction(conn):
        cur = conn.execute(
            "UPDATE contracts SET state = ?, blocked_reason = NULL, "
            "next_decision_at = ?, updated_at = ? "
            "WHERE contract_id = ? AND state = ? AND revision = ?",
            (
                ContractState.ACTIVE.value,
                now.isoformat(),
                now.isoformat(),
                contract_id,
                view.state.value,
                view.revision,
            ),
        )
        if cur.rowcount == 0:
            # The contract was modified (cancelled, archived, re-blocked,
            # etc.) between our read and this write. The new owner of
            # the state is authoritative; we do not touch it and do
            # not emit an event.
            return False
        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.CONTRACT_UNBLOCKED,
            payload={
                "reason": "plan gate cleared by PLAN_APPROVED",
                "previous_state": view.state.value,
                "new_state": ContractState.ACTIVE.value,
            },
            now=now,
            actor="daemon",
            goal_id=view.goal_id,
            contract_revision=view.revision,
            role="promoter",
        )
    return True


def mark_blocked_capacity_full(
    conn: sqlite3.Connection,
    contract_id: str,
    now: datetime,
) -> bool:
    """Transition an ACTIVE contract to BLOCKED(CAPACITY_FULL) without
    bumping the contract revision.

    P1 review (2026-09-08, 3rd round): the standard
    ``update_contract_state`` path bumps the contract revision, which
    is correct for content changes (acceptance-criteria edit, draft
    patch, …) but wrong for *bookkeeping* transitions like a
    cap-saturation block. Bumping the revision here would
    invalidate any just-approved ``PLAN_APPROVED`` (its
    ``contract_revision`` no longer matches the now-bumped contract)
    and the contract would then refuse to dispatch even after a wake,
    looking like ``NO_EXECUTOR`` to the operator.

    Fix: same direct-UPDATE pattern as
    :func:`wake_blocked_after_plan_approval` —
    write state/blocked_reason, leave revision untouched, and
    record a ``contract/blocked`` event for audit. The
    CAS guard on (state, revision) prevents a concurrent cancel
    from being silently overwritten.
    """
    from lhgp.contracts.contract_view import BlockReason
    from lhgp.persistence.schema import transaction

    view = get_contract(conn, contract_id)
    if view is None:
        return False
    if view.state != ContractState.ACTIVE:
        return False

    with transaction(conn):
        # next_decision_at 置 NULL，而不是置 now。容量饱和没有「到某个
        # 时刻就有额度」这回事——重试是事件驱动的（额度释放、心跳）。
        # 置 now 会踩到 earliest_next_decision_at：它的 states 含
        # 'blocked'，且对已过期的决策点**原样返回**（R1 审查的有意设计，
        # 见 decisions.py 注释：不能让 deadline 决策被拖到下个周期）。
        # 两者相乘 → 守护进程每轮算出 until=0 → sleep_seconds=0 →
        # 完全不睡，一边唤醒一边重新阻塞，CPU 空转（审计 B3）。
        # NULL 让 daemon 回落到心跳间隔，重试节奏由心跳给出。
        cur = conn.execute(
            "UPDATE contracts SET state = ?, blocked_reason = ?, "
            "next_decision_at = NULL, updated_at = ? "
            "WHERE contract_id = ? AND state = ? AND revision = ?",
            (
                ContractState.BLOCKED.value,
                BlockReason.CAPACITY_FULL.value,
                now.isoformat(),
                contract_id,
                view.state.value,
                view.revision,
            ),
        )
        if cur.rowcount == 0:
            return False
        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.CONTRACT_BLOCKED,
            payload={
                "reason": (
                    "every eligible executor is at max_concurrent_attempts; "
                    "auto-retry when a lease is released"
                ),
                "previous_state": view.state.value,
                "new_state": ContractState.BLOCKED.value,
                "blocked_reason": BlockReason.CAPACITY_FULL.value,
            },
            now=now,
            actor="daemon",
            goal_id=view.goal_id,
            contract_revision=view.revision,
            role="promoter",
        )
    return True


def wake_blocked_capacity_full(
    conn: sqlite3.Connection,
    contract_id: str,
    now: datetime,
    *,
    registry: ExecutorRegistry | None = None,
) -> bool:
    """Re-activate a contract that was blocked only because every
    eligible executor was busy.

    Returns True iff a state transition was actually performed. The
    contract is only woken when:

    - it currently sits in :data:`ContractState.BLOCKED`
    - its ``blocked_reason`` is :data:`BlockReason.CAPACITY_FULL`
      (set by the dispatch path when match_candidates saw at least
      one eligible candidate but every one was cap-saturated).
    - the executor pool actually has free capacity right now. Two
      granularities, chosen by whether the caller can supply the
      registry:

      * ``registry`` given (production path, ``tick.py``): precise —
        ``match_candidates`` is re-run against the live per-executor
        running counts, so the contract is woken exactly when a
        dispatch would now succeed. A non-empty candidate list means
        a previously saturated executor has a free slot.
      * ``registry`` omitted: conservative — the total running count
        must have dropped to zero. Sound (no in-flight attempt implies
        free capacity) but can wait longer than necessary when the
        busy executor is not one of this contract's candidates.

      Auditor note (B3): the "free capacity right now" clause was
      documented here from the start, but the implementation only
      checked state+reason — so every tick woke every CAPACITY_FULL
      contract even when nothing had changed. Combined with the
      ``next_decision_at = now`` written by
      :func:`mark_blocked_capacity_full`, that made the daemon spin.

    Same direct-UPDATE pattern as
    :func:`wake_blocked_after_plan_approval`: skip the revision bump
    so any concurrent state-derivation stays consistent. Records a
    ``contract/unblocked`` event for audit.
    """
    from lhgp.contracts.contract_view import BlockReason

    view = get_contract(conn, contract_id)
    if view is None:
        return False
    if view.state != ContractState.BLOCKED:
        return False
    if view.blocked_reason != BlockReason.CAPACITY_FULL:
        return False

    # 容量前置条件（docstring 第 3 条）：额度没释放就不要唤醒。
    from longtask.persistence.attempts import count_running_by_executor

    running_attempts = count_running_by_executor(conn)
    if registry is not None:
        if not registry.match_candidates(view.draft, running_attempts=running_attempts):
            return False
    elif any(running_attempts.values()):
        return False

    from lhgp.persistence.schema import transaction

    with transaction(conn):
        # P1 review (2026-09-08): same TOCTOU defense as
        # wake_blocked_after_plan_approval — the WHERE clause pins
        # state+revision so a concurrent cancel cannot be overwritten.
        cur = conn.execute(
            "UPDATE contracts SET state = ?, blocked_reason = NULL, "
            "next_decision_at = ?, updated_at = ? "
            "WHERE contract_id = ? AND state = ? AND revision = ?",
            (
                ContractState.ACTIVE.value,
                now.isoformat(),
                now.isoformat(),
                contract_id,
                view.state.value,
                view.revision,
            ),
        )
        if cur.rowcount == 0:
            return False
        append_event(
            conn,
            contract_id=contract_id,
            event_type=EventType.CONTRACT_UNBLOCKED,
            payload={
                "reason": "executor capacity available, retrying dispatch",
                "previous_state": view.state.value,
                "new_state": ContractState.ACTIVE.value,
            },
            now=now,
            actor="daemon",
            goal_id=view.goal_id,
            contract_revision=view.revision,
            role="promoter",
        )
    return True


def _has_recent_plan_approval(
    conn: sqlite3.Connection,
    contract_id: str,
    contract_revision: int,
    accepted_check_ids: list[str],
    spec_hash: str | None,
    now: datetime,
) -> bool:
    """Return True iff the contract has a PLAN_APPROVED event in the
    last :data:`PLAN_GATE_LOOKBACK_SECONDS` that is not superseded by a
    later PLAN_REJECTED **and is still binding to the current contract
    state**.

    The check is pure SQL plus a payload inspection:
    - The most recent plan verdict (approved or rejected) within the
      lookback window must be ``PLAN_APPROVED``.
    - Its ``contract_revision`` must equal the current contract's
      revision; otherwise a contract revision bump has invalidated
      the prior approval.
    - Its ``accepted_check_ids`` must equal the current set; otherwise
      acceptance criteria were edited after the plan was approved and
      the plan is no longer guaranteed to cover the new criteria.
    - Its ``spec_hash`` must equal the contract's current spec_hash;
      a spec edit invalidates the prior approval so the executor does
      not act on a plan written against an older acceptance spec.

    Without these checks, an old approval would silently let new
    acceptance criteria or a new spec through.  See the P1 reviews of
    2026-09-08 (acceptance criteria) and 2026-09-08 (spec binding).
    """
    cutoff_iso = (now - timedelta(seconds=PLAN_GATE_LOOKBACK_SECONDS)).isoformat()
    row = conn.execute(
        "SELECT event_type, contract_revision, payload_json FROM events "
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
    event_type, event_contract_revision, payload_json = row
    if str(event_type) != EventType.PLAN_APPROVED:
        return False
    payload = _parse_plan_payload(payload_json)
    # 6th-round P1 fix (2026-09-09): the lifecycle
    # binding reads from the events.contract_revision
    # COLUMN when present.  ``update_contract_state``
    # and ``patch_contract`` re-stamp any prior
    # PLAN_APPROVED to the new revision on lifecycle-only
    # bumps, so the column is the single source of truth
    # for "is this approval still binding to the current
    # contract revision?".  Legacy events written before
    # the migration was added carry NULL on the column
    # and a snapshot contract_revision in the payload —
    # fall back to the payload so pre-fix events still
    # gate correctly.
    if event_contract_revision is not None:
        if event_contract_revision != contract_revision:
            return False
    else:
        if payload.get("contract_revision") != contract_revision:
            return False
    stored_checks = payload.get("accepted_check_ids")
    if not isinstance(stored_checks, list):
        return False
    if list(stored_checks) != list(accepted_check_ids):
        return False
    # Spec hash: if the contract declares a spec, the approval must
    # bind the same hash. A None stored value alongside a current
    # spec means the plan was approved before the spec was attached.
    stored_spec_hash = payload.get("spec_hash")
    if spec_hash is not None and stored_spec_hash != spec_hash:
        return False
    return not (spec_hash is None and stored_spec_hash is not None)


def _parse_plan_payload(payload_json: str | None) -> dict[str, object]:
    """Parse the JSON payload of a plan verdict event, returning an empty
    dict on any error.  Older events may have been written before the
    binding fields were added; the gate must treat them as stale
    (missing ``contract_revision`` fails the match), not crash.
    """
    import json

    if not payload_json:
        return {}
    try:
        decoded = json.loads(payload_json)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
    if _plan_gate_required(contract) and not _has_recent_plan_approval(
        conn,
        cid,
        contract.revision,
        list(_extract_check_identifiers(contract)),
        contract.draft.acceptance.spec_hash,
        now,
    ):
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
    probe_input, _probe_consumed, _probe_ids = build_attempt_input(
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
