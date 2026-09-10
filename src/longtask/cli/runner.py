"""执行桥接层：把调度簿记变成真实 attempt（DESIGN §3.4、§5.1、§10）。

职责边界：run_daemon_tick 只做调度簿记（§3.3 ticker 不执行任务）；
本模块的 AttemptRunner 负责 prepare 复验 + spawn 拉起 + 存活心跳续约 +
终态回收（attempt/succeeded|failed + 租约释放）。

适配器实例按 executor_id 缓存：跨轮 observe/collect 需要同一实例
（subprocess 适配器持有进程句柄，fake 适配器持有脚本状态）。
守护进程重启后丢失句柄的 attempt 由租约心跳超时回收兜底（§7）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from longtask.acceptance.checks import CheckSpec
from longtask.acceptance.evaluator import evaluate_check
from longtask.acceptance.verdict import merge_evidence, parse_verdict_block
from longtask.adapters.base import (
    AttemptInput,
    ExecutorAdapter,
    PrepareRefusedError,
)
from longtask.adapters.factory import build_adapter
from longtask.adapters.registry import ExecutorRegistry, RegistryEntry
from longtask.contracts.schema import (
    AttemptRole,
    AttemptState,
    BlockReason,
    ContractDraft,
    ContractState,
    ContractView,
)
from longtask.persistence.attempts import (
    list_reconcilable_attempts,
    mark_attempt_orphaned,
    register_attempt_handle,
    set_attempt_state,
)
from longtask.persistence.context import (
    CapacityRefusedError,
    compile_context_snapshot,
    handover_prompt_addendum,
)
from longtask.persistence.errors import StoreError
from longtask.persistence.events import EventType
from longtask.persistence.projections import (
    HANDOVER_FILE,
    contract_dir,
    rebuild_projection,
)
from longtask.persistence.schema import transaction
from longtask.persistence.store import (
    LeaseFencedError,
    acceptance_at_revision,
    acquire_lease,
    append_event,
    attempt_evidence_binding,
    get_contract,
    get_events,
    get_lease,
    release_lease,
    renew_lease,
    update_contract_state,
)
from longtask.promoter.killswitch import is_kill_switch_active
from longtask.promoter.records import _count_verifier_attempts

# 事件 payload 内 stdout/stderr 截断上限：审计够用，不撑爆 log.jsonl
OUTPUT_TAIL_CHARS = 2000
DEFAULT_DIRECTIVE_MAX_AGE_SECONDS = 3600


def _resolve_directive_max_age_seconds(
    override: int | None,
) -> int:
    """解析 directive TTL：调用方显式值 → env → 3600s 默认。

    与 :class:`AttemptRunner` 构造时读取同一 env 变量（``LHGP_DIRECTIVE_MAX_AGE_SECONDS``）
    保证构造期与 ``build_attempt_input`` 调用期取到一致的值——避免一边
    实例化时 env 改了、另一边取错。1 小时默认：长于一次长 retry、短到
    防止合同已推进时旧 directive 复活。
    """
    if override is not None:
        return int(override)
    import os

    env_value = os.environ.get("LHGP_DIRECTIVE_MAX_AGE_SECONDS")
    if env_value is not None and env_value.strip():
        return int(env_value)
    return DEFAULT_DIRECTIVE_MAX_AGE_SECONDS


def _tail_text(value: object) -> str:
    text = str(value) if value is not None else ""
    return text[-OUTPUT_TAIL_CHARS:]


def contract_workspace(draft: ContractDraft) -> str:
    """合同冻结区声明的 workspace_root；未声明返回空串（适配器按 launch.cwd 兜底或拒接）。"""
    file_effects = draft.hard_constraints.get("file_effects")
    if isinstance(file_effects, dict):
        root = file_effects.get("workspace_root")
        if isinstance(root, str) and root.strip():
            return root
    return ""


def _executor_prompt(contract: ContractView) -> str:
    """执行者 task_prompt 的冻结区摘要（SPEC §11.2 合同可见性）。

    模型被唤起时必须得知：目标、验收判据（做到什么算完成）、写权限边界
    （硬约束）、deadline。合同延续与被遵守的前提是干活的模型知道合同
    内容——不是只在库里存着。
    """
    draft = contract.draft
    sections = [
        f"# 合同 {contract.contract_id}（rev {contract.revision}）",
        "",
        "## objective（冻结区，只读）",
        draft.objective,
        "",
        "## acceptance.checks（验收判据：逐条核对，全部 pass 才算完成）",
        *(f"- {c}" for c in draft.acceptance.checks),
        f"- 验收标准：{draft.acceptance.standard}",
        "",
        f"## deadline_at（冻结区）\n{draft.deadline_at.isoformat()}",
    ]
    if draft.hard_constraints:
        constraints = json.dumps(draft.hard_constraints, ensure_ascii=False, indent=2)
        sections += ["", "## hard_constraints（写权限边界，冻结区，只读）", constraints]
    return "\n".join(sections)


def build_attempt_input(
    root: Path,
    conn: sqlite3.Connection,
    contract: ContractView,
    attempt_id: str,
    now: datetime,
    *,
    with_context: bool = True,
    agent_id: str | None = None,
    directive_max_age_seconds: int | None = None,
) -> tuple[AttemptInput, int, list[int]]:
    """构造 AttemptInput（DESIGN §11.6 字段表）。

    Returns ``(AttemptInput, consumed_max_event_id, consumed_directive_ids)``:
    - the second element is the max AGENT_MESSAGE event id actually
      inlined into the freshly built snapshot;
    - the third element is the explicit list of directive event_ids
      newly consumed by this snapshot. The caller passes both to
      :func:`mark_directives_consumed` after Popen succeeds so the
      per-contract directive cursor only advances when the executor
      was actually started (P1 review, 2026-09-08, 2nd round: previously
      the cursor was bumped at snapshot-build time, which lost
      directives when the spawn then failed). The list also feeds
      the per-agent dedup set + directive/acknowledged audit events
      (3rd-round review 2026-09-08).

    ``agent_id`` is the registry executor_id of the agent that
    will receive the snapshot.  Used to filter directed directives
    (those with ``to_agent`` set) so a message addressed to one
    agent does not leak into another's snapshot (A2A scoping).

    lease_generation 动态取当前租约：租约获取前作 prepare 探针（旧代次），
    租约获取后作 spawn 入参（attempt 实际持有的新代次，§5.1 不可变五元组）。

    with_context=True 时（§4.1 临时上下文，Developer Preview 最小闭环）：
    - task_prompt 追加交接摘要附言（跨 attempt 现场——修复「再派 attempt
      没有验收失败上下文」的缺口，见内部真实运行记录）；
    - 物化该 attempt 的 context/attempts/<id>/active.md + scratch.md，
      路径填 context_snapshot_path（适配器据此装配，context.required=true
      无快照即拒接，§9）。容量超限抛 CapacityRefusedError（fail-closed）。

    ``directive_max_age_seconds``（A2A delivery hardening 2026-09-08）：
    透传给 :func:`compile_context_snapshot`，丢弃快照生成时间早于
    ``now - max_age`` 的 directive 事件。默认 3600s（可由
    ``LHGP_DIRECTIVE_MAX_AGE_SECONDS`` env 覆盖）。当调用方持有
    :class:`AttemptRunner` 实例时应显式传入 ``runner._directive_max_age_seconds``，
    保证构造期与调用期取到一致值。
    """
    draft = contract.draft
    active_lease = get_lease(conn, contract.contract_id)
    context_snapshot_path: str | None = None
    consumed_max_event_id = 0
    consumed_directive_ids: list[int] = []
    # SPEC §11.2：被唤起的执行者必须能得知合同——task_prompt 带冻结区摘要
    # （验收条款是「做到什么算完成」的判据，硬约束是写权限边界）。只给
    # objective 等于让模型盲干：干完不知道按什么标准被验收。
    task_prompt = _executor_prompt(contract)
    if with_context:
        addendum = handover_prompt_addendum(root, contract.contract_id)
        if addendum:
            task_prompt = f"{task_prompt}\n\n{addendum}"
        try:
            (
                active_path,
                _scratch,
                consumed_max_event_id,
                consumed_directive_ids,
            ) = compile_context_snapshot(
                root,
                conn,
                contract,
                attempt_id,
                now,
                to_agent=agent_id,
                max_age_seconds=_resolve_directive_max_age_seconds(directive_max_age_seconds),
            )
            context_snapshot_path = str(active_path)
        except CapacityRefusedError:
            # 容量合同不满足：按 §4.1 拒绝启动 attempt，向上传播
            raise
    return (
        AttemptInput(
            attempt_id=attempt_id,
            contract_id=contract.contract_id,
            revision=contract.revision,
            lease_generation=active_lease.generation if active_lease else 0,
            role=AttemptRole.EXECUTOR,
            contract_snapshot=draft.to_dict(),
            handover_path=str(contract_dir(root, contract.contract_id) / HANDOVER_FILE),
            workspace_root=contract_workspace(draft),
            budget_remaining={
                "max_dispatches": draft.budget.max_dispatches,
                "max_escalations": draft.budget.max_escalations,
                "max_output_bytes": draft.budget.max_output_bytes,
            },
            task_prompt=task_prompt,
            context_snapshot_path=context_snapshot_path,
            agent_id=agent_id,
        ),
        consumed_max_event_id,
        consumed_directive_ids,
    )


class AttemptRunner:
    """attempt 生命周期驱动：start（拉起）与 poll（观察/续约/回收）。"""

    def __init__(
        self,
        root: Path,
        conn: sqlite3.Connection,
        registry: ExecutorRegistry,
        adapter_factory: Callable[[RegistryEntry], ExecutorAdapter | None] | None = None,
        emit: Callable[[str], None] | None = None,
        directive_max_age_seconds: int | None = None,
    ) -> None:
        self._root = root
        self._conn = conn
        self._registry = registry
        self._factory = adapter_factory if adapter_factory is not None else build_adapter
        self._emit: Callable[[str], None] = emit if emit is not None else (lambda _msg: None)
        self._adapters: dict[str, ExecutorAdapter] = {}
        self._running: dict[str, dict[str, Any]] = {}
        # TTL for directives injected into a fresh attempt's snapshot.
        # 1 hour default: long enough to survive a long retry, short
        # enough to keep stale directives from re-firing after the
        # contract has moved on. Override via the env
        # ``LHGP_DIRECTIVE_MAX_AGE_SECONDS`` or pass explicitly.
        self._directive_max_age_seconds = _resolve_directive_max_age_seconds(
            directive_max_age_seconds
        )
        self.spawned_count = 0
        self.finished_count = 0

    def replace_registry(self, registry: ExecutorRegistry) -> None:
        """每轮重载注册表后同步给 runner（适配器缓存不重建，保持句柄）。"""
        self._registry = registry

    def _adapter_for(self, executor_id: str) -> ExecutorAdapter | None:
        cached = self._adapters.get(executor_id)
        if cached is not None:
            return cached
        entry = self._registry.get(executor_id)
        if entry is None:
            return None
        adapter = self._factory(entry)
        if adapter is None:
            return None
        self._adapters[executor_id] = adapter
        return adapter

    def adapter_for(self, executor_id: str | None) -> ExecutorAdapter | None:
        """公开取适配器入口：reconcile 必须与执行桥接共用同一实例。

        共用实例是硬要求——reattach 把外部 run 重新绑进适配器内部表，
        若 reconcile 用另一个实例，绑定结果执行层看不见，等于没绑。
        """
        if executor_id is None:
            return None
        return self._adapter_for(executor_id)

    def is_tracking(self, attempt_id: str) -> bool:
        """本进程是否仍持有该 attempt 的活句柄（reconcile 据此让路）。"""
        return attempt_id in self._running

    def is_idle(self) -> bool:
        """本进程无活 attempt（主循环据此决定能否睡到下一个决策点）。"""
        return not self._running

    def running_attempts(self) -> tuple[tuple[str, str], ...]:
        """返回当前进程持有的 (contract_id, attempt_id) 快照。"""
        return tuple(
            (str(info["contract_id"]), attempt_id) for attempt_id, info in self._running.items()
        )

    def adopt_reconciled_attempts(self) -> int:
        """把 reconcile 已重绑的 running attempts 纳入本 Runner 的观察表。

        ``reconcile_attempts`` 负责身份校验和租约续期，但不依赖执行层，
        因而不会直接修改 Runner 的内存进程表。daemon 重启后若不在此处
        接管，新的 Runner 只能反复 reconcile，无法 poll/collect/cancel
        该外部 run。仅接纳当前租约 holder 且 adapter 已能 observe 为 running
        的行，避免把未知或已被 fencing 的 attempt 误纳入。
        """
        adopted = 0
        for attempt in list_reconcilable_attempts(self._conn):
            if attempt.attempt_id in self._running or attempt.state != AttemptState.RUNNING.value:
                continue
            contract_id = attempt.contract_id
            if not contract_id or not attempt.executor_id:
                continue
            lease = get_lease(self._conn, contract_id)
            if lease is None or lease.holder_attempt_id != attempt.attempt_id:
                continue
            adapter = self._adapter_for(attempt.executor_id)
            if adapter is None:
                continue
            try:
                observation = adapter.observe(attempt.attempt_id)
            except (KeyError, OSError):
                continue
            if str(observation.get("state")) != AttemptState.RUNNING.value:
                continue
            self._running[attempt.attempt_id] = {
                "contract_id": contract_id,
                "executor_id": attempt.executor_id,
                "model": attempt.model_id or "*",
                "role": attempt.role,
                "contract_revision": attempt.contract_revision,
                "session_ref": attempt.session_locator or "",
                "generation": lease.generation,
            }
            adopted += 1
        return adopted

    def _persist_handle(
        self,
        adapter: ExecutorAdapter,
        contract: ContractView,
        attempt_id: str,
        now: datetime,
    ) -> None:
        """spawn 成功立刻持久化外部句柄（§11.3 MUST 持久返回）。

        句柄不落库，守护进程重启后就无法确认外部 run 死活，只能一律当作
        状态未知——那等于把「跨重启连续性」交给了运气。这是内存 Popen 的
        根本缺陷，必须在 spawn 后立刻补上。
        """
        handle = adapter.run_handle(attempt_id)
        if handle is None:
            # 适配器拿不出句柄：如实记账，不伪造一个可恢复的假象
            self._emit(f"runner/handle-unavailable:{contract.contract_id}:{attempt_id}")
            return
        register_attempt_handle(
            self._conn,
            attempt_id=attempt_id,
            external_run_id=handle.external_run_id,
            session_locator=handle.session_locator,
            recovery_strategy=handle.recovery_strategy,
            process_identity=handle.process_identity,
            capability_snapshot=handle.capability_snapshot,
            now=now,
        )
        set_attempt_state(
            self._conn,
            attempt_id=attempt_id,
            state=AttemptState.RUNNING.value,
            now=now,
        )
        append_event(
            self._conn,
            contract_id=contract.contract_id,
            attempt_id=attempt_id,
            event_type=EventType.HANDLE_REGISTERED,
            payload=handle.to_dict(),
            now=now,
            actor="daemon",
            goal_id=contract.goal_id,
            contract_revision=contract.revision,
            role="executor",
        )

    def start_attempt(
        self,
        now: datetime,
        *,
        contract_id: str,
        attempt_id: str,
        executor_id: str,
        model: str = "*",
    ) -> bool:
        """prepare 复验 + spawn 拉起；任一步失败记 attempt/failed 并释放租约。"""
        adapter = self._adapter_for(executor_id)
        contract = get_contract(self._conn, contract_id)
        if adapter is None or contract is None:
            self._fail_attempt(
                now, contract_id, attempt_id, f"executor or contract unavailable: {executor_id}"
            )
            return False
        try:
            input_, consumed_max_event_id, consumed_directive_ids = build_attempt_input(
                self._root,
                self._conn,
                contract,
                attempt_id,
                now,
                agent_id=executor_id,
                directive_max_age_seconds=self._directive_max_age_seconds,
            )
            # Per-attempt session token（安全加固）：spawn 前生成一次性凭据
            import hashlib
            import secrets

            _raw_token = secrets.token_urlsafe(32)
            _token_hash = hashlib.sha256(_raw_token.encode()).hexdigest()
            self._conn.execute(
                "UPDATE attempts SET session_token_hash = ? WHERE attempt_id = ?",
                (_token_hash, attempt_id),
            )
            self._conn.commit()
            input_ = dataclasses.replace(input_, session_token=_raw_token)
        except CapacityRefusedError as exc:
            # §4.1 容量合同不满足：拒绝启动 attempt（事件已由编译器记
            # context/capacity-refused，这里补记账并释放租约）
            self._fail_attempt(now, contract_id, attempt_id, f"context capacity refused: {exc}")
            return False
        try:
            launch = adapter.prepare(input_)
            session_ref = adapter.spawn(input_, launch)
        except PrepareRefusedError as exc:
            self._fail_attempt(now, contract_id, attempt_id, f"prepare refused: {exc}")
            return False
        except OSError as exc:
            self._fail_attempt(now, contract_id, attempt_id, f"spawn failed: {exc}")
            return False
        # P1 review (2026-09-08, 2nd round): cursor advance deferred
        # to *after* the spawn has actually launched a subprocess.
        # Previously the cursor was bumped in compile_context_snapshot,
        # so a spawn failure (executable missing, argv invalid, …)
        # silently lost the directives the failed snapshot had
        # inlined — the next attempt's snapshot would see the
        # advanced cursor and skip them.  See mark_directives_consumed.
        #
        # A2A scoping (3rd-round review): pass to_agent so the
        # per-agent cursor moves, not the contract-level broadcast
        # one.  This way, a directive addressed to B does not burn
        # through A's cursor.
        #
        # A2A delivery hardening (3rd-round review 2026-09-08):
        # pass consumed_directive_ids + now so the per-agent dedup
        # set is updated and a directive/acknowledged event is
        # written for each newly consumed directive.
        from longtask.persistence.context import mark_directives_consumed

        mark_directives_consumed(
            self._conn,
            contract_id,
            consumed_max_event_id,
            to_agent=executor_id,
            consumed_ids=consumed_directive_ids,
            now=now,
        )
        self._persist_handle(adapter, contract, attempt_id, now)
        lease = get_lease(self._conn, contract_id)
        self._running[attempt_id] = {
            "contract_id": contract_id,
            "executor_id": executor_id,
            "model": model,
            "role": AttemptRole.EXECUTOR.value,
            "contract_revision": contract.revision,
            "session_ref": session_ref,
            "generation": lease.generation if lease else None,
        }
        self.spawned_count += 1
        self._emit(f"runner/spawned:{contract_id}:{attempt_id}:{session_ref}")
        return True

    def poll_attempts(self, now: datetime) -> None:
        """观察运行中 attempt：存活者续约心跳，换代/丢失者记 stale，收尾者回收。"""
        for attempt_id, info in list(self._running.items()):
            contract_id = str(info["contract_id"])
            adapter = self._adapters.get(str(info["executor_id"]))
            if adapter is None:
                self._mark_stale(now, attempt_id, info, "executor no longer available")
                continue
            contract = get_contract(self._conn, contract_id)
            if contract is not None:
                started_row = self._conn.execute(
                    "SELECT started_at FROM attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
                started_at = (
                    datetime.fromisoformat(str(started_row[0]))
                    if started_row and started_row[0]
                    else None
                )
                timeout = timedelta(minutes=contract.draft.budget.max_attempt_minutes)
                if started_at is not None and now - started_at >= timeout:
                    cancel_error: Exception | None = None
                    try:
                        adapter.cancel(attempt_id, "attempt timeout exceeded")
                    except Exception as exc:
                        cancel_error = exc
                    if cancel_error is not None:
                        # 取消失败时外部进程的状态未知：保留 attempt 和租约，
                        # 交给 reconcile 的 orphan grace/fencing 路径处理，
                        # 绝不能把“请求取消”当作“进程已退出”。
                        mark_attempt_orphaned(
                            self._conn,
                            attempt_id=attempt_id,
                            now=now,
                        )
                        append_event(
                            self._conn,
                            contract_id=contract_id,
                            attempt_id=attempt_id,
                            event_type=EventType.ATTEMPT_ORPHANED,
                            payload={
                                "reason": f"timeout cancellation failed: {cancel_error}",
                                "timeout_seconds": timeout.total_seconds(),
                            },
                            now=now,
                            actor="daemon",
                            goal_id=contract.goal_id,
                            contract_revision=contract.revision,
                            role=str(info.get("role", AttemptRole.EXECUTOR.value)),
                        )
                        rebuild_projection(self._root, contract_id, self._conn)
                        self._running.pop(attempt_id, None)
                        self._emit(f"runner/attempt-orphaned:{contract_id}:{attempt_id}")
                        continue
                    self._fail_attempt(
                        now,
                        contract_id,
                        attempt_id,
                        f"attempt timeout exceeded ({timeout.total_seconds() / 60:g}m)",
                        error_class="attempt-timeout",
                    )
                    continue
            try:
                observation = adapter.observe(attempt_id)
            except KeyError:
                self._mark_stale(now, attempt_id, info, "adapter lost the attempt")
                continue

            lease = get_lease(self._conn, contract_id)
            generation = lease.generation if lease else None
            if generation is not None and generation != info["generation"]:
                # 租约被另一 attempt 接管：本 attempt 不再持有写权，停追（fencing §7）。
                # generation is None 表示租约已被释放（attempt 已正常收尾）——继续走
                # finish 路径让 collect 落 attempt/succeeded|failed，而不是误判 stale。
                self._mark_stale(now, attempt_id, info, "lease moved to another generation")
                continue

            if str(observation.get("state")) == AttemptState.RUNNING.value:
                # spawn 后短窗内子进程可能尚未退出（observe 只是瞬时快照）。
                # 存活且持有当前租约：代发心跳续约；下一轮 poll 再看是否收尾。
                # 安全审查 进程-C2：租约中途消失/换代时 renew_lease 会抛
                # LeaseFencedError、info["generation"] 为 None 时 int() 会抛
                # TypeError——任何一条都会击穿 poll_attempts 顶层，杀死整个
                # daemon（其余合同调度全部停摆）。fenced/stale 是 attempt 级
                # 事件，绝不允许升级为进程级崩溃。
                lease_gen: int | None = None
                if generation is not None:
                    lease_gen = generation
                elif isinstance(info.get("generation"), int):
                    lease_gen = int(info["generation"])
                if lease_gen is not None and contract is not None:
                    try:
                        renew_lease(
                            self._conn,
                            contract_id=contract_id,
                            holder_attempt_id=attempt_id,
                            lease_generation=lease_gen,
                            heartbeat_at=now,
                            timeout=timedelta(minutes=contract.draft.budget.max_attempt_minutes),
                            actor="daemon",
                        )
                    except LeaseFencedError:
                        # 租约已被释放或被新持有者 fence：本 attempt 失去
                        # 写权，按 stale 收尾，下一轮由 reconcile 接管。
                        self._mark_stale(now, attempt_id, info, "lease fenced during renew")
                        continue
                continue

            self._finish_attempt(now, attempt_id, info, adapter, str(observation.get("state")))

    def _finish_attempt(
        self,
        now: datetime,
        attempt_id: str,
        info: dict[str, Any],
        adapter: ExecutorAdapter,
        state: str,
    ) -> None:
        """终态回收：collect 结果落事件（截断），释放租约，停止跟踪。"""
        contract_id = str(info["contract_id"])
        role = str(info.get("role", AttemptRole.EXECUTOR.value))
        payload: dict[str, Any] = {
            "session_ref": info["session_ref"],
            "state": state,
            "role": role,
            "model": str(info.get("model", "*")),
        }
        try:
            collected = adapter.collect(attempt_id)
            payload["returncode"] = collected.get("returncode")
            full_stdout = str(collected.get("stdout") or "")
            payload["stdout_tail"] = _tail_text(collected.get("stdout"))
            payload["stderr_tail"] = _tail_text(collected.get("stderr"))
            # SPEC §12.4 通道 2：一次性 CLI verifier 无法调 write-back RPC，
            # 约定在 stdout 末尾写 lhgp-verdict 判定块；无块/非法 → None
            # （不猜、不静默兜底）。
            model_verdict = (
                parse_verdict_block(full_stdout) if role == AttemptRole.VERIFIER.value else None
            )
            payload["model_verdict"] = (
                {"verdict": model_verdict.verdict, "checks": list(model_verdict.checks)}
                if model_verdict is not None
                else None
            )
            # verifier 必须通过结构化 write-back 或 stdout 判定块提供验收
            # 结果；仅凭进程退出码无法证明 acceptance.checks 已被核验。
            verifier_written_back = any(
                event.attempt_id == attempt_id
                and event.event_type in (EventType.ATTEMPT_SUCCEEDED, EventType.ATTEMPT_FAILED)
                and (
                    (event.payload_json or "").find('"role": "verifier"') >= 0
                    or (event.payload_json or "").find('"checks"') >= 0
                )
                for event in get_events(self._conn, contract_id=contract_id)
            )
            if (
                role == AttemptRole.VERIFIER.value
                and not collected.get("finished_by_event")
                and not verifier_written_back
                and model_verdict is None
            ):
                payload["state"] = AttemptState.FAILED.value
                payload["error_class"] = "verification-evidence-missing"
                payload["reason"] = "verifier exited without structured acceptance evidence"
            if role == AttemptRole.VERIFIER.value:
                contract = get_contract(self._conn, contract_id)
                # 6th-round P1 fix: stamp the acceptance
                # spec_hash into the verifier event payload
                # so the user_confirm path can reject
                # evidence from a prior acceptance version
                # (a content change invalidates the prior
                # verifier pass — the lifecycle-only
                # revision bump alone does not).  Without
                # this, the unconditional verifier event
                # migration in update_contract_state lets
                # a stale verifier pass re-fire on the
                # current revision and the contract can
                # land in COMPLETE without a fresh check.
                #
                # 9th-round P1 fix: that stamp read the *live* contract row at
                # collect time, so patching the acceptance while this verifier
                # was running retroactively labelled old evidence with the new
                # requirement's fingerprint -- and user_confirm then matched it.
                # The identity now comes from the revision this attempt was
                # admitted at, which is what the verifier was actually given.
                attempt_revision = info.get("contract_revision")
                checked = (
                    acceptance_at_revision(self._conn, contract_id, attempt_revision)
                    if attempt_revision is not None
                    else None
                )
                payload.update(attempt_evidence_binding(self._conn, contract_id, attempt_revision))
                # Evidence must describe the acceptance the attempt ran under.
                # Fall back to the live row only when no snapshot resolves --
                # the binding stays empty in that case, so the event is refused
                # downstream rather than trusted with an identity it cannot
                # prove.
                binding_source = checked or (
                    contract.draft.acceptance if contract is not None else None
                )
                typed_checks = (
                    [check for check in binding_source.checks if isinstance(check, CheckSpec)]
                    if binding_source is not None
                    else []
                )
                if typed_checks and contract is not None:
                    workspace = contract_workspace(contract.draft)
                    if workspace:
                        # SPEC §12.4 裁决合成：确定性评估优先；协议
                        # undetermined 时模型显式 pass/fail 填补；冲突记录
                        # model_outcome 供审计。
                        deadline_budget = max(
                            0.1, (contract.draft.deadline_at - now).total_seconds()
                        )
                        results = [
                            evaluate_check(
                                check,
                                workspace_root=Path(workspace),
                                timeout_seconds=deadline_budget,
                            )
                            for check in typed_checks
                        ]
                        payload["evidence"] = [
                            merge_evidence(result.to_evidence(), model_verdict)
                            for result in results
                        ]
                        mandatory_failed = any(
                            str(entry["outcome"]) != "pass"
                            for check, entry in zip(typed_checks, payload["evidence"], strict=True)
                            if check.mandatory
                        )
                        payload["state"] = (
                            AttemptState.FAILED.value
                            if mandatory_failed
                            else AttemptState.SUCCEEDED.value
                        )
        except Exception as exc:  # 回收失败也要如实收尾，不悬挂租约
            payload["state"] = AttemptState.FAILED.value
            payload["collect_error"] = str(exc)

        succeeded = payload["state"] == AttemptState.SUCCEEDED.value
        # 事件、attempt 行、租约和投影必须在同一数据库事务中收口。
        # collect 在事务外执行，避免长时间占用 SQLite 写锁。
        with transaction(self._conn):
            append_event(
                self._conn,
                contract_id=contract_id,
                attempt_id=attempt_id,
                event_type=EventType.ATTEMPT_SUCCEEDED if succeeded else EventType.ATTEMPT_FAILED,
                payload=payload,
                now=now,
                actor="daemon",
                role=role,
                contract_revision=info.get("contract_revision"),
            )
            # P1：更新 attempts 行状态（DESIGN §7 attempt 轴）
            self._conn.execute(
                """
            UPDATE attempts
            SET state = ?, terminal_at = ?, updated_at = ?,
                return_code = ?, error_class = ?, payload_json = ?
            WHERE attempt_id = ?
            """,
                (
                    payload["state"],
                    now.isoformat(),
                    now.isoformat(),
                    payload.get("returncode"),
                    payload.get("collect_error"),
                    json.dumps(payload, ensure_ascii=False),
                    attempt_id,
                ),
            )
            self._release_lease_if_held(now, contract_id, attempt_id)
            rebuild_projection(self._root, contract_id, self._conn)
        executor_id = str(info["executor_id"])
        del self._running[attempt_id]
        self.finished_count += 1
        self._emit(f"runner/attempt-{payload['state']}:{contract_id}:{attempt_id}")
        if succeeded and role == "executor":
            # 只有执行者成功才派生交叉 verifier；verifier 自身成功不能
            # 再递归派生 verifier，否则真实多执行器注册表会形成无限验证链。
            self._dispatch_verifier(now, contract_id=contract_id, executor_id=executor_id)

    def _fail_attempt(
        self,
        now: datetime,
        contract_id: str,
        attempt_id: str,
        reason: str,
        *,
        error_class: str = "attempt-failed",
    ) -> None:
        append_event(
            self._conn,
            contract_id=contract_id,
            attempt_id=attempt_id,
            event_type=EventType.ATTEMPT_FAILED,
            payload={"reason": reason},
            now=now,
            actor="daemon",
        )
        # P1：更新 attempts 行
        self._conn.execute(
            """
            UPDATE attempts
            SET state = 'failed',
                terminal_at = ?,
                error_class = ?,
                updated_at = ?
            WHERE attempt_id = ?
            """,
            (now.isoformat(), error_class, now.isoformat(), attempt_id),
        )
        self._release_lease_if_held(now, contract_id, attempt_id)
        rebuild_projection(self._root, contract_id, self._conn)
        self._running.pop(attempt_id, None)
        self.finished_count += 1
        self._emit(f"runner/attempt-failed:{contract_id}:{attempt_id}")

    def _mark_stale(
        self, now: datetime, attempt_id: str, info: dict[str, Any], reason: str
    ) -> None:
        append_event(
            self._conn,
            contract_id=str(info["contract_id"]),
            attempt_id=attempt_id,
            event_type=EventType.ATTEMPT_STALE,
            payload={"reason": reason, "session_ref": info["session_ref"]},
            now=now,
            actor="daemon",
        )
        # 审计进程-R1：stale 是终态，遗留的租约会让 decide() 封顶 REMIND、
        # workspace 排他把死租约当占用者——合同空转最长一个 attempt 超时。
        # 释放失败（已换代）不影响终态记账。
        self._release_lease_if_held(now, str(info["contract_id"]), attempt_id)
        # P1：更新 attempts 行
        self._conn.execute(
            """
            UPDATE attempts
            SET state = 'stale', terminal_at = ?, updated_at = ?
            WHERE attempt_id = ?
            """,
            (now.isoformat(), now.isoformat(), attempt_id),
        )
        rebuild_projection(self._root, str(info["contract_id"]), self._conn)
        del self._running[attempt_id]
        self._emit(f"runner/attempt-stale:{info['contract_id']}:{attempt_id}")

    def _release_lease_if_held(self, now: datetime, contract_id: str, attempt_id: str) -> None:
        lease = get_lease(self._conn, contract_id)
        if lease is None or lease.holder_attempt_id != attempt_id:
            return
        # 已被回收/接管时 release_lease 抛 LeaseFencedError：fencing 生效，不重复释放
        with contextlib.suppress(LeaseFencedError):
            release_lease(
                self._conn,
                contract_id=contract_id,
                holder_attempt_id=attempt_id,
                lease_generation=lease.generation,
                now=now,
                actor="daemon",
            )

    def cancel_attempt(
        self,
        now: datetime,
        *,
        contract_id: str,
        attempt_id: str,
        reason: str,
        actor: str = "user",
    ) -> bool:
        """control/interrupt：打断执行中的 attempt（DESIGN §10 用户可干涉）。

        adapter.cancel 尽力打断（subprocess terminate→kill 宽限；fake 剧本化）；
        无论 adapter 是否成功取消都记 attempt/cancelled + 释放租约 + 停追，
        不悬挂租约。返回是否命中正在运行的 attempt。
        """
        info = self._running.get(attempt_id)
        if info is None or str(info.get("contract_id")) != contract_id:
            return False
        adapter = self._adapters.get(str(info["executor_id"]))
        if adapter is not None:
            try:
                adapter.cancel(attempt_id, reason)
            except Exception as exc:  # 取消失败也要如实收尾，不悬挂租约
                self._emit(f"runner/cancel-error:{contract_id}:{attempt_id}:{exc}")
        append_event(
            self._conn,
            contract_id=contract_id,
            attempt_id=attempt_id,
            event_type=EventType.ATTEMPT_CANCELLED,
            payload={"reason": reason},
            now=now,
            actor=actor,
        )
        set_attempt_state(
            self._conn,
            attempt_id=attempt_id,
            state=AttemptState.CANCELLED.value,
            now=now,
            error_class="cancelled-by-user",
        )
        self._release_lease_if_held(now, contract_id, attempt_id)
        rebuild_projection(self._root, contract_id, self._conn)
        self._running.pop(attempt_id, None)
        self._emit(f"runner/attempt-cancelled:{contract_id}:{attempt_id}")
        return True

    def _dispatch_verifier(self, now: datetime, *, contract_id: str, executor_id: str) -> bool:
        """执行者 succeeded 后派生 verifier（DESIGN §5.2 交叉核对）。

        候选必须 ≠ 执行者（防止同源盲区）；复用 start_attempt 的
        prepare+spawn 链，仅 role 切到 VERIFIER。verifier 自己核对
        acceptance.checks 后写回 attempt/succeeded|failed，由上层
        据此转 complete 或退回 active。
        """

        # Kill switch 激活时不得派生任何外部进程（安全审查 调度-C2）：
        # tick 有拦截，但本方法还有 daemon_loop 的两条旁路调用。
        if is_kill_switch_active(self._root):
            return False

        contract = get_contract(self._conn, contract_id)
        if contract is None or contract.state != ContractState.ACTIVE:
            return False
        # 不抢活租约（安全审查 调度-C1）：verifier 派发曾直接 CAS 覆盖
        # 在跑 executor 的租约，导致 executor 被误判 stale、外部进程
        # 失管、同 workspace 出现双写者。租约仍存活且持有者是非终态
        # attempt 时推迟派生（记 verification/deferred，下轮重试）。
        active_lease = get_lease(self._conn, contract_id)
        if active_lease is not None and active_lease.is_alive(now):
            holder_state = self._conn.execute(
                "SELECT state FROM attempts WHERE attempt_id = ? LIMIT 1",
                (active_lease.holder_attempt_id,),
            ).fetchone()
            holder_terminal = holder_state is not None and holder_state[0] in (
                "succeeded",
                "failed",
                "cancelled",
                "stale",
                "orphaned",
            )
            if not holder_terminal:
                append_event(
                    self._conn,
                    contract_id=contract_id,
                    event_type=EventType.DISPATCH_DEFERRED,
                    payload={
                        "reason": (
                            "verifier dispatch deferred: live lease held by "
                            f"running attempt {active_lease.holder_attempt_id}"
                        ),
                        "lease_generation": active_lease.generation,
                    },
                    now=now,
                    actor="daemon",
                )
                return False
        # C1 修复（P1）：用 attempts 实体表的 role='verifier' 判定已派生，
        # 不再用 payload_json 字符串匹配（会误判子串）。
        # SPEC §12.3 明确「历史 verifier 不得阻止新的 verifier 派生」——
        # 只有当存在非 terminal 的 verifier attempt 时才视为正在派生，
        # 避免与同 attempt_id 上后续轮次冲突。
        # 审计调度-R5：在途判定与预算同作用域（contract_id）。曾按 goal_id
        # 圈定——同 Goal 兄弟合同的 verifier 会让本合同的验收无限期延后。
        existing_verifier = self._conn.execute(
            """
            SELECT state FROM attempts
            WHERE contract_id = ? AND role = 'verifier'
              AND state NOT IN ('succeeded', 'failed', 'cancelled', 'stale', 'orphaned')
            ORDER BY admitted_at DESC LIMIT 1
            """,
            (contract.contract_id,),
        ).fetchone()
        if existing_verifier is not None:
            return False
        # P5 验证预算独立记账（§12.4）：verifier 派发消耗
        # verification_attempts_reserved 而非 max_dispatches——否则一轮
        # 验证就能吃光执行预算，repair 闭环直接饿死。计数源是 attempts
        # 表 role='verifier' 的行（可审计），默认保留两次验证机会。
        reserved = contract.draft.budget.verification_attempts_reserved
        used = _count_verifier_attempts(self._conn, contract_id)
        if used >= reserved:
            append_event(
                self._conn,
                contract_id=contract_id,
                event_type=EventType.ESCALATION_HANDED_TO_USER,
                payload={
                    "reason": (
                        f"verification budget exhausted: {used}/{reserved} "
                        "verifier attempts used (§12.4): user arbitration needed"
                    ),
                },
                now=now,
                actor="daemon",
            )
            # 防破坏性跟进（审查 调度-R4）：只落事件不落状态时，daemon
            # 下一轮会把不可验收的合同再派一轮 executor，白白烧光
            # dispatch 预算。转 blocked(need-user) 交给用户裁决。
            if contract.state == ContractState.ACTIVE:
                with contextlib.suppress(StoreError):
                    update_contract_state(
                        self._conn,
                        contract_id=contract_id,
                        new_state=ContractState.BLOCKED,
                        now=now,
                        blocked_reason=BlockReason.NEED_USER,
                        actor="daemon",
                    )
                    append_event(
                        self._conn,
                        contract_id=contract_id,
                        event_type=EventType.CONTRACT_BLOCKED,
                        payload={
                            "reason": "verification budget exhausted (§12.4)",
                            "blocked_reason": BlockReason.NEED_USER.value,
                        },
                        now=now,
                        actor="daemon",
                        goal_id=contract.goal_id,
                        contract_revision=contract.revision,
                    )
            return False
        # 选择候选：排除执行者本身，按执行器匹配规则排序；
        # requested_role='verifier' → 合同 authority 设了绑定时，
        # roles 不含 verifier 的执行器不入候选（§6.3 条件 2）
        # P1 review fix (2026-09-08): inject running_attempts so the
        # verifier pool also respects each executor's
        # max_concurrent_attempts; otherwise verifier dispatches could
        # saturate an executor that's still running the executor attempt.
        from longtask.persistence.attempts import count_running_by_executor

        running_attempts = count_running_by_executor(self._conn)
        candidates = self._registry.match_candidates(
            contract.draft,
            running_attempts=running_attempts,
            requested_role="verifier",
        )
        verifier_entry: RegistryEntry | None = None
        for entry in candidates:
            if entry.id == executor_id:
                continue
            verifier_entry = entry
            break
        if verifier_entry is None:
            # 池中无独立候选：如实记事件，不静默假装
            append_event(
                self._conn,
                contract_id=contract_id,
                event_type=EventType.ESCALATION_HANDED_TO_USER,
                payload={"reason": "no independent verifier candidate in registry (§5.2)"},
                now=now,
                actor="daemon",
            )
            return False

        # verifier attempt id：秒级时间戳只是可读前缀，必须再查库补序号。
        # 同一秒内的 repair/reverify 不得复用 attempt_id，否则适配器会把
        # 新 verifier 误判为旧进程的重复 spawn（也会破坏 request/事件追溯）。
        verifier_prefix = f"ver-{now.strftime('%Y%m%d%H%M%S')}-{contract_id[-4:]}"
        verifier_id = verifier_prefix
        sequence = 1
        while self._conn.execute(
            "SELECT 1 FROM attempts WHERE attempt_id = ? LIMIT 1", (verifier_id,)
        ).fetchone():
            verifier_id = f"{verifier_prefix}-{sequence}"
            sequence += 1
        adapter = self._adapter_for(verifier_entry.id)
        if adapter is None:
            self._fail_attempt(
                now,
                contract_id,
                verifier_id,
                f"verifier adapter unavailable: {verifier_entry.id}",
            )
            return False
        input_ = self._build_verifier_input(contract, verifier_id, now)
        try:
            launch = adapter.prepare(input_)
            session_ref = adapter.spawn(input_, launch)
        except PrepareRefusedError as exc:
            self._fail_attempt(now, contract_id, verifier_id, f"verifier prepare refused: {exc}")
            return False
        except OSError as exc:
            self._fail_attempt(now, contract_id, verifier_id, f"verifier spawn failed: {exc}")
            return False

        # verifier 占租约（fencing：与执行者不同 attempt_id 不同代次）
        active_lease = get_lease(self._conn, contract_id)
        expected_gen = active_lease.generation if active_lease else 0
        acquire_lease(
            self._conn,
            contract_id=contract_id,
            holder_attempt_id=verifier_id,
            expected_generation=expected_gen,
            heartbeat_at=now,
            timeout=timedelta(minutes=contract.draft.budget.max_attempt_minutes),
            actor="daemon",
            payload={"executor_id": verifier_entry.id, "urgency_tier": 3},
            role="verifier",
            contract_revision=contract.revision,
        )
        append_event(
            self._conn,
            contract_id=contract_id,
            attempt_id=verifier_id,
            event_type=EventType.ATTEMPT_STARTED,
            payload={
                "executor_id": verifier_entry.id,
                "role": "verifier",
                "verifier_for": executor_id,
                "contract_revision": contract.revision,
            },
            now=now,
            actor="daemon",
            goal_id=contract.goal_id,
            contract_revision=contract.revision,
            role="verifier",
        )
        # P1：写入 attempts 行（DESIGN §7 attempt 轴）
        self._conn.execute(
            """
            INSERT INTO attempts (
                attempt_id, goal_id, contract_id, contract_revision, role,
                executor_id, model_id, state, lease_generation, partition_id,
                admitted_at, started_at, terminal_at, return_code, error_class,
                payload_json, updated_at
            ) VALUES (?, ?, ?, ?, 'verifier', ?, ?, 'admitted', NULL, NULL, ?,
                      NULL, NULL, NULL, NULL, ?, ?)
            ON CONFLICT (attempt_id) DO UPDATE SET
                state = excluded.state,
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                verifier_id,
                contract.goal_id,
                contract.contract_id,
                contract.revision,
                verifier_entry.id,
                next((m for m in verifier_entry.models if m != "*"), "*"),
                now.isoformat(),
                json.dumps({"verifier_for": executor_id, "executor_id": verifier_entry.id}),
                now.isoformat(),
            ),
        )
        self._persist_handle(adapter, contract, verifier_id, now)
        lease = get_lease(self._conn, contract_id)
        self._running[verifier_id] = {
            "contract_id": contract_id,
            "executor_id": verifier_entry.id,
            "role": AttemptRole.VERIFIER.value,
            "contract_revision": contract.revision,
            "model": next((m for m in verifier_entry.models if m != "*"), "*"),
            "session_ref": session_ref,
            "generation": lease.generation if lease else None,
        }
        self.spawned_count += 1
        self._emit(f"runner/verifier-spawned:{contract_id}:{verifier_id}:{session_ref}")
        return True

    def _build_verifier_input(
        self, contract: ContractView, attempt_id: str, now: datetime
    ) -> AttemptInput:
        """verifier attempt 的 AttemptInput：role=verifier，task_prompt 带 checks。

        context_snapshot_path 通过 build_attempt_input 获得（含交接上下文——§5.2
        verifier 应能从交接文件读到执行者上轮的产出与失败原因）。
        """
        from longtask.persistence.context import handover_prompt_addendum

        base, _consumed, _consumed_ids = build_attempt_input(
            self._root,
            self._conn,
            contract,
            attempt_id,
            now,
            directive_max_age_seconds=self._directive_max_age_seconds,
        )
        checks_lines = []
        for c in contract.draft.acceptance.checks:
            if isinstance(c, CheckSpec):
                checks_lines.append(f"- {c.kind.value}:{c.target}")
            else:
                checks_lines.append(f"- {c}")
        checks_text = "\n".join(checks_lines)
        # SPEC §12.4 通道 2：一次性 CLI verifier 无法调 attempt/write-back
        # RPC，约定在 stdout 末尾写机器可读判定块，运行时解析合成裁决。
        verifier_prompt = (
            "你是 verifier（DESIGN §5.2）：独立核对以下验收条款。\n"
            "逐条真实核验（可读工作区文件、可运行验证命令），然后在输出末尾"
            "写一个判定块（ fenced code block，语言标记 lhgp-verdict），"
            "格式如下（check_id 用下面列出的原样标识，outcome 取 "
            "pass/fail/undetermined，source 写你核验依据的文件或命令）：\n"
            "```lhgp-verdict\n"
            '{"verdict": "succeeded", "checks": ['
            '{"check_id": "file-exists:x.py", "outcome": "pass", "source": "ws/x.py"}'
            "]}\n"
            "```\n"
            "全部 mandatory checks 都 pass 时 verdict 才是 succeeded，否则 "
            "failed。你的核对结论以该判定块为准——没有判定块视为无证据。\n\n"
            f"## acceptance.checks\n{checks_text}\n\n"
            f"## 标准\n{contract.draft.acceptance.standard}"
        )
        addendum = handover_prompt_addendum(self._root, contract.contract_id)
        if addendum:
            verifier_prompt = f"{verifier_prompt}\n\n{addendum}"
        return AttemptInput(
            attempt_id=base.attempt_id,
            contract_id=base.contract_id,
            revision=base.revision,
            lease_generation=base.lease_generation,
            role=AttemptRole.VERIFIER,
            contract_snapshot=base.contract_snapshot,
            handover_path=base.handover_path,
            workspace_root=base.workspace_root,
            budget_remaining=base.budget_remaining,
            task_prompt=verifier_prompt,
            context_snapshot_path=base.context_snapshot_path,
        )
