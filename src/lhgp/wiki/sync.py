"""Self-publishing wiki: render Markdown pages from authoritative contract state.

One page per active contract lives at ``<wiki_root>/auto/<contract_id>.md``.
Pages are re-rendered on every ``publish`` call — the page body always
reflects the current contract view, so callers don't need to track diffs.

Active means ``ContractState`` not in the terminal set. The state machine's
canonical ``NON_TERMINAL_STATES`` is the source of truth for "active"
(see ``lhgp.contracts.state_machine``). Terminal contracts are not
re-published; an existing page is only re-rendered with a terminal banner
once it is older than :data:`TERMINAL_BANNER_DAYS`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.contracts.contract_view import ContractState
from lhgp.contracts.state_machine import NON_TERMINAL_STATES
from lhgp.memory.types import Memory
from longtask.contracts.schema import ContractView
from longtask.persistence.projections import HANDOVER_FILE, parse_handover_markdown
from longtask.persistence.store import list_contracts

AUTO_SUBDIR = "auto"
TERMINAL_BANNER_DAYS = 7
TERMINAL_STATES: frozenset[ContractState] = frozenset(
    {
        ContractState.COMPLETE,
        ContractState.SATISFIED,
        ContractState.CANCELLED,
        ContractState.ARCHIVED,
    }
)
# Cap the per-page memory section so a contract with thousands of linked
# memories does not blow the frontmatter parsing budget on the indexer side.
MAX_MEMORIES_PER_PAGE = 50


def _data_root_from_conn(conn: sqlite3.Connection) -> Path | None:
    """Return the directory holding the SQLite file behind ``conn``.

    Used to find the on-disk ``handover.md`` projection. Returns None when
    the connection points at an in-memory or unnamed database — callers
    must treat that as "no handover available" rather than guessing.
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    # PRAGMA database_list schema: (seq, name, file). ``file`` is empty for
    # in-memory or temporary databases.
    file_path = row[2] if len(row) > 2 else None
    if not file_path:
        return None
    return Path(str(file_path)).resolve().parent


def _related_memories(conn: sqlite3.Connection, contract_id: str) -> list[Memory]:
    """Load every memory row that names this contract as its source.

    Bypasses ``list_memories`` because that helper has no
    ``source_contract_id`` filter and we want the complete listing for
    the page, not a budget-fitted top-N.
    """
    rows = conn.execute(
        "SELECT * FROM memories WHERE source_contract_id = ? "
        "ORDER BY score DESC, created_at DESC LIMIT ?",
        (contract_id, MAX_MEMORIES_PER_PAGE),
    ).fetchall()
    out: list[Memory] = []
    for r in rows:
        try:
            out.append(Memory.from_db_row(r))
        except (KeyError, TypeError, ValueError):
            # A corrupted row should not abort the whole publish pass;
            # drop it and keep going.
            continue
    return out


def _handover_next_action(data_root: Path | None, contract_id: str) -> str:
    """Read the contract's latest handover and return its ``next_action``.

    Mirrors the path layout in :func:`compile_context_snapshot`: the
    handover file lives at ``<data_root>/contracts/<id>/handover.md``.
    """
    if data_root is None:
        return "—"
    path = data_root / "contracts" / contract_id / HANDOVER_FILE
    if not path.is_file():
        return "—"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "—"
    data, violations = parse_handover_markdown(text)
    if data is None or violations:
        return "—"
    value = data.next_action.strip()
    return value or "—"


