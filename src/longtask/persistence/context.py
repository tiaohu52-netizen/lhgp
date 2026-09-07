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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from longtask.contracts.schema import ContractDraft, ContractView
from longtask.persistence.events import EventType
from longtask.persistence.events_query import get_latest_forecast_snapshot
from longtask.persistence.store import append_event

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


def _read_directive_cursor(conn: sqlite3.Connection, contract_id: str) -> int:
    """Last AGENT_MESSAGE event id this contract's attempts have consumed.

    Stored in the ``continuity_json`` column under a reserved key
    (``_directive_cursor``). Returns 0 if no cursor has been recorded
    yet (i.e. consume all events from the beginning).
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
    val = data.get(_DIRECTIVE_CURSOR_KEY, 0)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _bump_directive_cursor(conn: sqlite3.Connection, contract_id: str, new_id: int) -> None:
    """Persist the new cursor. ``new_id`` is always set to the max of
    the existing value and the new value so a stale write cannot
    rewind the cursor.
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
    try:
        current = int(data.get(_DIRECTIVE_CURSOR_KEY, 0))
    except (TypeError, ValueError):
        current = 0
    if new_id <= current:
        return  # don't rewind
    data[_DIRECTIVE_CURSOR_KEY] = int(new_id)
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
) -> tuple[Path, Path]:
    """物化该次 attempt 的上下文：active.md 快照 + scratch.md 骨架。

    返回 (active_path, scratch_path)。容量超限（policy.max_bytes）记
    context/capacity-refused 并抛 CapacityRefusedError（fail-closed，
    §4.1：压缩后仍不满足 required 容量合同则拒绝启动 attempt）。
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

    # Agent messaging：用户的 directive 消息注入到 agent 上下文最前面——
    # 这让用户可以在 agent 工作中途改变方向而不用终止重来。
    from lhgp.persistence.messages import pending_directives

    # P0 verifier finding: without a per-contract cursor, every
    # attempt sees the entire AGENT_MESSAGE history (the user can
    # fire 50 directives and each attempt re-injects all 50).
    # Read the cursor and pass ``after_event_id`` so only NEW
    # directives since the last attempt land in this snapshot;
    # the cursor is bumped to the max consumed event_id below.
    directive_cursor = _read_directive_cursor(conn, contract.contract_id)

    # 硬 cap:用户连发 50 条时不能让 snapshot 爆 max_bytes。每条 text
    # 截断到 240 字,够传达意图,防止单条 1MB directive 直接打爆。
    raw_directives = pending_directives(
        conn,
        contract_id=contract.contract_id,
        after_event_id=directive_cursor,
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
        sections += ["## ⚡ 用户指令（必须遵守）", ""]
        for d in directives:
            text = str(d.get("text", ""))[:_DIRECTIVE_TEXT_CHARS]
            sections.append(f"- **{text}**")
        if len(raw_directives) > _MAX_DIRECTIVES_INJECTED:
            sections.append(
                f"- … ({len(raw_directives) - _MAX_DIRECTIVES_INJECTED} more directives truncated)"
            )
        sections += [
            "",
            "以上指令来自用户，优先级高于合同中的 soft_guidance。"
            "如果你无法遵守，在写回中说明原因。",
            "",
        ]
        # Compute the max consumed event id; the cursor is bumped
        # *after* the snapshot is successfully written below — if
        # the capacity check fails (or disk write fails), the cursor
        # stays put and the next attempt replays the same directives.
        # The cursor never rewinds (see _bump_directive_cursor).
        max_event_id = max(
            (int(d["event_id"]) for d in directives if "event_id" in d),
            default=directive_cursor,
        )
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

    # Cursor bump *after* the snapshot is on disk: a capacity
    # failure or write failure above raises before reaching this
    # point, so the next attempt will replay the same directives
    # instead of silently losing them.
    if directives and max_event_id > directive_cursor:
        _bump_directive_cursor(conn, contract.contract_id, max_event_id)

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
    return active_path, scratch_path


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


__all__ = [
    "ACTIVE_FILE",
    "SCRATCH_FILE",
    "CapacityRefusedError",
    "ContextPolicy",
    "compile_context_snapshot",
    "handover_prompt_addendum",
]
