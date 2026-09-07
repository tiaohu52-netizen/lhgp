"""P2: memory SQLite store tests.

Covers record / list / search / get / expire / bump_score and the
tuple-vs-Row compat paths that the P6 round tripped on.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.memory.store import (
    MemoryStoreError,
    bump_score,
    expire_due,
    get_memory,
    list_memories,
    make_pattern_memory,
    record_memory,
    search_memories,
)
from lhgp.memory.types import Memory, MemoryKind, MemoryScope

pytestmark = pytest.mark.unit


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "state.db"
    c = sqlite3.connect(db)
    # Minimal v4 schema: only the columns the store uses
    c.execute("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body_md TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_contract_id TEXT,
            source_event_id INTEGER,
            source_actor TEXT,
            score REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            schema_version INTEGER NOT NULL DEFAULT 4
        )
    """)
    c.commit()
    yield c
    c.close()


def _make(scope: MemoryScope, kind: MemoryKind, **kw: object) -> Memory:
    now = datetime.now(UTC)
    return Memory(
        scope=scope,
        kind=kind,
        title=str(kw.get("title", "test")),
        body_md=str(kw.get("body_md", "body")),
        tags=tuple(kw.get("tags", ())),
        source_contract_id=kw.get("source_contract_id"),
        source_event_id=kw.get("source_event_id"),
        source_actor=kw.get("source_actor"),
        score=float(kw.get("score", 0.5)),  # type: ignore[arg-type]
        created_at=now,
        expires_at=kw.get("expires_at"),
        schema_version=4,
    )


class TestRecordMemory:
    def test_records_and_assigns_id(self, conn: sqlite3.Connection) -> None:
        m = _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="x")
        assert m.id is None
        rid = record_memory(conn, m)
        assert rid == 1
        assert m.id == 1
        # Round-trip
        got = get_memory(conn, 1)
        assert got is not None
        assert got.title == "x"
        assert got.scope is MemoryScope.PROJECT

    def test_rejects_double_record(self, conn: sqlite3.Connection) -> None:
        m = _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="x")
        record_memory(conn, m)
        # The second call sees m.id already set and refuses.
        with pytest.raises(MemoryStoreError):
            record_memory(conn, m)

    def test_id_reflects_back_to_dataclass(self, conn: sqlite3.Connection) -> None:
        m = _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="x")
        record_memory(conn, m)
        assert m.id == 1


class TestListMemories:
    def test_returns_score_desc(self, conn: sqlite3.Connection) -> None:
        for i, score in enumerate([0.3, 0.9, 0.5, 0.7]):
            record_memory(
                conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title=f"m{i}", score=score)
            )
        mems = list_memories(conn)
        assert [m.title for m in mems] == ["m1", "m3", "m2", "m0"]
        assert [m.score for m in mems] == [0.9, 0.7, 0.5, 0.3]

    def test_filter_by_scope(self, conn: sqlite3.Connection) -> None:
        record_memory(conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="p"))
        record_memory(conn, _make(MemoryScope.GLOBAL, MemoryKind.RULE, title="g"))
        assert {m.title for m in list_memories(conn, scope=MemoryScope.PROJECT)} == {"p"}
        assert {m.title for m in list_memories(conn, scope=MemoryScope.GLOBAL)} == {"g"}

    def test_filter_by_kind(self, conn: sqlite3.Connection) -> None:
        record_memory(conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="p"))
        record_memory(conn, _make(MemoryScope.PROJECT, MemoryKind.GOTCHA, title="g"))
        assert {m.title for m in list_memories(conn, kind=MemoryKind.GOTCHA)} == {"g"}

    def test_filter_by_tags_any(self, conn: sqlite3.Connection) -> None:
        record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="a", tags=("topic/sql",))
        )
        record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="b", tags=("topic/mcp",))
        )
        record_memory(
            conn,
            _make(
                MemoryScope.PROJECT, MemoryKind.PATTERN, title="c", tags=("topic/sql", "topic/mcp")
            ),
        )
        out = list_memories(conn, tags_any=("topic/sql",))
        assert {m.title for m in out} == {"a", "c"}

    def test_excludes_expired_by_default(self, conn: sqlite3.Connection) -> None:
        past = datetime.now(UTC) - timedelta(days=1)
        record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="e", expires_at=past)
        )
        assert list_memories(conn) == []
        # include_expired=True brings it back
        assert len(list_memories(conn, include_expired=True)) == 1

    def test_limit(self, conn: sqlite3.Connection) -> None:
        for i in range(5):
            record_memory(conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title=f"m{i}"))
        assert len(list_memories(conn, limit=3)) == 3