def _format_iso(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.isoformat()


def _minutes_overdue(now: datetime, deadline: datetime) -> int:
    delta = now - deadline
    return max(0, int(delta.total_seconds() // 60))


def _yaml_list(values: tuple[str, ...] | list[str]) -> str:
    if not values:
        return "[]"
    quoted = [f'"{v.replace(chr(34), chr(92) + chr(34))}"' for v in values]
    return "[" + ", ".join(quoted) + "]"


def _status_line(view: ContractView) -> str:
    state = view.state.value
    acceptance = view.acceptance_status.value
    deadline = view.deadline_status.value
    return f"state={state} · acceptance={acceptance} · deadline={deadline}"


def _deadline_block(view: ContractView, now: datetime) -> list[str]:
    deadline = view.draft.deadline_at
    overdue_minutes = _minutes_overdue(now, deadline)
    lines: list[str] = []
    if overdue_minutes > 0:
        lines.append(f"> ⚠️ 已超期 {overdue_minutes} 分钟")
    lines.append(f"`{_format_iso(deadline)}`")
    return lines


def _render_page(
    view: ContractView,
    memories: list[Memory],
    *,
    next_action: str,
    is_terminal: bool,
    now: datetime,
) -> str:
    title = view.draft.title or view.contract_id
    deadline_str = _format_iso(view.draft.deadline_at)
    tags: list[str] = []
    ctx = view.draft.context
    if isinstance(ctx, dict):
        ctx_tags = ctx.get("tags")
        if isinstance(ctx_tags, list):
            tags.extend(str(t) for t in ctx_tags)
    for mem in memories:
        for t in mem.tags:
            if t not in tags:
                tags.append(t)
    if not tags:
        tags = ["auto/contract-page"]

    fm_lines = [
        "---",
        "type: contract-page",
        f"id: {view.contract_id}",
        f"title: {title}",
        f"status: {view.state.value}",
        f"deadline: {deadline_str}",
        f"tags: {_yaml_list(tags)}",
        "audience: [contractor, reviewer]",
        "---",
        "",
    ]
    body: list[str] = []
    if is_terminal:
        body.append(
            f"> ⚠️ TERMINAL — contract reached state `{view.state.value}` on "
            f"`{_format_iso(view.updated_at)}`. This page is auto-rendered "
            "and may not reflect the latest plan; check the contract state in the DB."
        )
        body.append("")
    body.append(f"# {title}")
    body.append("")
    body.append("## objective")
    body.append("")
    body.append(view.draft.objective.strip() or "_(no objective)_")
    body.append("")
    body.append("## status")
    body.append("")
    body.append(_status_line(view))
    body.append("")
    body.append("## acceptance")
    body.append("")
    body.append("**standard**")
    body.append("")
    body.append(view.draft.acceptance.standard.strip() or "_(none)_")
    body.append("")
    body.append("**checks**")
    body.append("")
    if not view.draft.acceptance.checks:
        body.append("- (none)")
    else:
        for chk in view.draft.acceptance.checks:
            if hasattr(chk, "to_dict"):
                rendered = json.dumps(chk.to_dict(), ensure_ascii=False)
            else:
                rendered = str(chk)
            body.append(f"- `{rendered}`")
    body.append("")
    body.append("## deadline")
    body.append("")
    body.extend(_deadline_block(view, now))
    body.append("")
    body.append("## next_action")
    body.append("")
    body.append(next_action)
    body.append("")
    body.append("## memory")
    body.append("")
    if not memories:
        body.append("- _(no memories linked to this contract)_")
    else:
        for m in memories:
            body.append(f"- {m.title} ({m.kind.value}, {m.scope.value}, score={m.score:.2f})")
    body.append("")
    body.append("## related")
    body.append("")
    body.append("_(reserved for future-stream links)_")
    body.append("")
    return "\n".join(fm_lines) + "\n".join(body)


def _atomic_write(path: Path, content: str) -> None:
    """temp + os.replace: a crash mid-write cannot leave a half-written page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _iter_contracts(
    conn: sqlite3.Connection,
    states: frozenset[ContractState],
) -> list[ContractView]:
    """Call :func:`list_contracts` once per state to materialise a flat list."""
    out: list[ContractView] = []
    for st in states:
        out.extend(list_contracts(conn, state=st, limit=10_000))
    return out


def publish_active_contracts(
    conn: sqlite3.Connection,
    wiki_root: Path,
    *,
    now: datetime | None = None,
) -> list[Path]:
    """Render a Markdown page per active contract under ``<wiki_root>/auto/``.

    Active contracts get a fresh page every call. Terminal contracts get
    a re-rendered page only if an existing page is older than
    :data:`TERMINAL_BANNER_DAYS`; the re-render prepends a terminal banner
    so a reader can tell at a glance the contract is no longer in flight.

    Returns the absolute paths of every page written (active + terminal
    re-renders), in write order. A return of ``[]`` means nothing changed.
    """
    now = now or datetime.now(UTC)
    auto_dir = wiki_root / AUTO_SUBDIR
    auto_dir.mkdir(parents=True, exist_ok=True)

    active_views = _iter_contracts(conn, NON_TERMINAL_STATES)
    terminal_views = _iter_contracts(conn, TERMINAL_STATES)
    data_root = _data_root_from_conn(conn)

    written: list[Path] = []

    for view in active_views:
        memories = _related_memories(conn, view.contract_id)
        next_action = _handover_next_action(data_root, view.contract_id)
        page = _render_page(
            view,
            memories,
            next_action=next_action,
            is_terminal=False,
            now=now,
        )
        path = auto_dir / f"{view.contract_id}.md"
        _atomic_write(path, page)
        written.append(path)

    for view in terminal_views:
        path = auto_dir / f"{view.contract_id}.md"
        if not path.is_file():
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if now - mtime < timedelta(days=TERMINAL_BANNER_DAYS):
            continue
        memories = _related_memories(conn, view.contract_id)
        next_action = _handover_next_action(data_root, view.contract_id)
        page = _render_page(
            view,
            memories,
            next_action=next_action,
            is_terminal=True,
            now=now,
        )
        _atomic_write(path, page)
        written.append(path)

    return written


__all__ = [
    "AUTO_SUBDIR",
    "MAX_MEMORIES_PER_PAGE",
    "NON_TERMINAL_STATES",
    "TERMINAL_BANNER_DAYS",
    "TERMINAL_STATES",
    "publish_active_contracts",
]
