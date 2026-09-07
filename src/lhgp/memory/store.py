"""P6+1 / memory-and-wiki Phase 2: SQLite CRUD for the ``memories`` table.

The store is intentionally small. All intelligence (what to add, when to
expire, what to retrieve) lives outside the store — in callers like
``MemoryIndex`` and the auto-write hooks in
``longtask/persistence/evaluations.py``. The store is the I/O boundary,
nothing more.

Scope/retrieval rules:
  - ``record_memory`` is the only write path. It is fail-loud: schema
    mismatches raise ``MemoryStoreError``.
  - ``list_memories`` returns memories in score-desc + created-desc order,
    filtered by scope / kind / tag-subset. Callers apply the top-N cut.
  - ``expire_due`` is a maintenance sweep, not a transaction. Callers
    schedule it from the daemon tick.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from lhgp.memory.types import Memory, MemoryKind, MemoryScope


class MemoryStoreError(Exception):
    """Raised when a memory write or read violates the contract.

    This is a fail-closed surface: callers that catch this should refuse
    to proceed, not degrade silently.
    """


def record_memory(
    conn: sqlite3.Connection,
    memory: Memory,
    *,
    now: datetime | None = None,
) -> int:
    """Insert one memory; return the assigned id.

    Sets ``memory.id`` and ``memory.created_at`` as a side effect. Uses
    the explicit ``created_at`` if the caller already set one, otherwise
    fills it from ``now`` (or ``datetime.now(UTC)``).
    """
    now = now or datetime.now(UTC)
    if memory.id is not None:
        raise MemoryStoreError("record_memory called on Memory that already has id")
    row = memory.to_db_row()
    if not row.get("created_at"):
        row["created_at"] = now.isoformat()
    cols = (
        "scope",
        "kind",
        "title",
        "body_md",
        "tags_json",
        "source_contract_id",
        "source_event_id",
        "source_actor",
        "score",
        "created_at",
        "expires_at",
        "schema_version",
    )
    placeholders = ",".join("?" for _ in cols)
    params = tuple(row[c] for c in cols)
    try:
        with conn:
            cur = conn.execute(
                f"INSERT INTO memories ({','.join(cols)}) VALUES ({placeholders})",  # noqa: S608 — cols and placeholders built from hardcoded tuple
                params,
            )
    except sqlite3.IntegrityError as e:
        raise MemoryStoreError(f"failed to record memory: {e}") from e
    if cur.lastrowid is None:
        raise MemoryStoreError("sqlite cursor returned no lastrowid for memories insert")
    memory_id = int(cur.lastrowid)
    # Reflect back to the caller so the in-memory dataclass is consistent.
    object.__setattr__(memory, "id", memory_id)
    object.__setattr__(memory, "created_at", datetime.fromisoformat(row["created_at"]))
    return memory_id


def get_memory(conn: sqlite3.Connection, memory_id: int) -> Memory | None:
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ?",
        (int(memory_id),),
    ).fetchone()
    if row is None:
        return None
    return Memory.from_db_row(row)


def list_memories(
    conn: sqlite3.Connection,
    *,
    scope: MemoryScope | str | None = None,
    kind: MemoryKind | str | None = None,
    tags_any: tuple[str, ...] | None = None,
    include_expired: bool = False,
    limit: int = 100,
) -> list[Memory]:
    """Return memories matching filters, score-desc then created-desc.

    ``tags_any`` returns memories whose tag list contains at least one
    of the given tags (OR-semantics). ``limit`` caps the result.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope.value if isinstance(scope, MemoryScope) else str(scope))
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind.value if isinstance(kind, MemoryKind) else str(kind))
    if not include_expired:
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(datetime.now(UTC).isoformat())
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM memories {where_sql} "  # noqa: S608 — clauses whitelisted
        f"ORDER BY score DESC, created_at DESC LIMIT ?",
        (*params, int(limit)),
    ).fetchall()
    out: list[Memory] = []
    for row in rows:
        m = Memory.from_db_row(row)
        if tags_any is not None and not any(t in m.tags for t in tags_any):
            continue
        out.append(m)
    return out


