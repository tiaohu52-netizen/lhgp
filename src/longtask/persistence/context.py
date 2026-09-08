"""临时上下文（DESIGN §4.1）：认知工作集的物化与限额。

临时上下文是给当前执行者的认知工作集，不是第三份任务真相：由合同、
交接（handover）、最新进度编译出该次 attempt 的 active.md 快照 +
scratch.md 可编辑区。attempt 之间互不共享；快照带来源版本与过期时间。

实现范围（Developer Preview 最小闭环）：
- ContextPolicy：§4.1 policy 的程序化表达（limits.max_bytes、
  expires_after_minutes、editable 区块）；从合同 context 字段解析。
- compile_context_snapshot：物化 active.md（合同锚点 + 交接剩余/
  next_action + 最近 attempt 终态摘要），容量超限按 fail-closed 记
  context/capacity-refused 并拒绝启动（§4.1 容量合同）。
- init_scratch：初始化 scratch.md 可编辑区骨架。
- build_attempt_context：AttemptRunner 派发前的入口——编译快照并把
  交接摘要融入任务文本（修复「再派 attempt 没有验收失败上下文」的
  真实缺口，见内部真实运行记录）。

source 阶段摘要（stages/*.md）与 promotion 流程属 §4.1 完整语义，
本期不实现（claims 如实记录）；交接文件已是跨 attempt 的权威通道。
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from longtask.contracts.schema import ContractDraft, ContractView
from longtask.persistence.events import EventType
from longtask.persistence.events_query import get_latest_forecast_snapshot
from longtask.persistence.store import append_event, get_contract

logger = logging.getLogger(__name__)

CONTEXT_DIR = "context"
ATTEMPTS_DIR = "attempts"
ACTIVE_FILE = "active.md"
SCRATCH_FILE = "scratch.md"

# §4.1 默认限额：快照总量与过期时间（合同 context 字段可覆盖）
DEFAULT_MAX_BYTES = 24000
DEFAULT_EXPIRES_MINUTES = 240

# task_prompt 内交接摘要的追加上限：任务文本不该被交接内容淹没
HANDOVER_IN_PROMPT_CHARS = 1200

# 用户 directive 注入硬 cap:防止用户连发 50 条打爆 max_bytes,
# 也防止单条 1MB directive 直接撑爆 snapshot 渲染。
_MAX_DIRECTIVES_INJECTED = 20
_DIRECTIVE_TEXT_CHARS = 240

# Sidecar key inside ``contracts.continuity_json`` that holds the
# "last AGENT_MESSAGE event id this contract's attempts have
# consumed" cursor. The typed ``Continuity`` dataclass is unaware
# of this key; we read/write the raw JSON column directly so adding
# a cursor is a zero-migration change.
_DIRECTIVE_CURSOR_KEY = "_directive_cursor"

# Active.md 段顺序(优先级):breach warning > 长期记忆 > 其它。
# breach warning 必须在 contract anchor 之前,任何只读 contract metadata
# 的工具先看到时间风险信号。
# 长期记忆 放 deadline 之后、handover 之前 —— 风险感知 > 历史现场。


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """§4.1 准入合同的程序化表达（Developer Preview 子集）。

    required=true 的合同要求执行器装配临时上下文（§9：适配器无快照
    即拒接）；max_bytes 是容量合同，超限 fail-closed 拒绝启动 attempt。
    """

    required: bool = False
    max_bytes: int = DEFAULT_MAX_BYTES
    expires_after_minutes: int = DEFAULT_EXPIRES_MINUTES
    editable_sections: tuple[str, ...] = (
        "current_focus",
        "working_hypotheses",
        "next_actions",
        "risks",
        "open_questions",
        "handoff_notes",
    )

    @classmethod
    def from_contract(cls, draft: ContractDraft) -> ContextPolicy:
        """从合同 context 字段解析。

        可选 limits 字段形状错误时回落默认值；required 是语义开关，类型
        错误必须拒绝，只有显式布尔值才生效。
        """
        raw: Any = draft.context
        if not isinstance(raw, dict):
            raw = {}
        limits_raw = raw.get("limits")
        limits: dict[str, Any] = limits_raw if isinstance(limits_raw, dict) else {}
        if isinstance(limits.get("max_bytes"), bool):
            raise TypeError("context.limits.max_bytes must be an integer")
        if isinstance(limits.get("expires_after_minutes"), bool):
            raise TypeError("context.limits.expires_after_minutes must be an integer")
        try:
            max_bytes = int(limits.get("max_bytes", DEFAULT_MAX_BYTES))
            expires = int(limits.get("expires_after_minutes", DEFAULT_EXPIRES_MINUTES))
        except (TypeError, ValueError):
            max_bytes, expires = DEFAULT_MAX_BYTES, DEFAULT_EXPIRES_MINUTES
        raw_required = raw.get("required", False)
        if not isinstance(raw_required, bool):
            raise TypeError("context.required must be a boolean")
        return cls(
            required=raw_required,
            max_bytes=max(1, max_bytes),
            expires_after_minutes=max(1, expires),
        )


def _handover_data(
    root: Path,
    contract_id: str,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """读交接文件的最低必填结构；缺失按空值（无交接=初次 attempt）。

    读失败（OSError、parse 异常、data=None）时落
    HANDOVER_INCOMPLETE 事件 —— 区分"无交接=初次 attempt"和
    "交接文件损坏=上 attempt 写的坏掉"两种语义，避免 verifier 看到
    空交接段时误判为初次 attempt。
    """
    from longtask.persistence.projections import parse_handover_markdown

    path = root / "contracts" / contract_id / "handover.md"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data, violations = parse_handover_markdown(text)
    except OSError as exc:
        _record_handover_incomplete(conn, contract_id, "os_error", str(exc), now)
        return {}
    if data is None:
        _record_handover_incomplete(
            conn,
            contract_id,
            "parse_failed",
            "; ".join(violations) or "no parse result",
            now,
        )
        return {}
    return {
        "current_stage": data.current_stage,
        "remaining": "\n".join(f"- {item}" for item in data.remaining),
        "next_action": data.next_action,
        "estimate_remaining_hours": str(data.estimate_remaining_hours),
        "source_attempt_id": data.source_attempt_id,
        "open_risks": "\n".join(f"- {item}" for item in data.open_risks),
    }


def _record_handover_incomplete(
    conn: sqlite3.Connection | None,
    contract_id: str,
    reason: str,
    detail: str,
    now: datetime | None,
) -> None:
    """Best-effort: write HANDOVER_INCOMPLETE so the audit log shows why the
    handover section is empty."""
    if conn is None or now is None:
        return
    with contextlib.suppress(Exception):
        append_event(
            conn,
            contract_id=contract_id,
            goal_id=None,
            event_type=EventType.HANDOVER_INCOMPLETE,
            payload={"reason": reason, "detail": detail[:512]},
            now=now,
            actor="context",
            role="system",
        )


def _read_directive_cursor(
    conn: sqlite3.Connection, contract_id: str, to_agent: str | None = None
) -> int:
    """Last AGENT_MESSAGE event id this contract's attempts have consumed.

    Stored in the ``continuity_json`` column under a per-scope key:
    ``_directive_cursor`` for the legacy contract-level broadcast
    cursor, ``_directive_cursor::<agent>`` for a per-agent cursor
    (A2A scoping — only that agent's own cursor moves when it
    consumes a directive addressed to it).  Returns 0 if no cursor
    has been recorded yet (i.e. consume all events from the
    beginning).
    """
    row = conn.execute(
        "SELECT continuity_json FROM contracts WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()
    if row is None or not row[0]:
        return 0
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    key = _DIRECTIVE_CURSOR_KEY if to_agent is None else f"{_DIRECTIVE_CURSOR_KEY}::{to_agent}"
    val = data.get(key, 0)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def mark_directives_consumed(
    conn: sqlite3.Connection,
    contract_id: str,
    new_id: int,
    to_agent: str | None = None,
    *,
    consumed_ids: list[int] | None = None,
    now: datetime | None = None,
) -> bool:
    """Bump the per-(contract, agent) directive cursor to ``new_id``.

    Called by the runner **after** Popen succeeds for a spawned
    attempt, so the cursor only advances when the executor has
    actually been started.  Previously the cursor was advanced at
    snapshot-build time, which meant a spawn failure (executable
    missing, argv invalid, …) silently lost the directives the
    failed snapshot had inlined — the next attempt's snapshot
    would see the advanced cursor and skip them.

    A2A scoping: pass ``to_agent`` to move the per-agent cursor
    (used by directed directives).  Pass ``None`` to move the
    contract-level broadcast cursor (legacy behaviour).

    A2A delivery hardening (3rd-round review 2026-09-08):
    - ``consumed_ids`` (optional): the actual list of directive
      event_ids newly consumed by this snapshot.  Each one gets
      written to the per-agent dedup set
      (``acknowledged_directives::<agent>`` in ``continuity_json``)
      so a future snapshot rebuild cannot re-apply it; the legacy
      high-water-mark cursor still moves to ``new_id`` for the
      common case where the snapshot is built in event order.
    - ``now`` (optional): used to stamp the
      ``directive/acknowledged`` event.  When omitted, the call
      is a no-op for the confirmation event (legacy behaviour).
    Returns True iff the underlying row was actually updated (i.e.
    the cursor moved).  The cursor never rewinds; if a stale
    ``new_id`` is passed (lower than the stored cursor), the call
    is a no-op.
    """
    from longtask.persistence.store import transaction

    moved = False
    with transaction(conn):
        # Cursor bump: max-guard in SQL prevents rewind on race.
        key = _DIRECTIVE_CURSOR_KEY if to_agent is None else f"{_DIRECTIVE_CURSOR_KEY}::{to_agent}"
        moved = _bump_directive_cursor_atomic(conn, contract_id, key, new_id)
        # Ack recording is independent of the cursor bump:
        # even if the cursor did not move (another thread's
        # new_id was higher), this thread's ``consumed_ids`` are
        # still part of the dedup set and must be recorded.
        # Otherwise a slow thread with a lower new_id would
        # silently lose its consumed_ids (5th-round review
        # regression test ``test_concurrent_consumed_ids_record_all_acks``).
        if consumed_ids and now is not None:
            _record_directive_acks(
                conn, contract_id, to_agent=to_agent, consumed_ids=consumed_ids, now=now
            )
    return moved


def _read_acknowledged_directives(
    conn: sqlite3.Connection, contract_id: str, *, to_agent: str | None
) -> set[int]:
    """Return the set of directive event_ids this agent has already ack'd."""
    row = conn.execute(
        "SELECT continuity_json FROM contracts WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()
    if row is None:
        return set()
    raw = row[0] or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    key = (
        _DIRECTIVE_CURSOR_KEY + "_ack"
        if to_agent is None
        else f"{_DIRECTIVE_CURSOR_KEY}::{to_agent}::_ack"
    )
    value = data.get(key, [])
    if not isinstance(value, list):
        return set()
    out: set[int] = set()
    for item in value:
        if isinstance(item, int):
            out.add(item)
    return out


def _record_directive_acks(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    to_agent: str | None,
    consumed_ids: list[int],
    now: datetime,
) -> None:
    """Record per-agent directive acknowledgements and emit audit events."""
    row = conn.execute(
        "SELECT continuity_json FROM contracts WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()
    if row is None:
        return
    raw = row[0] or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    key = (
        _DIRECTIVE_CURSOR_KEY + "_ack"
        if to_agent is None
        else f"{_DIRECTIVE_CURSOR_KEY}::{to_agent}::_ack"
    )
    existing = data.get(key, [])
    if not isinstance(existing, list):
        existing = []
    seen_ids: set[int] = {item for item in existing if isinstance(item, int)}
    new_acks: list[int] = []
    for cid in consumed_ids:
        if not isinstance(cid, int) or cid in seen_ids:
            continue
        seen_ids.add(cid)
        new_acks.append(cid)
    if not new_acks:
        return
    # Bounded retention: keep at most the last 1000 acknowledged ids
    # to prevent unbounded growth.  A contract with thousands of
    # directives over a long-lived goal would otherwise bloat the row.
    merged: list[int] = (existing + new_acks)[-1000:]
    data[key] = merged
    from lhgp.persistence.events import EventType
    from lhgp.persistence.events_query import append_event

    actor_label = f"agent:{to_agent}" if to_agent else "agent:broadcast"
    # The dedup set UPDATE and each directive/acknowledged event write
    # are inside one transaction. Without this, a crash between the
    # dedup write and the last ack event would leave the system in a
    # "consumed but not audited" state, which breaks the
    # directive/acknowledged rate metric and audit trail. The inner
    # ``with transaction(conn):`` opens its own BEGIN IMMEDIATE;
    # if the caller already has one open, ``transaction()`` is
    # a no-op (it checks ``conn.in_transaction``), so this is
    # nesting-safe.
    from longtask.persistence.store import transaction

    with transaction(conn):
        try:
            conn.execute(
                "UPDATE contracts SET continuity_json = ? WHERE contract_id = ?",
                (json.dumps(data, ensure_ascii=False), contract_id),
            )
        except sqlite3.Error as exc:
            logger.warning(
                "directive ack write failed for contract %s (to_agent=%s): %s",
                contract_id,
                to_agent,
                exc,
            )
            return
        for did in new_acks:
            try:
                append_event(
                    conn,
                    contract_id=contract_id,
                    event_type=EventType.DIRECTIVE_ACKNOWLEDGED,
                    payload={
                        "directive_event_id": did,
                        "to_agent": to_agent,
                        "cursor_position": max(merged) if merged else 0,
                    },
                    now=now,
                    actor=actor_label,
                )
            except sqlite3.Error as exc:
                logger.warning(
                    "directive/acknowledged event write failed for %s/%s: %s",
                    contract_id,
                    did,
                    exc,
                )


def _bump_directive_cursor_atomic(
    conn: sqlite3.Connection,
    contract_id: str,
    key: str,
    new_id: int,
) -> bool:
    """Atomically set the per-(contract, key) directive cursor to
    ``MAX(existing, new_id)``.

    One SQL statement combines the JSON read, the max-guard, and
    the JSON write, so two concurrent callers cannot lose updates
    via a classic read-merge-write race.  The caller's
    ``with transaction(conn):`` provides the WAL write lock; the
    SQL is the race-free piece.

    Returns True iff the cursor moved (strictly forward).
    """
    cursor = conn.execute(
        """
        UPDATE contracts
        SET continuity_json = json_set(
            continuity_json,
            '$.' || ?,
            MAX(COALESCE(CAST(json_extract(continuity_json, '$.' || ?) AS INTEGER), 0), ?)
        )
        WHERE contract_id = ?
          AND MAX(COALESCE(CAST(json_extract(continuity_json, '$.' || ?) AS INTEGER), 0), ?)
              > COALESCE(CAST(json_extract(continuity_json, '$.' || ?) AS INTEGER), 0)
        """,
        (key, key, new_id, contract_id, key, new_id, key),
    ).rowcount
    return cursor > 0


def _bump_directive_cursor(
    conn: sqlite3.Connection, contract_id: str, new_id: int, to_agent: str | None = None
) -> None:
    """Persist the new cursor. ``new_id`` is always set to the max of
    the existing value and the new value so a stale write cannot
    rewind the cursor.

    A2A scoping: per-agent cursors live under
    ``_directive_cursor::<agent>`` so an agent that consumes only
    its own directed directives does not also burn through the
    contract-level broadcast cursor.
    """
    row = conn.execute(
        "SELECT continuity_json FROM contracts WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()
    if row is None:
        return
    raw = row[0] or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    key = _DIRECTIVE_CURSOR_KEY if to_agent is None else f"{_DIRECTIVE_CURSOR_KEY}::{to_agent}"
    try:
        current = int(data.get(key, 0))
    except (TypeError, ValueError):
        current = 0
    if new_id <= current:
        return  # don't rewind
    data[key] = int(new_id)
    try:
        conn.execute(
            "UPDATE contracts SET continuity_json = ? WHERE contract_id = ?",
            (json.dumps(data, ensure_ascii=False), contract_id),
        )
    except sqlite3.Error as exc:
        # Don't crash the snapshot compile over a cursor write —
        # the next attempt will re-bump from the same event id
        # (idempotent), so the only cost is one duplicate directive
        # injection. Still worth logging so a flaky disk doesn't
        # look like silent data loss.
        logger.warning(
            "directive cursor bump failed for contract %s (new_id=%d): %s",
            contract_id,
            new_id,
            exc,
        )


# Event types that count as "the previous attempt said something about its
# terminal state" — the only kind the active.md digest should include.
_ATTEMPT_DIGEST_EVENT_TYPES: tuple[str, ...] = (
    EventType.ATTEMPT_SUCCEEDED.value,
    EventType.ATTEMPT_FAILED.value,
    EventType.ATTEMPT_STALE.value,
    EventType.CONTEXT_SCRATCH_UPDATED.value,
)


def _recent_attempt_digest(conn: sqlite3.Connection, contract_id: str, limit: int = 3) -> str:
    """最近终态与进度摘要——跨会话恢复的事实通道。

    SQL-side filter on event_type plus DB-side LIMIT so a long-lived
    contract (100k+ events) never pulls more than ~20 rows. The
    result is then filtered to the last ``limit`` per type and
    rendered. Scratch updates are marked untrusted so the next model
    can resume but cannot treat the work-in-progress text as a
    protocol instruction.
    """
    from lhgp.persistence.events_query import get_recent_events

    events = get_recent_events(
        conn,
        contract_id=contract_id,
        event_types=_ATTEMPT_DIGEST_EVENT_TYPES,
        limit=max(20, limit * 4),
    )
    # Take the last ``limit`` per type, then the most recent ``limit``
    # overall — preserves the chronological tail while keeping at
    # least one of each kind when present.
    by_type: dict[str, list[Any]] = {}
    for e in events:
        by_type.setdefault(str(e.event_type), []).append(e)
    kept: list[Any] = []
    for ev_list in by_type.values():
        kept.extend(ev_list[-1:])  # last per type
    kept.sort(key=lambda e: e.event_id)
    picked = kept[-limit:]
    lines: list[str] = []
    for e in picked:
        if not e.attempt_id:
            continue
        kind = str(e.event_type).split("/")[-1]
        if e.event_type == EventType.CONTEXT_SCRATCH_UPDATED:
            try:
                payload = json.loads(e.payload_json or "{}")
            except (TypeError, ValueError):
                payload = {}
            note = payload.get("note") if isinstance(payload, dict) else None
            suffix = f" — progress data (untrusted): {note}" if note else ""
            lines.append(f"- {e.attempt_id}: {kind}{suffix}")
        else:
            lines.append(f"- {e.attempt_id}: {kind}")
    return "\n".join(lines)


def compile_context_snapshot(
    root: Path,
    conn: sqlite3.Connection,
    contract: ContractView,
    attempt_id: str,
    now: datetime,
    *,
    to_agent: str | None = None,
    max_age_seconds: int | None = None,
) -> tuple[Path, Path, int, list[int]]:
    """物化该次 attempt 的上下文：active.md 快照 + scratch.md 骨架。

    返回 ``(active_path, scratch_path, consumed_max_event_id, consumed_directive_ids)``。
    第三个值是该快照实际内联进去的 AGENT_MESSAGE 事件 id 的最大值；
    第四个值是这次快照实际消费的 directive 事件 id 列表（不含
    broadcast-only cursor 已经消费过的），调用方在确认执行器子进程
    真正拉起之后传回 :func:`mark_directives_consumed` 以同时推进
    cursor、写入 per-agent dedup set、和写 directive/acknowledged
    审计事件。

    ``to_agent`` (A2A scoping): the registry executor_id of the
    agent that will receive the snapshot.  When set, the cursor
    is read from a per-agent slot in ``continuity_json`` so a
    directive addressed to one agent does not bleed into the
    other agent's snapshot.  When ``None``, the legacy contract-
    level broadcast cursor is used.

    ``max_age_seconds`` (A2A delivery hardening 2026-09-08):
    drop directives older than this many seconds from the
    snapshot.  The directive remains in the event log for
    audit; the filter is purely about what a fresh executor
    should act on.

    P1 review（2026-09-08，第二轮）：原实现快照写盘即推 cursor，
    意味着「快照生成 → spawn 失败」之间没有任何信号时，已注入快照
    的用户指令会随 cursor 上推而丢失（下一个 attempt 的快照会跳过
    它们）。本函数现在不直接动 cursor，由调用方在 spawn 兑现后再
    落账。

    容量超限（policy.max_bytes）记 context/capacity-refused 并抛
    CapacityRefusedError（fail-closed，§4.1：压缩后仍不满足
    required 容量合同则拒绝启动 attempt）。
    """
    policy = ContextPolicy.from_contract(contract.draft)
    draft = contract.draft
    # Memory-and-wiki Phase 2 + context P0: clean up any expired
    # snapshots from prior attempts before writing the new one.
    # The previous attempt's file declares its own expires_at; once
    # the clock passes that, the file is dead weight on disk and a
    # stale read for any consumer that doesn't re-check the clock.
    _expire_old_snapshots(root, conn, contract.contract_id, now=now)
    handover = _handover_data(root, contract.contract_id, conn=conn, now=now)
    digest = _recent_attempt_digest(conn, contract.contract_id)
    deadline_snapshot = get_latest_forecast_snapshot(conn, contract_id=contract.contract_id)

    # Memory-and-wiki Phase 2: 跨合同长期记忆检索。
    # 按 contract.context 选域(global 总是带,domain 按 contract 域名,
    # project 取 score top-N),并尊重 context.max_bytes 容量合同。
    from lhgp.memory import MemoryIndex, render_for_active_md

    # 给 memory 一个 1/10 容量但有 40% 上限,小合同(max_bytes<10K)也
    # 不会因为 clamp 而 memory 占整体 100%+。下限 1.5KB 保证至少能
    # 装 1-2 条小记忆。
    memory_budget = min(int(policy.max_bytes * 0.4), 4000)
    memory_budget = max(memory_budget, 1500)
    mem_index = MemoryIndex(conn, budget_bytes=memory_budget)
    mem_text = render_for_active_md(
        mem_index.retrieve(draft.context if isinstance(draft.context, dict) else None)
    )

    # Agent messaging：directive 消息注入到 agent 上下文最前面——
    # 这让用户/其他 agent 可以在工作中途改变方向而不用终止重来。
    from lhgp.persistence.messages import pending_directives

    # P0 verifier finding: without a per-contract cursor, every
    # attempt sees the entire AGENT_MESSAGE history (the user can
    # fire 50 directives and each attempt re-injects all 50).
    # Read the cursor and pass ``after_event_id`` so only NEW
    # directives since the last attempt land in this snapshot;
    # the cursor is bumped to the max consumed event_id below.
    #
    # A2A scoping (2026-09-08, 3rd-round review): the cursor is
    # read from a per-agent slot when ``to_agent`` is set, so a
    # directive addressed to one agent is consumed once by *that*
    # agent and never re-injected to any other agent working the
    # same contract.  Broadcast directives (to_agent=None) still
    # use the contract-level cursor so the legacy user → all-agents
    # path keeps working.
    directive_cursor = _read_directive_cursor(conn, contract.contract_id, to_agent=to_agent)
    # Default to the existing cursor when no directives are included;
    # the runner's post-spawn mark_directives_consumed call is a
    # no-op for new_id <= current (P1 review), so passing the
    # existing cursor forward is safe.
    max_event_id = directive_cursor
    consumed_directive_ids: list[int] = []

    # 硬 cap:用户连发 50 条时不能让 snapshot 爆 max_bytes。每条 text
    # 截断到 240 字,够传达意图,防止单条 1MB directive 直接打爆。
    raw_directives = pending_directives(
        conn,
        contract_id=contract.contract_id,
        after_event_id=directive_cursor,
        to_agent=to_agent,
        now=now,
        max_age_seconds=max_age_seconds,
        dedup_seen=_read_acknowledged_directives(conn, contract.contract_id, to_agent=to_agent),
    )
    directives = raw_directives[:_MAX_DIRECTIVES_INJECTED]
    sections: list[str] = [
        f"# Active Context: {contract.contract_id} / {attempt_id}",
        "",
        f"- compiled_at: {now.isoformat()}",
        f"- expires_at: {(now + timedelta(minutes=policy.expires_after_minutes)).isoformat()}",
        f"- contract_revision: {contract.revision}",
        "",
    ]
    # Deadline breach warning at the very top so any tool that scans
    # for "⚠" notices before doing anything else. (P0 verifier finding.)
    if draft.deadline_at < now:
        breach_minutes = int((now - draft.deadline_at).total_seconds() // 60)
        sections.insert(
            0,
            f"## ⚠️ 合同已超期 {breach_minutes} 分钟 "
            f"(deadline_at={draft.deadline_at.isoformat()}, now={now.isoformat()})",
        )
        sections.insert(1, "")
    if directives:
        # A2A scoping (3rd-round review): the header reflects the
        # actual source — a directive from agent X addressed to this
        # agent is rendered as such, not as a generic "user
        # directive".  This preserves provenance for the model and
        # matches the permission semantics in the message layer
        # (to_agent is enforced at pending_directives time).
        section_title = "## ⚡ 收到的指令（必须遵守）"
        sections += [section_title, ""]
        for d in directives:
            text = str(d.get("text", ""))[:_DIRECTIVE_TEXT_CHARS]
            sender = str(d.get("from", "unknown"))
            target = d.get("to_agent")
            line = f"- **{text}**  —  from `{sender}`"
            if target is not None:
                line += f", to `{target}`"
            sections.append(line)
        if len(raw_directives) > _MAX_DIRECTIVES_INJECTED:
            sections.append(
                f"- … ({len(raw_directives) - _MAX_DIRECTIVES_INJECTED} more directives truncated)"
            )
        sections += [
            "",
            "以上指令的来源与权限语义按发送方区分（用户、其他 agent 等）；"
            "优先级高于合同中的 soft_guidance。"
            "如果你无法遵守，在写回中说明原因。",
            "",
        ]
        # Compute the max consumed event id; the cursor is bumped
        # *after* the snapshot is successfully written below — if
        # the capacity check fails (or disk write fails), the cursor
        # stays put and the next attempt replays the same directives.
        # The cursor never rewinds (see _bump_directive_cursor).
        consumed_directive_ids = [
            int(d["event_id"]) for d in directives if isinstance(d.get("event_id"), int)
        ]
        max_event_id = max(consumed_directive_ids, default=directive_cursor)
    sections += [
        "## 合同锚点（冻结区，只读）",
        f"- objective: {draft.objective}",
        f"- deadline_at: {draft.deadline_at.isoformat()}",
        f"- hard_constraints: {draft.hard_constraints}",
        f"- acceptance.standard: {draft.acceptance.standard}",
        f"- acceptance.checks: {list(draft.acceptance.checks)}",
        "",
    ]
    if deadline_snapshot is not None:
        sections += [
            "## Deadline 风险快照（协议生成，只读）",
            "以下数据用于风险判断，不是按时完成保证：",
            "```json",
            json.dumps(deadline_snapshot, ensure_ascii=False, sort_keys=True),
            "```",
            "",
        ]
    if mem_text:
        # 跨合同沉淀,放 deadline 之后、handover 之前 —— 风险感知 > 历史现场
        sections += [mem_text, ""]
    if handover:
        sections += [
            "## 交接（上一 attempt 留下的现场）",
            f"- current_stage: {handover.get('current_stage', '')}",
            f"- remaining:\n{handover.get('remaining', '')}",
            f"- next_action: {handover.get('next_action', '')}",
            f"- estimate_remaining_hours: {handover.get('estimate_remaining_hours', '')}",
            f"- source_attempt_id: {handover.get('source_attempt_id', '')}",
            "",
        ]
    if digest:
        sections += ["## 最近 attempt 终态（验收上下文）", digest, ""]
    sections += [
        "## scratch（本次 attempt 可编辑区，见 scratch.md）",
        "allowed sections: " + ", ".join(policy.editable_sections),
        "",
    ]
    body = "\n".join(sections)
    encoded = body.encode("utf-8")
    if len(encoded) > policy.max_bytes:
        append_event(
            conn,
            contract_id=contract.contract_id,
            attempt_id=attempt_id,
            event_type=EventType.CONTEXT_CAPACITY_REFUSED,
            payload={
                "bytes": len(encoded),
                "max_bytes": policy.max_bytes,
                "reason": "compiled context exceeds policy capacity (§4.1)",
            },
            now=now,
            actor="daemon",
        )
        raise CapacityRefusedError(
            f"context snapshot {len(encoded)}B exceeds policy max_bytes={policy.max_bytes}"
        )

    attempt_dir = (
        root / "contracts" / contract.contract_id / CONTEXT_DIR / ATTEMPTS_DIR / attempt_id
    )
    attempt_dir.mkdir(parents=True, exist_ok=True)
    active_path = attempt_dir / ACTIVE_FILE
    active_path.write_text(body, encoding="utf-8")

    scratch_path = attempt_dir / SCRATCH_FILE
    scratch_path.write_text(_scratch_skeleton(attempt_id), encoding="utf-8")

    # Cursor bump is *not* done here.  P1 review (2026-09-08, 2nd
    # round) showed that bumping on snapshot build loses the
    # directives when the spawn then fails (executable missing,
    # argv invalid, …): the cursor advances, the next attempt's
    # snapshot skips the same directives, and the user message
    # never reaches an executor.  The caller (build_attempt_input
    # → runner.start_attempt → SubprocessAdapter.spawn) now calls
    # :func:`mark_directives_consumed` after Popen succeeds, so the
    # cursor only advances when the executor was actually started.
    # We still return the max_event_id so the caller knows what to
    # mark consumed.

    append_event(
        conn,
        contract_id=contract.contract_id,
        attempt_id=attempt_id,
        event_type=EventType.CONTEXT_SNAPSHOT_BUILT,
        payload={
            "active_path": str(active_path),
            "bytes": len(encoded),
            "expires_after_minutes": policy.expires_after_minutes,
        },
        now=now,
        actor="daemon",
    )
    return active_path, scratch_path, max_event_id, consumed_directive_ids


def _scratch_skeleton(attempt_id: str) -> str:
    """scratch.md 骨架：§4.1 editable 区块。"""
    return (
        f"# Scratch: {attempt_id}\n\n"
        "## current_focus\n\n(当前焦点)\n\n"
        "## working_hypotheses\n\n(工作假设)\n\n"
        "## next_actions\n\n(下一步)\n\n"
        "## risks\n\n(风险)\n\n"
        "## open_questions\n\n(待解问题)\n\n"
        "## handoff_notes\n\n(交接备注)\n"
    )


_EXPIRES_AT_PREFIX = "- expires_at: "


def _expire_old_snapshots(
    root: Path,
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    now: datetime,
) -> int:
    """Sweep expired active.md snapshots. Emits CONTEXT_SNAPSHOT_EXPIRED.

    Walks ``contracts/<id>/context/attempts/*/active.md``, parses the
    ``- expires_at:`` header line, and removes the file + emits an
    audit event if the deadline has passed. Best-effort: a malformed
    file is left in place (no event) so a real snapshot is never
    lost because of a parsing error. Returns the count of expired
    files for tests / logging.
    """
    attempts_root = root / "contracts" / contract_id / CONTEXT_DIR / ATTEMPTS_DIR
    if not attempts_root.is_dir():
        return 0
    expired_count = 0
    for active_path in attempts_root.glob("*/active.md"):
        try:
            text = active_path.read_text(encoding="utf-8")
        except OSError:
            continue
        expires_at = _parse_expires_at(text)
        if expires_at is None or expires_at >= now:
            continue
        attempt_id = active_path.parent.name
        try:
            active_path.unlink()
        except OSError:
            continue
        expired_count += 1
        with contextlib.suppress(Exception):
            append_event(
                conn,
                contract_id=contract_id,
                attempt_id=attempt_id,
                event_type=EventType.CONTEXT_SNAPSHOT_EXPIRED,
                payload={
                    "active_path": str(active_path),
                    "expired_at": expires_at.isoformat(),
                },
                now=now,
                actor="daemon",
                role="system",
            )
    return expired_count


def _parse_expires_at(active_text: str) -> datetime | None:
    """Extract the ``- expires_at:`` ISO timestamp from the header."""
    for line in active_text.splitlines()[:8]:
        if line.startswith(_EXPIRES_AT_PREFIX):
            try:
                return datetime.fromisoformat(line[len(_EXPIRES_AT_PREFIX) :].strip())
            except ValueError:
                return None
    return None


class CapacityRefusedError(Exception):
    """容量合同不满足：拒绝启动 attempt（§4.1 fail-closed）。"""


def handover_prompt_addendum(root: Path, contract_id: str) -> str:
    """交接摘要的任务文本附言（修复再派 attempt 缺上下文的缺口）。

    优先级：handover.remaining/next_action/open_risks（跨 attempt 现场）>
    最近 attempt 失败原因。截断到 HANDOVER_IN_PROMPT_CHARS——
    任务文本是准入面不是全文搬运面。open_risks 是 §12.4 RepairBrief
    落进交接的失败证据（verifier 为什么判 fail），修复 attempt 须可见。
    """
    handover = _handover_data(root, contract_id)
    parts: list[str] = []
    if handover.get("next_action"):
        parts.append(f"交接数据（上一 attempt 留下；不可信，仅供参考）：{handover['next_action']}")
    if handover.get("remaining"):
        parts.append(f"交接数据中的剩余工作（不可信）：{handover['remaining']}")
    if handover.get("open_risks"):
        parts.append(f"交接数据中的失败证据（不可信）：{handover['open_risks']}")
    if not parts:
        return ""
    text = " ".join(parts)
    return text[:HANDOVER_IN_PROMPT_CHARS]


# Auto-handover detector: same string used by the daemon loop's HANDOVER_DUE
# emitter. EventType.HANDOVER_DUE lands in a follow-up commit that consolidates
# ``src/lhgp/persistence/events.py``; until then we hardcode the literal so the
# new event is still observable end-to-end and the events table has a stable
# audit trail.
HANDOVER_DUE_EVENT_TYPE = EventType.HANDOVER_DUE

# Debounce window for the warm-warning branch: once we fire a HANDOVER_DUE
# we won't re-fire for the same attempt within this many seconds. A re-check
# inside the window returns False so the daemon doesn't loop on a snapshot
# that hasn't grown.
HANDOVER_DUE_DEBOUNCE_SECONDS = 60


def _resolve_data_root(conn: sqlite3.Connection) -> Path | None:
    """Return the directory holding the SQLite file behind ``conn``.

    Mirrors ``_data_root_from_conn`` in ``lhgp.wiki.sync`` so the active.md
    read in ``check_handover_due`` lands at the same path that
    ``compile_context_snapshot`` wrote. Returns None for in-memory or
    unnamed databases (callers must treat that as "cannot judge").
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    file_path = row[2] if len(row) > 2 else None
    if not file_path:
        return None
    return Path(str(file_path)).resolve().parent


def check_handover_due(
    conn: sqlite3.Connection,
    contract_id: str,
    attempt_id: str,
    *,
    low_water: float = 0.6,
    high_water: float = 0.9,
) -> bool:
    """Decide if this attempt's active.md is at risk of exhausting the
    §4.1 context window.

    The daemon loop calls this between attempts. When True, the caller
    writes a ``handover.md`` via the existing ``_handover_data`` path
    with ``next_action`` populated from the most recent attempt's
    submitted evaluation, and emits a HANDOVER_DUE event so the next
    attempt resumes from a clean session.

    Returns:
        - False: contract not found, or active.md missing (no snapshot
          to judge).
        - True: size / max_bytes >= high_water (overdue — fire
          immediately, no debounce).
        - True: size / max_bytes >= low_water AND no HANDOVER_DUE event
          in the last ``HANDOVER_DUE_DEBOUNCE_SECONDS`` for this attempt
          (warm warning; the function writes a HANDOVER_DUE event
          itself so a re-call inside the debounce window returns False).
        - False otherwise.
    """
    view = get_contract(conn, contract_id)
    if view is None:
        return False
    policy = ContextPolicy.from_contract(view.draft)

    root = _resolve_data_root(conn)
    if root is None:
        return False
    active_path = (
        root / "contracts" / contract_id / CONTEXT_DIR / ATTEMPTS_DIR / attempt_id / ACTIVE_FILE
    )
    if not active_path.is_file():
        return False

    size = active_path.stat().st_size
    ratio = size / policy.max_bytes

    if ratio >= high_water:
        return True  # Overdue: always fire, no debounce.

    if ratio >= low_water:
        now = datetime.now(UTC)
        cutoff_iso = (now - timedelta(seconds=HANDOVER_DUE_DEBOUNCE_SECONDS)).isoformat()
        recent = conn.execute(
            "SELECT 1 FROM events WHERE contract_id = ? AND attempt_id = ? "
            "AND event_type = ? AND created_at >= ? LIMIT 1",
            (contract_id, attempt_id, HANDOVER_DUE_EVENT_TYPE, cutoff_iso),
        ).fetchone()
        if recent is not None:
            return False
        append_event(
            conn,
            contract_id=contract_id,
            attempt_id=attempt_id,
            event_type=HANDOVER_DUE_EVENT_TYPE,
            payload={
                "size": size,
                "max_bytes": policy.max_bytes,
                "ratio": round(ratio, 4),
                "reason": "auto_handover_warm",
            },
            now=now,
            actor="daemon",
            role="system",
        )
        return True

    return False


__all__ = [
    "ACTIVE_FILE",
    "SCRATCH_FILE",
    "CapacityRefusedError",
    "ContextPolicy",
    "check_handover_due",
    "compile_context_snapshot",
    "handover_prompt_addendum",
]
