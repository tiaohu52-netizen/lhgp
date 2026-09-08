"""常驻调度主循环（DESIGN §3.3、§6.4、§10）。

循环执行 run_daemon_tick + 执行桥接：每轮 tick 前观察/回收 attempt、
tick 后真实拉起新 attempt；分层唤醒（L0 电源守卫 / L1 计划任务）
与 control/interrupt 消费在轮内兑现。
"""

from __future__ import annotations

import json as _json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any

from longtask.adapters.registry import ExecutorRegistry
from longtask.cli.daemon_proc import (
    DAEMON_STOP_FILE,
    DEFAULT_TICK_INTERVAL_SECONDS,
    REGISTRY_FILE,
    rpc_socket_path,
)
from longtask.cli.runner import AttemptRunner
from longtask.cli.tick import run_daemon_tick
from longtask.contracts.schema import ContractState
from longtask.enforcement import (
    DeadlineEnforcer,
    DeadlineLevel,
    EnforcementAction,
    compute_deadline_level,
    render_text,
)
from longtask.persistence.decisions import earliest_next_decision_at
from longtask.persistence.events import EventType
from longtask.persistence.notifications import drain_notifications
from longtask.persistence.projections import rebuild_projection
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    auto_approve_drafted_contract,
    connect,
    ensure_schema,
    get_events,
    list_contracts,
    list_drafted_contracts,
)
from longtask.promoter.reconcile import ReconcileBranch, reconcile_attempts
from longtask.rpc.methods import Method
from longtask.rpc.server import parse_envelope, route
from longtask.rpc.transport import serve_unix_socket
from longtask.scheduler.wakeup import (
    NullSchedulePort,
    PowerPort,
    RtcAlarm,
    SchedulePort,
    SleepGuard,
    WindowsPowerPort,
    guard_needed,
)

# User control-plane mutations must preempt adaptive idle sleep.  A read-only
# RPC may wait for the next decision point; these methods cannot, because their
# effect is specifically to change what the next decision should be.
_WAKE_ON_RPC = frozenset(
    {
        Method.CONTRACT_APPROVE,
        Method.CONTRACT_PATCH,
        Method.CONTRACT_PAUSE,
        Method.CONTRACT_RESUME,
        Method.CONTRACT_CANCEL,
        Method.CONTRACT_ARBITRATE,
        Method.CONTRACT_REQUEST_VERIFICATION,
        Method.CONTROL_NOTIFY,
        Method.CONTROL_FOLLOWUP,
        Method.CONTROL_STEER,
        Method.CONTROL_INTERRUPT,
        Method.CONTROL_SPAWN,
        Method.LEASE_RENEW,
        Method.LEASE_RELEASE,
        Method.ATTEMPT_WRITE_BACK,
    }
)