def search_memories(
    conn: sqlite3.Connection,
    needle: str,
    *,
    scope: MemoryScope | str | None = None,
    limit: int = 50,
) -> list[Memory]:
    """Case-insensitive substring search over title + body + tags.

    The needle is escaped before being wrapped in ``%...%`` so a user
    searching for ``%`` or ``_`` does not get wildcard expansion. A
    raw ``%`` in the search string would otherwise widen the match
    set beyond what the user typed. ``ESCAPE '\\'`` tells SQLite
    that the backslash is the escape character; without it the
    backslashes are literal and the wildcard would still fire.
    """
    escaped = needle.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    clauses = [
        "(LOWER(title) LIKE ? ESCAPE '\\' "
        "OR LOWER(body_md) LIKE ? ESCAPE '\\' "
        "OR LOWER(tags_json) LIKE ? ESCAPE '\\')"
    ]
    params: list[Any] = [like, like, like]
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope.value if isinstance(scope, MemoryScope) else str(scope))
    where_sql = "WHERE " + " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM memories {where_sql} "  # noqa: S608 — clauses whitelisted
        f"ORDER BY score DESC, created_at DESC LIMIT ?",
        (*params, int(limit)),
    ).fetchall()
    return [Memory.from_db_row(r) for r in rows]


def expire_due(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Delete memories whose ``expires_at`` is in the past.

    Returns the list of deleted memory ids so the caller can record
    an audit event with the same ids. Reading first then deleting
    keeps the SQL portable (SQLite does not have ``DELETE ... RETURNING``
    in older builds).
    """
    now = now or datetime.now(UTC)
    with conn:
        cur = conn.execute(
            "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now.isoformat(),),
        )
        ids = [int(row[0]) for row in cur.fetchall()]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",  # noqa: S608 — ids are integers, placeholders count = len(ids)
                ids,
            )
    return ids


def bump_score(
    conn: sqlite3.Connection,
    memory_id: int,
    delta: float,
) -> float | None:
    """Adjust score by ``delta``; return the new score or None if not found."""
    with conn:
        cur = conn.execute(
            "UPDATE memories SET score = score + ? WHERE id = ?",
            (float(delta), int(memory_id)),
        )
    if cur.rowcount == 0:
        return None
    row = conn.execute("SELECT score FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
    return float(row[0]) if row else None


def make_memory(
    title: str,
    body_md: str,
    *,
    tags: tuple[str, ...] = (),
    source_contract_id: str | None = None,
    source_event_id: int | None = None,
    source_actor: str | None = "auto",
    score: float = 0.5,
    expires_in_days: int | None = 180,
    kind: MemoryKind = MemoryKind.PATTERN,
    scope: MemoryScope = MemoryScope.PROJECT,
) -> Memory:
    """Build a :class:`Memory` with sensible defaults.

    ``kind`` and ``scope`` are parameters so the auto-mine hook can emit
    GOTCHA or DOMAIN-flavored records without rebuilding the dataclass.
    The defaults match what a typical "mined pattern" looks like.
    """
    now = datetime.now(UTC)
    if expires_in_days is None:
        expires_at: datetime | None = None
    else:
        expires_at = now + timedelta(days=int(expires_in_days))
    return Memory(
        scope=scope,
        kind=kind,
        title=title,
        body_md=body_md,
        tags=tags,
        source_contract_id=source_contract_id,
        source_event_id=source_event_id,
        source_actor=source_actor,
        score=score,
        created_at=now,
        expires_at=expires_at,
    )


# Backwards-compat alias. The original name implied the kind was always
# PATTERN; after the kind/scope params landed the name stopped reflecting
# the contract. Keep it as a thin alias so older callers and tests
# continue to work.
make_pattern_memory = make_memory


__all__ = [
    "MemoryStoreError",
    "bump_score",
    "expire_due",
    "get_memory",
    "list_memories",
    "make_memory",
    "make_pattern_memory",  # back-compat alias of make_memory
    "record_memory",
    "search_memories",
]