class TestSearchMemories:
    def test_title_match(self, conn: sqlite3.Connection) -> None:
        record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="tuple/Row compat")
        )
        record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="schema migration")
        )
        out = search_memories(conn, "tuple")
        assert len(out) == 1
        assert out[0].title == "tuple/Row compat"

    def test_body_match(self, conn: sqlite3.Connection) -> None:
        record_memory(
            conn,
            _make(
                MemoryScope.PROJECT,
                MemoryKind.PATTERN,
                title="x",
                body_md="cursor.fetchall() returns plain tuple",
            ),
        )
        out = search_memories(conn, "fetchall")
        assert len(out) == 1

    def test_tag_match(self, conn: sqlite3.Connection) -> None:
        record_memory(
            conn,
            _make(
                MemoryScope.PROJECT,
                MemoryKind.PATTERN,
                title="x",
                tags=("topic/sql",),
                body_md="body",
            ),
        )
        out = search_memories(conn, "sql")
        assert len(out) == 1

    def test_case_insensitive(self, conn: sqlite3.Connection) -> None:
        record_memory(
            conn,
            _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="Tuple"),
        )
        out = search_memories(conn, "tuple")
        assert len(out) == 1

    def test_scope_filter(self, conn: sqlite3.Connection) -> None:
        record_memory(conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="p"))
        record_memory(conn, _make(MemoryScope.GLOBAL, MemoryKind.RULE, title="g"))
        out = search_memories(conn, "p", scope=MemoryScope.PROJECT)
        assert len(out) == 1
        out = search_memories(conn, "g", scope=MemoryScope.PROJECT)
        assert out == []


class TestExpireAndBump:
    def test_expire_due_deletes_past(self, conn: sqlite3.Connection) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        future = datetime.now(UTC) + timedelta(hours=1)
        past_id = record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="e", expires_at=past)
        )
        record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="f", expires_at=future)
        )
        deleted = expire_due(conn)
        assert deleted == [past_id]
        assert {m.title for m in list_memories(conn, include_expired=True)} == {"f"}

    def test_expire_due_no_op_when_none_due(self, conn: sqlite3.Connection) -> None:
        record_memory(
            conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="x", expires_at=None)
        )
        record_memory(
            conn,
            _make(
                MemoryScope.PROJECT,
                MemoryKind.PATTERN,
                title="y",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
        assert expire_due(conn) == []

    def test_bump_score_returns_new_score(self, conn: sqlite3.Connection) -> None:
        record_memory(conn, _make(MemoryScope.PROJECT, MemoryKind.PATTERN, title="x", score=0.5))
        new = bump_score(conn, 1, 0.2)
        assert new == pytest.approx(0.7, abs=1e-9)

    def test_bump_score_unknown_id_returns_none(self, conn: sqlite3.Connection) -> None:
        assert bump_score(conn, 9999, 0.1) is None


class TestMakePatternMemory:
    def test_builds_pattern_with_sensible_defaults(self) -> None:
        m = make_pattern_memory(title="t", body_md="b")
        assert m.scope is MemoryScope.PROJECT
        assert m.kind is MemoryKind.PATTERN
        assert m.title == "t"
        assert m.body_md == "b"
        assert m.source_actor == "auto"
        assert m.score == 0.5
        # Default expires_in_days=180 → created_at + 180 days
        assert (m.expires_at - m.created_at).days == 180  # type: ignore[operator]

    def test_no_expire(self) -> None:
        m = make_pattern_memory(title="t", body_md="b", expires_in_days=None)
        assert m.expires_at is None

    def test_zero_days_is_immediate_not_none(self) -> None:
        # ``expires_in_days=0`` must mean "expires now", not "no
        # expiry" (the truthy ``if expires_in_days`` check used to
        # collapse 0 to None).
        m = make_pattern_memory(title="t", body_md="b", expires_in_days=0)
        assert m.expires_at is not None
        assert (m.expires_at - m.created_at).total_seconds() < 1  # type: ignore[operator]

    def test_alias_make_memory_is_same(self) -> None:
        from lhgp.memory.store import make_memory, make_pattern_memory

        # The back-compat alias points at the same function so callers
        # that still use the old name get the new behavior.
        assert make_memory is make_pattern_memory
        a = make_memory(title="t", body_md="b", expires_in_days=0)
        b = make_pattern_memory(title="t", body_md="b", expires_in_days=0)
        assert a.expires_at is not None
        assert b.expires_at is not None