def run_daemon_loop(
    root: Path,
    *,
    interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
    max_cycles: int | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    emit_fn: Callable[[str], None] | None = None,
    power_port: PowerPort | None = None,
    schedule_port: SchedulePort | None = None,
) -> dict[str, Any]:
    """常驻调度主循环（DESIGN §3.3）：循环执行 run_daemon_tick + 执行桥接。

    - 每轮从 registry.json 重载执行器池（用户框定与开关即时生效）；
    - AttemptRunner 在每轮 tick 前观察/回收 attempt、tick 后真实拉起新 attempt
      （§3.3 ticker 只调度不执行，拉起属于推动层执行桥接）；
    - 分层唤醒（§6.4/ADR-0002）：每轮刷新 L1 计划任务注册、按需持有/释放
      L0 电源守卫；端口可注入（测试零副作用），缺省 L0 用 Windows 实现、
      L1 无通道则记 wakeup/degraded 降级，不静默；
    - daemon.stop 存在 → 优雅退出并清理标记（配合 halt_daemon，§15.2）；
    - now_fn/sleep_fn/max_cycles 可注入：测试确定性，无真实墙钟、无真实长睡。
    """
    clock = now_fn if now_fn is not None else (lambda: datetime.now(UTC))
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    # submit-and-leave: 上一轮 daemon 退出时残留的 DRAFTED 合同在重启后由本轮
    # 入口立刻扫描升级，避免等满一个 tick interval 才被 dispatcher 看见。
    # 幂等：第二次启动若已无残留，就是一次零成本遍历。
    _auto_approve_drafted_contracts(conn, clock(), emit_fn)
    rpc_stop = threading.Event()
    wake_event = threading.Event()
    fired_tasks: SimpleQueue[str] = SimpleQueue()
    rpc_thread: threading.Thread | None = None
    token_path = root / "daemon.token"
    if token_path.is_file():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:

            def dispatch_rpc(raw: dict[str, Any]) -> dict[str, Any]:
                # Use the single canonical parser; re-coercing fields here would
                # silently reintroduce bool/string ambiguity at the daemon edge.
                envelope = parse_envelope(raw)
                rpc_conn = connect(StoreConfig(db_path=root / "state.db"))
                try:
                    ensure_schema(rpc_conn)
                    result = route(
                        envelope,
                        conn=rpc_conn,
                        now=clock(),
                        registry=ExecutorRegistry.load_from_file(root / REGISTRY_FILE),
                    )
                    if envelope.method is Method.DAEMON_WAKE and result.get("ok"):
                        task_id = result.get("result", {}).get("task_id")
                        if isinstance(task_id, str):
                            fired_tasks.put(task_id)
                            wake_event.set()
                    elif envelope.method in _WAKE_ON_RPC and result.get("ok"):
                        wake_event.set()
                    return result
                finally:
                    rpc_conn.close()

            def serve_rpc() -> None:
                try:
                    serve_unix_socket(
                        endpoint=rpc_socket_path(root),
                        token=token,
                        dispatch=dispatch_rpc,
                        stop_event=rpc_stop,
                    )
                except (OSError, RuntimeError) as exc:
                    if emit_fn is not None:
                        emit_fn(f"rpc/degraded: local socket unavailable: {exc}")

            rpc_thread = threading.Thread(
                target=serve_rpc,
                name="lhgp-rpc",
                daemon=True,
            )
            rpc_thread.start()
    runner = AttemptRunner(root, conn, ExecutorRegistry(), emit=emit_fn)
    guard = SleepGuard(power_port if power_port is not None else WindowsPowerPort())
    rtc = RtcAlarm(schedule_port if schedule_port is not None else NullSchedulePort())
    total_dispatched = 0
    total_expired = 0
    cycles = 0
    stopped = False
    try:
        while max_cycles is None or cycles < max_cycles:
            if (root / DAEMON_STOP_FILE).is_file():
                stopped = True
                break
            now_val = clock()
            registry = ExecutorRegistry.load_from_file(root / REGISTRY_FILE)
            runner.replace_registry(registry)
            # submit-and-leave: 每轮 tick 顶部把新落库（运行中通过 MCP/HTTP 提交）
            # 的 DRAFTED 合同按 auto_approve 范围升级为 ACTIVE。放在 reconcile 之前
            # 是为了确保 dispatcher（run_daemon_tick）以及 reconcile 看到的合同状态
            # 已经收敛过；与 RPC 线程可能并发的 contract/auto-approve 走同一 store
            # 原语，CAS 解决竞争，重复扫描是 no-op。
            _auto_approve_drafted_contracts(conn, now_val, emit_fn)
            # §9 步骤 2 / §11.3：先 reconcile 外部 attempt，再谈其它。
            # 本进程仍持有活句柄的 attempt 让给 runner 自己管（locally_tracked）。
            reconciled = reconcile_attempts(
                root,
                conn,
                now=now_val,
                resolve_adapter=runner.adapter_for,
                locally_tracked=runner.is_tracking,
                emit=emit_fn,
            )
            runner.adopt_reconciled_attempts()
            # 审计调度-R6：reconcile 分支 2 结算的 executor 成功同样要走
            # 交叉验收——否则重启窗口内的成功永远到不了 verifier，且下一轮
            # tick 会再派一个冗余 executor。
            _dispatch_verifiers_for_reconciled(root, conn, runner, reconciled, now_val)
            # 合同进入 cancelled/expired 后，控制面已不再允许继续推进；
            # 在 poll 前终止本进程持有的外部 attempt，避免已终止承诺
            # 继续消耗资源或产生迟到写回。
            _cancel_terminal_contract_attempts(conn, runner, now_val)
            # 先回收上一轮的收尾者并给存活者续心跳，再调度，最后拉起新 attempt
            runner.poll_attempts(now_val)
            # 消费 control/interrupt 请求（用户通过 RPC 打断执行中的 attempt）
            _consume_interrupt_requests(root, conn, runner, now_val)
            # 消费 verification/requested 请求（用户直接请求验收，§12.4）
            _consume_verification_requests(root, conn, runner, now_val)
            # P6：deadline 升级/锁住，在本轮调度生效前兑现（幂等：相同 level
            # 不重复落事件；breached 立即落 DEADLINE_BREACH_LOCKED，
            # 阻断 run_daemon_tick 后续派发新 attempt）。
            _enforce_deadlines(root, conn, now_val, emit_fn)
            # memory-and-wiki Phase 2：自动清掉过期的长期记忆（180/365 天
            # 衰减是 contract，没这条 sweep 就只是纸面承诺）。廉价
            # DELETE，命中 idx_memories_expires，无行就 no-op。
            _expire_due_memories(conn, now_val, emit_fn)
            # resilient-execution：context 预算自动交接（DESIGN §4.1）。在
            # 调度新 attempt 之前巡检本进程持有的 running attempt；逼近
            # 上限的写 handover/due 审计事件，迫使下一轮 tick 不再派发。
            _check_handover_due(conn, runner, now_val, emit_fn)
            # 消费由本机计划任务经 daemon/wake 投递的一次性 fired 信号；
            # 先解除旧登记，再由本轮 tick 计算并重新 arm 下一决策点。
            while True:
                try:
                    rtc_task_id = fired_tasks.get_nowait()
                except Empty:
                    break
                rtc.note_fired(rtc_task_id)
            res = run_daemon_tick(root, conn, registry, now=now_val, emit_fn=emit_fn)
            if emit_fn is not None:
                drain_notifications(
                    conn,
                    now=now_val,
                    deliver=lambda notification: emit_fn(
                        _json.dumps(
                            {
                                "notification": notification.event_type,
                                "channel": notification.channel,
                                "goal_id": notification.goal_id,
                                "payload": notification.payload,
                                "idempotency_key": notification.idempotency_key,
                            },
                            ensure_ascii=False,
                        )
                    ),
                )
            total_dispatched += int(res.get("dispatched", 0))
            total_expired += int(res.get("expired", 0))
            for started in res.get("attempts_started", []):
                runner.start_attempt(
                    now_val,
                    contract_id=str(started["contract_id"]),
                    attempt_id=str(started["attempt_id"]),
                    executor_id=str(started["executor_id"]),
                    model=str(started.get("model", "*")),
                )
            # 分层唤醒：L1 对齐 active 合同的唤醒注册；L0 按需持有/释放电源请求
            rtc.refresh(conn, now=now_val)
            needed, guard_cid = guard_needed(conn, now=now_val)
            if guard_cid is not None:
                guard.update(
                    conn,
                    now=now_val,
                    guard_needed=needed,
                    reason="active lease or urgency >= 1.0 (§6.4 L0)",
                    contract_id=guard_cid,
                )
            elif guard.held:
                # 释放事件挂空合同：全局事件由审计流可见，不属于任何单个合同
                guard.update(
                    conn,
                    now=now_val,
                    guard_needed=False,
                    reason="no active lease and urgency < 1.0 (§6.4 L0)",
                    contract_id="",
                )
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if interval_seconds > 0:
                # P4：按最早决策点自适应休眠（SPEC §9 next_decision_at）。
                # 有活 attempt 时仍要保底心跳节奏（续约/回收观察不能停），
                # 但不能因此跳过更早的 deadline 决策点；两者取较小值。
                # 用本轮已取的 now_val 计算时长，不二次调用 clock()（可注入
                # 有限迭代器，多调一次就 StopIteration）。
                sleep_seconds = interval_seconds
                next_at = earliest_next_decision_at(conn, now=now_val)
                if next_at is not None:
                    until = (next_at - now_val).total_seconds()
                    # A due decision must be handled immediately; a positive
                    # point is capped by the heartbeat interval even when a
                    # subprocess is currently active.
                    sleep_seconds = min(interval_seconds, max(0.0, until))
                if sleep_fn is time.sleep:
                    # 真实 daemon 用可被 daemon/wake 唤醒的等待；测试注入的
                    # sleep_fn 保持原有确定性语义，不触碰线程事件。
                    wake_event.wait(sleep_seconds)
                    wake_event.clear()
                else:
                    sleep_fn(sleep_seconds)
    finally:
        rpc_stop.set()
        if rpc_thread is not None:
            rpc_thread.join(timeout=2)
        if stopped:
            (root / DAEMON_STOP_FILE).unlink(missing_ok=True)
        conn.close()
    return {
        "ok": True,
        "cycles": cycles,
        "stopped_by_stop_file": stopped,
        "dispatched": total_dispatched,
        "expired": total_expired,
        "spawned": runner.spawned_count,
        "finished": runner.finished_count,
    }


