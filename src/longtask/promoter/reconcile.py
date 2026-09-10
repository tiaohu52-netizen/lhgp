"""Reconciler：恢复外部 attempt 观察关系（SPEC §8、§9 步骤 2、§11.3）。

运行时启动后必须先 reconcile（§11.3）。四分支：

1. 能确认同一外部 run 仍活着   → 重新绑定并续租
2. 能确认已终止               → collect 结果并结算 attempt
3. 状态未知                   → 标记 orphaned，recovery grace 内不得重复 spawn
4. 宽限后仍未知               → fence 旧 generation，记录风险，让位给重新派发

边界（SPEC §8）：Reconciler 只恢复观察关系与簿记，**不决定重新派发**——
分支 4 只负责 fence，是否换人由 Dispatcher/Promoter 在下一轮 tick 决定。
因此本模块不 import cli 层，也不调 dispatch。

「不得重复 spawn」怎么落地：宽限期内由 Reconciler 代持并续租。租约活着
时 decide() 会把档位封顶到 remind（§7），分发路径自然不会另起会话——
不靠调度层额外配合，也不靠祈祷。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from longtask.adapters.base import ExecutorAdapter
from longtask.adapters.handles import (
    EXTERNAL_STATE_UNKNOWN,
    RECOVERY_NONRECOVERABLE,
    RECOVERY_REATTACH,
    ExternalRunHandle,
    parse_legacy_session_ref,
)
from longtask.adapters.processes import process_alive
from longtask.contracts.schema import AttemptState
from longtask.persistence.attempts import (
    StoredAttempt,
    list_reconcilable_attempts,
    mark_attempt_orphaned,
    set_attempt_state,
)
from longtask.persistence.events import EventType
from longtask.persistence.events_query import append_event
from longtask.persistence.leases import get_lease, release_lease, renew_lease
from longtask.persistence.projections import rebuild_projection
from longtask.persistence.store import LeaseFencedError, attempt_evidence_binding, get_contract

# recovery grace 兜底：合同未声明 continuity.recovery_grace_minutes 时用
DEFAULT_RECOVERY_GRACE = timedelta(minutes=5)
# 续约超时兜底：合同读不到时的最小保护窗口
_FALLBACK_LEASE_TIMEOUT = timedelta(minutes=10)


class ReconcileBranch(StrEnum):
    """reconcile 四分支 + 跳过（SKIPPED 不是第五个分支，只是本轮不动作）。"""

    REATTACHED = "reattached"
    COLLECTED = "collected"
    ORPHAN_GRACED = "orphan-graced"
    FENCED_REDISPATCHED = "fenced-redispatched"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """一条 attempt 的 reconcile 结果：做了什么、为什么。"""

    attempt_id: str
    contract_id: str
    branch: ReconcileBranch
    detail: str
    external_state: str | None = None


def reconcile_attempts(
    root: Path,
    conn: sqlite3.Connection,
    *,
    now: datetime,
    resolve_adapter: Callable[[str | None], ExecutorAdapter | None],
    recovery_grace: timedelta = DEFAULT_RECOVERY_GRACE,
    locally_tracked: Callable[[str], bool] | None = None,
    emit: Callable[[str], None] | None = None,
) -> list[ReconcileOutcome]:
    """全量 reconcile：扫描所有非终态 attempt，逐条走四分支（§11.3）。

    - 幂等：重跑不会把宽限期续期（mark_attempt_orphaned 只在首次写
      orphaned_at），也不会重复结算已终态的 attempt；
    - fail-closed：观察不到、适配器不在、句柄缺失，一律按「状态未知」处理，
      绝不当成已终止，也绝不当成仍活着；
    - locally_tracked：本进程仍持有活句柄的 attempt 直接跳过。这不是偷懒，
      而是必要保护——对活着的 Popen 再走一次 reattach 会用按 pid 重绑的
      句柄覆盖真实句柄，把可用的 collect 通道弄丢。
    """
    outcomes: list[ReconcileOutcome] = []
    for attempt in list_reconcilable_attempts(conn):
        if locally_tracked is not None and locally_tracked(attempt.attempt_id):
            continue
        # 审计进程-R2：合同冻结区 continuity.recovery_grace_minutes 是
        # 生效配置而非文档摆设——按 attempt 所属合同覆盖默认宽限。
        effective_grace = recovery_grace
        contract = get_contract(conn, attempt.contract_id or attempt.goal_id)
        if contract is not None:
            effective_grace = timedelta(minutes=contract.draft.continuity.recovery_grace_minutes)
        outcome = _reconcile_one(
            root,
            conn,
            attempt,
            now=now,
            resolve_adapter=resolve_adapter,
            grace=effective_grace,
            emit=emit,
        )
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def attempt_handle(attempt: StoredAttempt) -> ExternalRunHandle | None:
    """从 attempt 行还原外部句柄（§11.3）。

    放在 promoter 而不是 persistence：句柄类型是 adapters 协议面的一部分，
    依赖方向只能是 adapters → persistence，persistence 不许反向 import。

    优先用 P3 的句柄列；老 attempt 只有 payload 里的 session_ref 字符串时
    走 parse_legacy_session_ref 兼容解析，拿不到就返回 None（状态未知）。
    """
    if attempt.external_run_id and attempt.session_locator:
        return ExternalRunHandle(
            external_run_id=attempt.external_run_id,
            session_locator=attempt.session_locator,
            recovery_strategy=attempt.recovery_strategy or RECOVERY_NONRECOVERABLE,
            capability_snapshot=attempt.capability_snapshot,
            process_identity=attempt.process_identity,
        )
    legacy = attempt.payload.get("session_ref")
    if isinstance(legacy, str) and legacy:
        return parse_legacy_session_ref(legacy)
    return None


def _reconcile_one(
    root: Path,
    conn: sqlite3.Connection,
    attempt: StoredAttempt,
    *,
    now: datetime,
    resolve_adapter: Callable[[str | None], ExecutorAdapter | None],
    grace: timedelta,
    emit: Callable[[str], None] | None,
) -> ReconcileOutcome | None:
    """单条 attempt 的四分支判定。"""
    cid = _resolve_contract_id(conn, attempt)
    if cid is None:
        _emit(
            emit,
            f"reconcile/skipped-ambiguous-contract:{attempt.attempt_id}:{attempt.goal_id}",
        )
        return ReconcileOutcome(
            attempt_id=attempt.attempt_id,
            contract_id=attempt.goal_id,
            branch=ReconcileBranch.SKIPPED,
            detail="attempt contract identity is ambiguous; no recovery or write-back performed",
            external_state=None,
        )
    lease = get_lease(conn, cid)
    holds_lease = lease is not None and lease.holder_attempt_id == attempt.attempt_id

    # 已在宽限中：只做宽限维护或到期 fence（分支 3 续 / 分支 4）
    if attempt.state == AttemptState.ORPHANED.value:
        return _sweep_orphan(
            root, conn, attempt, lease, holds_lease, now=now, grace=grace, emit=emit
        )

    handle = attempt_handle(attempt)
    if handle is None:
        return _orphan(
            root,
            conn,
            attempt,
            holds_lease=holds_lease,
            lease_generation=lease.generation if lease else None,
            now=now,
            emit=emit,
            detail="no persisted external run handle: external state unknown (§11.3)",
        )

    adapter = resolve_adapter(attempt.executor_id)
    if adapter is None:
        return _orphan(
            root,
            conn,
            attempt,
            holds_lease=holds_lease,
            lease_generation=lease.generation if lease else None,
            now=now,
            emit=emit,
            detail=f"executor {attempt.executor_id!r} unavailable: cannot observe external run",
        )

    if not handle.is_recoverable():
        return _orphan(
            root,
            conn,
            attempt,
            holds_lease=holds_lease,
            lease_generation=lease.generation if lease else None,
            now=now,
            emit=emit,
            detail=f"recovery_strategy={handle.recovery_strategy!r}: "
            "external run unobservable, state unknown (§11.3)",
        )

    if not adapter.reattach(handle):
        # reattach 拒绝 ≠ 已终止（身份不可证时绝不当死）。但 pid 死活是
        # 另一个问题：pid 确认不存在 = 该 run 必然已终止（收尸后 start_time
        # 读不到的身份盲区，不掩盖「进程没了」这个事实）。
        # 用 pid 做终态确认不违反 §11.3「pid 不单独作为身份真相」——
        # 身份（是不是同一 run）不判，死活（进程在不在）如实判。
        # 退出码不可得，走分支 2 如实结算（exit_code_known=False）。
        # pid 死活探测仅对 reattach 策略句柄可信——spawn 时真实记录过
        # pid；poll/legacy 句柄的 pid 只是解析提示（可能是占位数字），
        # 不得拿去判终态（fail-closed：宁可 orphan 也不猜）。
        pid = (
            _pid_from_identity(handle.process_identity)
            if handle.recovery_strategy == RECOVERY_REATTACH
            else None
        )
        if pid is not None and process_alive(pid) is False:
            return _collect(
                root,
                conn,
                attempt,
                adapter,
                handle,
                AttemptState.FAILED.value,
                holds_lease=holds_lease,
                lease_generation=lease.generation if lease else None,
                now=now,
                emit=emit,
                exit_code_known=False,
                collect_note=(
                    "pid confirmed gone after reattach refused (post-reap window, §11.3): "
                    "settled failed with unknown exit code"
                ),
            )
        # 无法确认 ≠ 已终止：这是 §11.3 分支 3 的入口
        return _orphan(
            root,
            conn,
            attempt,
            holds_lease=holds_lease,
            lease_generation=lease.generation if lease else None,
            now=now,
            emit=emit,
            detail="adapter cannot confirm the same external run: state unknown (§11.3)",
        )

    try:
        observation = adapter.observe(attempt.attempt_id)
    except Exception as exc:  # 观察失败即未知，不猜
        return _orphan(
            root,
            conn,
            attempt,
            holds_lease=holds_lease,
            lease_generation=lease.generation if lease else None,
            now=now,
            emit=emit,
            detail=f"observe failed after reattach: {exc} (treated as unknown, §11.3)",
        )

    state = str(observation.get("state", ""))
    if state == EXTERNAL_STATE_UNKNOWN:
        return _orphan(
            root,
            conn,
            attempt,
            holds_lease=holds_lease,
            lease_generation=lease.generation if lease else None,
            now=now,
            emit=emit,
            detail="external run identity unverifiable: state unknown (§11.3)",
        )
    if state == AttemptState.RUNNING.value:
        return _reattach(root, conn, attempt, handle, lease, holds_lease, now=now, emit=emit)
    # 已确认终态：分支 2 —— collect 并结算
    return _collect(
        root,
        conn,
        attempt,
        adapter,
        handle,
        state,
        holds_lease=holds_lease,
        lease_generation=lease.generation if lease else None,
        now=now,
        emit=emit,
    )


def _reattach(
    root: Path,
    conn: sqlite3.Connection,
    attempt: StoredAttempt,
    handle: ExternalRunHandle,
    lease: Any,
    holds_lease: bool,
    *,
    now: datetime,
    emit: Callable[[str], None] | None,
) -> ReconcileOutcome:
    """分支 1：确认同一外部 run 仍活着 → 重新绑定并续租（§11.3）。"""
    cid = _resolve_contract_id(conn, attempt) or attempt.goal_id
    set_attempt_state(
        conn,
        attempt_id=attempt.attempt_id,
        state=AttemptState.RUNNING.value,
        now=now,
    )
    renewed = holds_lease and _renew(conn, cid, attempt.attempt_id, lease.generation, now)
    append_event(
        conn,
        contract_id=cid,
        attempt_id=attempt.attempt_id,
        event_type=EventType.RECONCILE_REATTACHED,
        payload={
            "external_run_id": handle.external_run_id,
            "session_locator": handle.session_locator,
            "recovery_strategy": handle.recovery_strategy,
            "lease_renewed": renewed,
            "reason": "same external run confirmed alive after restart (§11.3 branch 1)",
        },
        now=now,
        actor="daemon",
        goal_id=cid,
        contract_revision=attempt.contract_revision,
        role="system",
    )
    rebuild_projection(root, cid, conn)
    _emit(emit, f"reconcile/reattached:{cid}:{attempt.attempt_id}")
    return ReconcileOutcome(
        attempt_id=attempt.attempt_id,
        contract_id=cid,
        branch=ReconcileBranch.REATTACHED,
        detail="external run confirmed alive; lease "
        + ("renewed" if renewed else "not held, left untouched"),
        external_state=AttemptState.RUNNING.value,
    )


def _resolve_contract_id(conn: sqlite3.Connection, attempt: StoredAttempt) -> str | None:
    """Resolve a persisted attempt to one contract without guessing.

    New rows carry ``contract_id`` explicitly.  For legacy rows, fallback is
    safe only when the Goal has exactly one contract (or one revision match).
    """
    if attempt.contract_id:
        return attempt.contract_id
    rows = conn.execute(
        "SELECT contract_id FROM contracts WHERE goal_id = ? ORDER BY contract_id",
        (attempt.goal_id,),
    ).fetchall()
    if len(rows) == 1:
        return str(rows[0][0])
    matching = [
        str(row[0])
        for row in conn.execute(
            "SELECT contract_id FROM contracts WHERE goal_id = ? AND revision = ?",
            (attempt.goal_id, attempt.contract_revision),
        ).fetchall()
    ]
    return matching[0] if len(matching) == 1 else None


def _collect(
    root: Path,
    conn: sqlite3.Connection,
    attempt: StoredAttempt,
    adapter: ExecutorAdapter,
    handle: ExternalRunHandle,
    observed_state: str,
    *,
    holds_lease: bool,
    lease_generation: int | None,
    now: datetime,
    emit: Callable[[str], None] | None,
    exit_code_known: bool = True,
    collect_note: str | None = None,
) -> ReconcileOutcome:
    """分支 2：确认已终止 → collect 结果并结算 attempt（§11.3）。

    exit_code_known=False + collect_note：pid 确认消失但无法 collect 的
    收尸后窗口（reattach 已拒绝）——如实结算 failed，不猜退出码。
    """
    cid = _resolve_contract_id(conn, attempt) or attempt.goal_id
    payload: dict[str, Any] = {
        "external_run_id": handle.external_run_id,
        "session_locator": handle.session_locator,
    }
    state = observed_state
    if collect_note is not None:
        # 进程已确认消失：不再调 collect（句柄不可用），直接如实结算
        payload["exit_code_known"] = exit_code_known
        payload["collect_note"] = collect_note
    else:
        try:
            collected = adapter.collect(attempt.attempt_id)
            payload["returncode"] = collected.get("returncode")
            payload["exit_code_known"] = collected.get("exit_code_known", True)
            if collected.get("error_class"):
                payload["error_class"] = collected["error_class"]
        except Exception as exc:
            # 回收失败如实记账：退出码不可得不等于成功（detached run 常见）
            state = AttemptState.FAILED.value
            payload["collect_error"] = str(exc)
            payload["exit_code_known"] = False

    succeeded = state == AttemptState.SUCCEEDED.value
    if succeeded and attempt.role == "verifier":
        # 8th-round P1 fix: this is the crash-recovery twin of the runner's
        # stamping.  A verifier attempt settled here carried no content
        # identity at all, and the old "both hashes missing means match"
        # rule made it unconditional pass-through evidence -- so a contract
        # whose verifier was recovered after a daemon restart could be
        # confirmed against an acceptance that had been edited since.
        #
        # 9th-round P1: the identity is taken from the revision stored on the
        # attempt row -- the acceptance this verifier was dispatched under --
        # not from whatever the contract says now.
        payload.update(attempt_evidence_binding(conn, cid, attempt.contract_revision))
    append_event(
        conn,
        contract_id=cid,
        attempt_id=attempt.attempt_id,
        event_type=EventType.ATTEMPT_SUCCEEDED if succeeded else EventType.ATTEMPT_FAILED,
        payload=payload,
        now=now,
        actor="daemon",
        goal_id=cid,
        contract_revision=attempt.contract_revision,
        role=attempt.role,
    )
    append_event(
        conn,
        contract_id=cid,
        attempt_id=attempt.attempt_id,
        event_type=EventType.RECONCILE_COLLECTED,
        payload={
            "settled_state": state,
            "external_run_id": handle.external_run_id,
            "reason": "external run confirmed terminated; attempt settled (§11.3 branch 2)",
        },
        now=now,
        actor="daemon",
        goal_id=cid,
        contract_revision=attempt.contract_revision,
        role="system",
    )
    set_attempt_state(
        conn,
        attempt_id=attempt.attempt_id,
        state=state,
        now=now,
        return_code=_as_int(payload.get("returncode")),
        error_class=payload.get("collect_error") or payload.get("error_class"),
    )
    if holds_lease and lease_generation is not None:
        with contextlib.suppress(LeaseFencedError):
            release_lease(
                conn,
                contract_id=cid,
                holder_attempt_id=attempt.attempt_id,
                lease_generation=lease_generation,
                now=now,
                actor="daemon",
            )
    rebuild_projection(root, cid, conn)
    _emit(emit, f"reconcile/collected:{cid}:{attempt.attempt_id}:{state}")
    return ReconcileOutcome(
        attempt_id=attempt.attempt_id,
        contract_id=cid,
        branch=ReconcileBranch.COLLECTED,
        detail=f"external run terminated; attempt settled as {state}",
        external_state=state,
    )


def _orphan(
    root: Path,
    conn: sqlite3.Connection,
    attempt: StoredAttempt,
    *,
    holds_lease: bool,
    lease_generation: int | None,
    now: datetime,
    emit: Callable[[str], None] | None,
    detail: str,
) -> ReconcileOutcome:
    """分支 3：状态未知 → 标记 orphaned，宽限期内代持租约阻止重复 spawn。"""
    cid = _resolve_contract_id(conn, attempt) or attempt.goal_id
    first_time = attempt.state != AttemptState.ORPHANED.value
    mark_attempt_orphaned(conn, attempt_id=attempt.attempt_id, now=now)
    # 代持：租约活着时 decide() 封顶 remind，分发路径不会另起会话（§7）
    renewed = (
        holds_lease
        and lease_generation is not None
        and _renew(conn, cid, attempt.attempt_id, lease_generation, now)
    )
    if first_time:
        append_event(
            conn,
            contract_id=cid,
            attempt_id=attempt.attempt_id,
            event_type=EventType.ATTEMPT_ORPHANED,
            payload={"reason": detail, "orphaned_at": now.isoformat()},
            now=now,
            actor="daemon",
            goal_id=cid,
            contract_revision=attempt.contract_revision,
            role=attempt.role,
        )
        append_event(
            conn,
            contract_id=cid,
            attempt_id=attempt.attempt_id,
            event_type=EventType.RECONCILE_ORPHAN_GRACED,
            payload={
                "reason": detail,
                "lease_held_for_grace": renewed,
                "note": "no respawn during recovery grace (§11.3 branch 3)",
            },
            now=now,
            actor="daemon",
            goal_id=cid,
            contract_revision=attempt.contract_revision,
            role="system",
        )
        rebuild_projection(root, cid, conn)
        _emit(emit, f"reconcile/orphan-graced:{cid}:{attempt.attempt_id}")
    return ReconcileOutcome(
        attempt_id=attempt.attempt_id,
        contract_id=cid,
        branch=ReconcileBranch.ORPHAN_GRACED,
        detail=f"{detail}; lease held for grace={renewed}",
        external_state=None,
    )


def _terminate_if_identity_confirmed(
    conn: sqlite3.Connection, attempt: StoredAttempt, now: datetime
) -> None:
    """宽限到期 fence 前，身份可证的外部进程 best-effort 终止。

    只对 RECOVERY_REATTACH 策略句柄动作：pid+start_time 身份比对通过
    （确认还是当年那个 run）才 terminate；比对失败或不可判定一律不动——
    绝不凭 pid 撞名杀无辜进程。结果如实落 handle 事件供审计。
    """
    handle = attempt_handle(attempt)
    if handle is None or not handle.is_recoverable():
        return
    identity = handle.process_identity
    pid = identity.get("pid")
    start_time = identity.get("start_time")
    if not isinstance(pid, int) or not isinstance(start_time, (int, float)):
        return
    from lhgp.adapters.processes import identity_matches, process_alive, terminate_pid

    if process_alive(pid) is not True:
        return  # 已退出或不可判定：不动
    if identity_matches(pid, float(start_time)) is not True:
        return  # pid 被复用：不是我们的 run，绝不碰
    if terminate_pid(pid):
        append_event(
            conn,
            contract_id=attempt.contract_id or attempt.goal_id,
            attempt_id=attempt.attempt_id,
            event_type=EventType.HANDLE_REGISTERED,
            payload={
                "action": "terminate_on_fence",
                "pid": pid,
                "reason": "orphan grace expired; confirmed-identity run terminated",
            },
            now=now,
            actor="daemon",
            goal_id=attempt.goal_id,
            contract_revision=attempt.contract_revision,
            role="system",
        )


def _sweep_orphan(
    root: Path,
    conn: sqlite3.Connection,
    attempt: StoredAttempt,
    lease: Any,
    holds_lease: bool,
    *,
    now: datetime,
    grace: timedelta,
    emit: Callable[[str], None] | None,
) -> ReconcileOutcome:
    """宽限维护：未到期继续代持；到期则 fence 并让位给重新派发（分支 4）。"""
    cid = _resolve_contract_id(conn, attempt) or attempt.goal_id
    if not holds_lease or lease is None:
        # 租约已被回收或转给新 holder：旧的这一条已经 fence 过了
        return ReconcileOutcome(
            attempt_id=attempt.attempt_id,
            contract_id=cid,
            branch=ReconcileBranch.SKIPPED,
            detail="orphan already fenced: lease released or moved to another holder",
            external_state=None,
        )

    orphaned_at = attempt.orphaned_at or now
    if now - orphaned_at < grace:
        renewed = _renew(conn, cid, attempt.attempt_id, lease.generation, now)
        return ReconcileOutcome(
            attempt_id=attempt.attempt_id,
            contract_id=cid,
            branch=ReconcileBranch.ORPHAN_GRACED,
            detail=f"within recovery grace ({grace}); lease held={renewed}",
            external_state=None,
        )

    # 宽限到期：fence 旧 generation（释放租约使旧写回 LEASE_FENCED），
    # 记录风险，重新派发交由 Dispatcher 下一轮决定（§8 边界）。
    # 审计进程-R3：fence 前对 reattach 策略句柄做身份确认下的 best-effort
    # 终止——「状态未知」的活进程与新 attempt 双写 workspace 是最坏结局。
    _terminate_if_identity_confirmed(conn, attempt, now)
    with contextlib.suppress(LeaseFencedError):
        release_lease(
            conn,
            contract_id=cid,
            holder_attempt_id=attempt.attempt_id,
            lease_generation=lease.generation,
            now=now,
            actor="daemon",
        )
    append_event(
        conn,
        contract_id=cid,
        attempt_id=attempt.attempt_id,
        event_type=EventType.RECONCILE_FENCED_REDISPATCHED,
        payload={
            "fenced_generation": lease.generation,
            "orphaned_at": orphaned_at.isoformat(),
            "grace": grace.total_seconds(),
            "risk": "external run state still unknown after grace; "
            "old generation fenced, redispatch left to dispatcher (§11.3 branch 4)",
        },
        now=now,
        actor="daemon",
        goal_id=cid,
        contract_revision=attempt.contract_revision,
        role="system",
    )
    rebuild_projection(root, cid, conn)
    _emit(emit, f"reconcile/fenced-redispatched:{cid}:{attempt.attempt_id}")
    return ReconcileOutcome(
        attempt_id=attempt.attempt_id,
        contract_id=cid,
        branch=ReconcileBranch.FENCED_REDISPATCHED,
        detail=f"grace {grace} expired with unknown external state; fenced gen {lease.generation}",
        external_state=None,
    )


def _renew(
    conn: sqlite3.Connection,
    contract_id: str,
    attempt_id: str,
    lease_generation: int,
    now: datetime,
) -> bool:
    """代持续约：宽限期内保住写权，让调度层不会另起会话。失败返回 False。"""
    timeout = _FALLBACK_LEASE_TIMEOUT
    contract = get_contract(conn, contract_id)
    if contract is not None:
        timeout = timedelta(minutes=contract.draft.budget.max_attempt_minutes)
    try:
        renew_lease(
            conn,
            contract_id=contract_id,
            holder_attempt_id=attempt_id,
            lease_generation=lease_generation,
            heartbeat_at=now,
            timeout=timeout,
            actor="daemon",
            role="system",
        )
    except Exception:
        return False
    return True


def _as_int(value: Any) -> int | None:
    """退出码取值：只认真正的 int，None/字符串一律当不可得（不猜 0）。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pid_from_identity(identity: dict[str, Any]) -> int | None:
    """从 process_identity 提取 pid：接受 int/float/str 数字形态。

    JSON 往返会把 pid 变成 float（17620.0）——这是序列化形态不是
    精度问题，如实转换；非数字（缺 pid/坏数据）返回 None。
    """
    raw = identity.get("pid")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        pid = int(raw)
        return pid if pid > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw)
    return None


def _emit(emit: Callable[[str], None] | None, message: str) -> None:
    if emit is not None:
        emit(message)


__all__ = [
    "DEFAULT_RECOVERY_GRACE",
    "ReconcileBranch",
    "ReconcileOutcome",
    "reconcile_attempts",
]
