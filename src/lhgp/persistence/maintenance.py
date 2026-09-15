"""Contract revision diff and terminal-event pruning (maintenance reads).

diff_revisions gives humans and agents a field-level view of what changed
between two immutable revision snapshots - the "what did the other side
change" answer for contract terms.

prune_terminal_events deletes event rows belonging to terminal contracts
older than a retention window.  It exists because the events table grows
without bound (audit R3); the dry-run default keeps deletion explicit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from lhgp.contracts.state_machine import TERMINAL_STATES

# Fields compared by diff_revisions, in report order.
#
# 必须与 ``_load_revision`` 的 SELECT 逐字对齐（``zip(..., strict=True)``
# 会当场拦下错位）。此前这份清单漏了修订表里另外 7 个语义列，于是
# 「改授权 / 改执行策略 / 改自动批准 / 改注意力与连续性策略」这些
# **真正影响调度与权限**的修订在 diff 里完全不显示——用户以为合同没变。
_DIFF_FIELDS = (
    "title",
    "objective",
    "deadline_at",
    "state",
    "deadline_status",
    "acceptance_status",
    "blocked_reason",
    "workload_initial_hours",
    "hard_constraints_json",
    "acceptance_json",
    "budget_json",
    "soft_guidance_json",
    "context_json",
    "execution_json",
    "client_meta_json",
    "authority_json",
    "attention_json",
    "continuity_json",
    "auto_approve_json",
)


def _load_revision(
    conn: sqlite3.Connection, contract_id: str, revision: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT title, objective, deadline_at, state, deadline_status,"
        " acceptance_status, blocked_reason, workload_initial_hours,"
        " hard_constraints_json, acceptance_json, budget_json, soft_guidance_json,"
        " context_json, execution_json, client_meta_json, authority_json,"
        " attention_json, continuity_json, auto_approve_json"
        " FROM contract_revisions WHERE contract_id = ? AND revision = ?",
        (contract_id, revision),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_DIFF_FIELDS, row, strict=True))


def diff_revisions(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    from_revision: int,
    to_revision: int,
) -> dict[str, Any]:
    """Field-level diff between two immutable revision snapshots.

    Mutable-zone values (acceptance/soft_guidance/budget) are compared as
    parsed JSON so key order does not show up as a change.
    """
    before = _load_revision(conn, contract_id, from_revision)
    after = _load_revision(conn, contract_id, to_revision)
    if before is None:
        return {"found": False, "revision": from_revision}
    if after is None:
        return {"found": False, "revision": to_revision}
    changes: list[dict[str, Any]] = []
    for field in _DIFF_FIELDS:
        old_value = before[field]
        new_value = after[field]
        if field.endswith("_json"):
            try:
                if json.loads(old_value or "{}") == json.loads(new_value or "{}"):
                    continue
                old_show: Any = json.loads(old_value or "{}")
                new_show: Any = json.loads(new_value or "{}")
            except ValueError:
                if old_value == new_value:
                    continue
                old_show, new_show = old_value, new_value
        elif old_value == new_value:
            continue
        else:
            old_show, new_show = old_value, new_value
        changes.append({"field": field, "from": old_show, "to": new_show})
    return {
        "found": True,
        "contract_id": contract_id,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "changed": bool(changes),
        "changes": changes,
    }


def prune_terminal_events(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    keep_days: int,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Delete events of TERMINAL contracts older than the retention window.

    Only contracts already in a terminal state are touched; active history
    is never pruned.  Returns the candidate count; with dry_run=True
    nothing is deleted.
    """
    if keep_days < 0:
        raise ValueError("keep_days must be >= 0")
    cutoff = (now - timedelta(days=keep_days)).isoformat()
    # 终态集合是固定枚举，占位符数量随之恒定；排序保证两条语句一致
    terminal_states = tuple(sorted(s.value for s in TERMINAL_STATES))
    placeholders = ", ".join("?" * len(terminal_states))
    params = (*terminal_states, cutoff)
    count_sql = (
        "SELECT COUNT(*), COALESCE(MIN(created_at), '') FROM events"  # noqa: S608
        " WHERE contract_id IN (SELECT contract_id FROM contracts"
        f" WHERE state IN ({placeholders})) AND created_at < ?"
    )
    row = conn.execute(count_sql, params).fetchone()
    candidates = int(row[0])
    oldest = row[1] or None
    deleted = 0
    if not dry_run and candidates:
        delete_sql = (
            "DELETE FROM events WHERE contract_id IN (SELECT contract_id"  # noqa: S608
            " FROM contracts WHERE state IN (" + placeholders + ")) AND created_at < ?"
        )
        cur = conn.execute(delete_sql, params)
        deleted = cur.rowcount
        conn.commit()
    return {
        "cutoff": cutoff,
        "candidate_events": candidates,
        "oldest_created_at": oldest,
        "deleted": deleted,
        "dry_run": dry_run,
    }


__all__ = ["diff_revisions", "prune_terminal_events"]