def _expire_due_memories(
    conn: sqlite3.Connection,
    now: datetime,
    emit_fn: Callable[[str], None] | None,
) -> int:
    """Sweep expired long-term memories. Best-effort; failures are logged.

    The sweep and the audit event are written in a single
    ``with transaction(conn):`` block so a crash between the DELETE
    and the ``append_event`` either commits both or rolls back both.
    The previous shape ran the DELETE inside its own ``with conn:``
    (auto-committed) and the audit event in a separate transaction,
    so a crash in the window between them could drop the audit event
    while the deletes had already been persisted. ``emit_fn`` is
    called with a one-line summary iff rows were dropped.
    """
    try:
        from lhgp.memory import _expire_due_in_transaction
        from lhgp.persistence.schema import transaction
    except ImportError:
        return 0
    try:
        from longtask.persistence.events import EventType
        from longtask.persistence.store import append_event

        with transaction(conn):
            expired_ids = _expire_due_in_transaction(conn, now=now)
            n = len(expired_ids)
            if n == 0:
                return 0
            append_event(
                conn,
                contract_id=None,
                goal_id=None,
                event_type=EventType.MEMORY_EXPIRED,
                payload={"expired_count": n, "expired_ids": expired_ids},
                now=now,
                actor="daemon",
                role="system",
            )
    except Exception as exc:
        if emit_fn is not None:
            emit_fn(f"memory/expire: sweep failed: {exc}")
        return 0
    if emit_fn is not None:
        emit_fn(f"memory/expire: dropped {n} due memories")
    return n


def _check_handover_due(
    conn: sqlite3.Connection,
    runner: AttemptRunner,
    now: datetime,
    emit_fn: Callable[[str], None] | None,
) -> int:
    """Auto-handover detector (DESIGN §4.1)：每个 tick 检查本进程持有的
    running attempt 的 active.md 是否逼近 context 窗口。

    check_handover_due() 自身已处理「warm」分支：size/max_bytes ≥ 0.6 且
    上次 HANDOVER_DUE 早于 60s 时，自动落一条 debounced 审计事件并返回
    True。「overdue」分支（≥ 0.9）它只返回 True 不落事件，由本函数补一条
    紧急事件——否则审计日志看不到「已越线」这一刻，运维只能事后从
    上下文崩溃倒推。

    写入失败/contract 找不到/active.md 缺失均被吞掉；这是 best-effort
    巡检，不能让一个 attempt 的元数据异常把整轮 tick 拉黑。
    """
    from longtask.persistence.context import (
        ACTIVE_FILE,
        ATTEMPTS_DIR,
        CONTEXT_DIR,
        ContextPolicy,
        _resolve_data_root,
        check_handover_due,
    )
    from longtask.persistence.store import append_event, get_contract

    fired = 0
    for contract_id, attempt_id in runner.running_attempts():
        try:
            if not check_handover_due(conn, contract_id, attempt_id):
                continue
        except Exception as exc:  # never let one bad attempt break the tick
            if emit_fn is not None:
                emit_fn(f"handover/check: {contract_id}/{attempt_id} skipped: {exc}")
            continue
        try:
            view = get_contract(conn, contract_id)
            if view is None:
                continue
            policy = ContextPolicy.from_contract(view.draft)
            root = _resolve_data_root(conn)
            if root is None:
                continue
            active_path = (
                root
                / "contracts"
                / contract_id
                / CONTEXT_DIR
                / ATTEMPTS_DIR
                / attempt_id
                / ACTIVE_FILE
            )
            if not active_path.is_file():
                continue
            size = active_path.stat().st_size
            ratio = size / policy.max_bytes
        except Exception as exc:
            if emit_fn is not None:
                emit_fn(f"handover/check: {contract_id}/{attempt_id} stat failed: {exc}")
            continue
        if ratio < 0.9:
            # warm 分支的事件已由 check_handover_due 落过；不要再加一条。
            continue
        try:
            append_event(
                conn,
                contract_id=contract_id,
                attempt_id=attempt_id,
                event_type=EventType.HANDOVER_DUE,
                payload={
                    "size": size,
                    "max_bytes": policy.max_bytes,
                    "ratio": round(ratio, 4),
                    "reason": "auto_handover_overdue",
                },
                now=now,
                actor="daemon",
                role="system",
            )
        except Exception as exc:
            if emit_fn is not None:
                emit_fn(f"handover/due: {contract_id}/{attempt_id} audit failed: {exc}")
            continue
        fired += 1
        if emit_fn is not None:
            emit_fn(f"handover/due: {contract_id}/{attempt_id} ratio={ratio:.2f} (overdue)")
    return fired


def _dispatch_verifiers_for_reconciled(
    root: Path,
    conn: sqlite3.Connection,
    runner: Any,
    outcomes: Any,
    now: datetime,
) -> None:
    """reconcile 分支 2 结算的 executor 成功 → 派生交叉 verifier（§5.2）。

    审计调度-R6：reconcile 路径绕过 _finish_attempt，成功结算后从不派
    verifier，交叉验收断链且下一轮 tick 重派冗余 executor。此处对每个
    COLLECTED 且 state=succeeded 的 executor attempt 补一次验收派发；
    _dispatch_verifier 自带的活租约/预算守卫会拒绝越权派发。
    """
    for outcome in outcomes:
        if outcome.branch != ReconcileBranch.COLLECTED:
            continue
        row = conn.execute(
            "SELECT role, state, executor_id FROM attempts WHERE attempt_id = ? LIMIT 1",
            (outcome.attempt_id,),
        ).fetchone()
        if row is None or row[0] != "executor" or row[1] != "succeeded":
            continue
        runner._dispatch_verifier(
            now,
            contract_id=outcome.contract_id,
            executor_id=str(row[2] or ""),
        )


def _consume_verification_requests(
    root: Path,
    conn: sqlite3.Connection,
    runner: AttemptRunner,
    now: datetime,
) -> None:
    """消费 verification/requested 事件（SPEC §12.4 用户触发验收）。

    RPC handler（contract/request-verification）只做校验与事件落库；
    daemon 每轮 tick 顶部扫描请求事件，用持有进程表的 AttemptRunner
    派生独立 verifier（spawn 必须由 daemon 做——RPC handler 没有进程表，
    与 control/interrupt 相同的「写事件-消费」分工）。

    幂等：attempts 表一旦出现 role=verifier 行（含 terminal——
    §12.3 历史 verifier 的存在本就不阻止新派生，新的请求走新事件），
    同一请求事件不再重复兑现。
    """
    for contract in list_contracts(conn, limit=1000):
        if contract.state != ContractState.ACTIVE:
            continue
        requested = [
            event
            for event in get_events(conn, contract_id=contract.contract_id)
            if event.event_type == EventType.VERIFICATION_REQUESTED
        ]
        if not requested:
            continue
        consumed_request_ids: set[int] = set()
        for event in get_events(conn, contract_id=contract.contract_id):
            if event.event_type != EventType.VERIFICATION_CONSUMED:
                continue
            try:
                payload = _json.loads(event.payload_json or "{}")
                consumed_request_ids.add(int(payload["request_event_id"]))
            except (KeyError, TypeError, ValueError, _json.JSONDecodeError):
                # A malformed marker cannot prove consumption; leave the
                # request eligible and let the next tick retry it.
                continue
        pending = next(
            (event for event in requested if event.event_id not in consumed_request_ids),
            None,
        )
        if pending is None:
            continue
        # attempts.goal_id stores the owning Goal identity, not the contract
        # identity.  A contract may be bound to a long-lived goal, so using
        # contract_id here would miss an existing verifier and break the
        # request-consumption idempotence guarantee.
        already = conn.execute(
            "SELECT attempt_id FROM attempts "
            "WHERE contract_id = ? AND role = 'verifier' "
            "AND state NOT IN ('succeeded', 'failed', 'cancelled', 'stale', 'orphaned') "
            "LIMIT 1",
            (contract.contract_id,),
        ).fetchone()
        if already is not None:
            continue
        last_executor = conn.execute(
            "SELECT executor_id FROM attempts "
            "WHERE goal_id = ? AND role = 'executor' "
            "ORDER BY admitted_at DESC LIMIT 1",
            (contract.goal_id,),
        ).fetchone()
        executor_id = str(last_executor[0]) if last_executor else ""
        ok = runner._dispatch_verifier(
            now, contract_id=contract.contract_id, executor_id=executor_id
        )
        append_event(
            conn,
            contract_id=contract.contract_id,
            goal_id=contract.goal_id,
            event_type=EventType.VERIFICATION_CONSUMED,
            payload={
                "request_event_id": pending.event_id,
                "outcome": "dispatched" if ok else "refused",
            },
            now=now,
            actor="daemon",
            contract_revision=contract.revision,
            role="verifier",
        )
        if ok:
            append_event(
                conn,
                contract_id=contract.contract_id,
                goal_id=contract.goal_id,
                event_type=EventType.VERIFICATION_STARTED,
                payload={
                    "requested_by": "user",
                    "executor_of_record": executor_id,
                    "request_event_id": pending.event_id,
                },
                now=now,
                actor="daemon",
            )
            rebuild_projection(root, contract.contract_id, conn)


def _cancel_terminal_contract_attempts(
    conn: sqlite3.Connection,
    runner: AttemptRunner,
    now: datetime,
) -> None:
    """终止已取消/已过期合同的本进程 attempt（幂等、只处理本进程）。"""
    terminal_contracts = {
        contract.contract_id
        for contract in list_contracts(conn, limit=1000)
        if contract.state in (ContractState.CANCELLED, ContractState.EXPIRED)
    }
    for contract_id, attempt_id in runner.running_attempts():
        if contract_id not in terminal_contracts:
            continue
        runner.cancel_attempt(
            now,
            contract_id=contract_id,
            attempt_id=attempt_id,
            reason="contract reached terminal state",
            actor="daemon",
        )


def _consume_interrupt_requests(
    root: Path,
    conn: sqlite3.Connection,
    runner: AttemptRunner,
    now: datetime,
) -> None:
    """消费 control/interrupt 请求（DESIGN §10 可干涉、§6.4 仲裁时刻语义）。

    RPC handler 只写盘上事件；daemon 每轮 tick 顶部扫描「attempt/cancelled
    via=control/interrupt」事件，调用 AttemptRunner.cancel_attempt 兑现：
    adapter.cancel + attempt/cancelled 保留 + 租约释放 + 停追。

    幂等：cancel_attempt 对非 running attempt 返回 False 且不重复记事件；
    重扫已消费的 interrupt 事件是 no-op（attempt 已从 runner 移除）。
    """
    for contract in list_contracts(conn, limit=1000):
        for event in get_events(conn, contract_id=contract.contract_id):
            if event.event_type != EventType.ATTEMPT_CANCELLED:
                continue
            try:
                payload = _json.loads(event.payload_json or "{}")
            except ValueError:
                continue
            if payload.get("via") != "control/interrupt":
                continue
            if event.attempt_id is None:
                continue
            runner.cancel_attempt(
                now,
                contract_id=contract.contract_id,
                attempt_id=event.attempt_id,
                reason=str(payload.get("reason", "user interrupt")),
            )
            rebuild_projection(root, contract.contract_id, conn)


def _latest_escalation_level(conn: sqlite3.Connection, contract_id: str) -> DeadlineLevel | None:
    """最近一次 deadline 升级事件的 level；同 level 重扫不重复落事件。"""
    row = conn.execute(
        "SELECT payload_json FROM events "
        "WHERE contract_id = ? AND event_type IN (?, ?) "
        "ORDER BY event_id DESC LIMIT 1",
        (
            contract_id,
            EventType.DEADLINE_LEVEL_ESCALATED.value,
            EventType.DEADLINE_BREACH_LOCKED.value,
        ),
    ).fetchone()
    if row is None:
        return None
    try:
        return DeadlineLevel(_json.loads(row[0] or "{}").get("level", ""))
    except (ValueError, _json.JSONDecodeError):
        return None


def _enforce_deadlines(
    root: Path,
    conn: sqlite3.Connection,
    now: datetime,
    emit: Callable[[str], None] | None = None,
) -> list[EnforcementAction]:
    """P6: deadline 升级 + 锁住（每轮 tick 顶部调用一次）。

    遍历 ACTIVE/BLOCKED 合同，按当前时间算 level；level 与上次升级相同则跳过
    （幂等）；level 升级或 breached 落 DEADLINE_LEVEL_ESCALATED /
    DEADLINE_BREACH_LOCKED 事件，重建 projection 让后续 run_daemon_tick
    看到新 deadline_status。

    breached 等级的 action.lock_new_attempts 由后端调度器在 attempt 派发
    时按 contract.deadline_status==MISSED 拒绝（与 DEADLINE_STATUS_CHANGED
    共享同一守门），故此处只发事件、不另开闸。
    """
    enforcer = DeadlineEnforcer()
    actions: list[EnforcementAction] = []
    for contract in list_contracts(conn, limit=1000):
        if contract.state not in (ContractState.ACTIVE, ContractState.BLOCKED):
            continue
        deadline = contract.draft.deadline_at
        window_seconds = (deadline - contract.created_at).total_seconds()
        # window<=0 表示 deadline<=created_at（数据异常）。
        # 仍要走完流程：compute_deadline_level 看 remaining<=0 必返 BREACHED，
        # 至少保证事件落地；不能因为窗口算不出来就把 breached 合同漏过。
        total_seconds: float | None = window_seconds if window_seconds > 0 else None
        decision = compute_deadline_level(deadline, now=now, total_seconds=total_seconds)
        if decision.level == DeadlineLevel.NORMAL:
            continue
        last_level = _latest_escalation_level(conn, contract.contract_id)
        if last_level is not None and last_level.value == decision.level.value:
            continue
        action = enforcer.enforce(contract, decision, now=now)
        actions.append(action)
        event_type = (
            EventType.DEADLINE_BREACH_LOCKED
            if action.level == DeadlineLevel.BREACHED
            else EventType.DEADLINE_LEVEL_ESCALATED
        )
        append_event(
            conn,
            contract_id=contract.contract_id,
            goal_id=contract.goal_id,
            event_type=event_type,
            payload={
                "level": action.level.value,
                "actions": list(action.actions),
                "rationale": action.rationale,
                "remaining_seconds": action.metadata.get("remaining_seconds"),
                "elapsed_ratio": action.metadata.get("elapsed_ratio"),
            },
            now=now,
            actor="daemon",
            contract_revision=contract.revision,
        )
        rebuild_projection(root, contract.contract_id, conn)
    if emit is not None and actions:
        emit(render_text(actions))
    return actions


def _auto_approve_drafted_contracts(
    conn: sqlite3.Connection,
    now: datetime,
    emit_fn: Callable[[str], None] | None,
) -> int:
    """submit-and-leave 扫尾：把 DRAFTED 合同按 auto_approve 范围升级为 ACTIVE。

    入口（daemon 启动）+ 每轮 tick 顶部都会调一次；store 原语
    :func:`auto_approve_drafted_contract` 自身已吞掉
    RevisionConflictError / StoreError 并返回 False，因此
    - 第二次连扫同集合合同全部是 no-op（DRAFTED → ACTIVE 已发生，不在结果里）；
    - 任何单一合同的失败不会传染到本轮 tick 其它合同或 dispatcher。
    本函数再加一层 try/except 是 belt-and-suspenders：哪怕 store 抛出
    未声明的异常（例如 contract 视图字段异常触发 AttributeError），
    sweep 仍能给剩余合同一个机会，并把异常信息降级成一条 emit。
    """
    try:
        drafted = list_drafted_contracts(conn)
    except Exception as exc:
        if emit_fn is not None:
            emit_fn(f"contract/auto-approve: list failed: {exc}")
        return 0
    approved = 0
    for contract in drafted:
        try:
            if auto_approve_drafted_contract(conn, contract, now):
                approved += 1
        except Exception as exc:
            if emit_fn is not None:
                emit_fn(f"contract/auto-approve: {contract.contract_id} skipped: {exc}")
            continue
    return approved
