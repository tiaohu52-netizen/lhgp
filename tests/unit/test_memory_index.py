"""P2: MemoryIndex retrieval tests.

Covers scope selection, capacity dropping, single-oversize truncation,
and the relative ordering of global > domain > project.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.memory.index import MemoryIndex, RetrievedMemory, render_for_active_md
from lhgp.memory.store import record_memory
from lhgp.memory.types import Memory, MemoryKind, MemoryScope

_MEM_NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "state.db"
    c = sqlite3.connect(db)
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


def _insert(
    conn: sqlite3.Connection,
    scope: MemoryScope,
    *,
    title: str = "x",
    body: str = "b",
    score: float = 0.5,
    tags: tuple[str, ...] = (),
    source_contract_id: str | None = None,
) -> int:
    now = datetime.now(UTC)
    m = Memory(
        scope=scope,
        kind=MemoryKind.PATTERN,
        title=title,
        body_md=body,
        tags=tags,
        source_contract_id=source_contract_id,
        created_at=now,
        expires_at=now + timedelta(days=180),
        score=score,
        schema_version=4,
    )
    return record_memory(conn, m)


class TestScopeSelection:
    def test_global_always_included(self, conn: sqlite3.Connection) -> None:
        _insert(conn, MemoryScope.GLOBAL, title="system-rule", score=0.4)
        _insert(conn, MemoryScope.PROJECT, title="unrelated", score=0.9)
        idx = MemoryIndex(conn, top_n=5, budget_bytes=2000)
        out = idx.retrieve(None)
        titles = [r.title for r in out]
        assert "system-rule" in titles
        # PROJECT memory is also fine; both can be in result.
        assert "unrelated" in titles

    def test_domain_scope_matches_tags(self, conn: sqlite3.Connection) -> None:
        _insert(
            conn,
            MemoryScope.DOMAIN,
            title="sql-rule",
            score=0.8,
            tags=("topic/sql",),
        )
        _insert(
            conn,
            MemoryScope.DOMAIN,
            title="mcp-rule",
            score=0.8,
            tags=("topic/mcp",),
        )
        _insert(conn, MemoryScope.PROJECT, title="project-foo", score=0.7)
        idx = MemoryIndex(conn, top_n=10, budget_bytes=4000)
        out = idx.retrieve({"domain": "sql"})
        titles = [r.title for r in out]
        assert "sql-rule" in titles
        assert "mcp-rule" not in titles
        # project-foo is also pulled in (project scope always)
        assert "project-foo" in titles

    def test_score_descending(self, conn: sqlite3.Connection) -> None:
        for i, s in enumerate([0.3, 0.9, 0.5, 0.7]):
            _insert(conn, MemoryScope.PROJECT, title=f"m{i}", score=s)
        idx = MemoryIndex(conn, top_n=10, budget_bytes=2000)
        out = idx.retrieve(None)
        assert [r.title for r in out] == ["m1", "m3", "m2", "m0"]

    def test_expired_excluded(self, conn: sqlite3.Connection) -> None:
        # Insert with explicit past expiry.
        now = datetime.now(UTC)
        m = Memory(
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.PATTERN,
            title="stale",
            body_md="x",
            created_at=now,
            expires_at=now - timedelta(hours=1),
            score=1.0,
            schema_version=4,
        )
        record_memory(conn, m)
        idx = MemoryIndex(conn, top_n=10, budget_bytes=2000)
        out = idx.retrieve(None)
        assert out == []


class TestCapacityContract:
    def test_drops_lowest_score_to_fit(self, conn: sqlite3.Connection) -> None:
        # Section header + 2 memories (body 50 chars) ≈ 232 bytes UTF-8.
        # Budget = 300 fits 2; the next add overflows, so lowest-score drops.
        for i, s in enumerate([0.3, 0.9, 0.5, 0.7]):
            _insert(
                conn,
                MemoryScope.PROJECT,
                title=f"m{i}",
                body="x" * 50,
                score=s,
            )
        idx = MemoryIndex(conn, top_n=10, budget_bytes=300)
        out = idx.retrieve(None)
        # The 2 highest-score entries (m1=0.9, m3=0.7) should be picked.
        titles = [r.title for r in out]
        assert "m1" in titles
        assert "m3" in titles
        assert "m2" not in titles
        assert "m0" not in titles

    def test_single_oversize_memory_truncates(self, conn: sqlite3.Connection) -> None:
        # One huge memory that overflows even the budget on its own.
        _insert(
            conn,
            MemoryScope.PROJECT,
            title="huge",
            body="x" * 10000,
            score=1.0,
        )
        idx = MemoryIndex(conn, top_n=10, budget_bytes=500)
        out = idx.retrieve(None)
        # It still shows up (truncated, not dropped).
        assert len(out) == 1
        assert "[…truncated…]" in out[0].body_md
        # The truncated body is at most ~budget_bytes wide.
        assert len(out[0].body_md) <= 500

    def test_top_n_caps_initial_pull(self, conn: sqlite3.Connection) -> None:
        # 20 memories, top_n=3 → at most 3 in result (before capacity).
        for i in range(20):
            _insert(conn, MemoryScope.PROJECT, title=f"m{i}", score=0.5)
        idx = MemoryIndex(conn, top_n=3, budget_bytes=10000)
        out = idx.retrieve(None)
        assert len(out) == 3


class TestRenderForActiveMd:
    def test_empty(self) -> None:
        assert render_for_active_md([]) == ""

    def test_renders_section_header(self) -> None:
        r = RetrievedMemory(
            title="t", body_md="b", kind="pattern", score=0.5, tags=(), source="auto"
        )
        text = render_for_active_md([r])
        assert "## 长期记忆" in text
        assert "**t**" in text
        assert "pattern" in text
        assert "auto" in text

    def test_renders_tags_when_present(self) -> None:
        r = RetrievedMemory(
            title="t", body_md="b", kind="pattern", score=0.5, tags=("topic/sql",), source="auto"
        )
        text = render_for_active_md([r])
        assert "#topic/sql" in text


class TestSeparatorAccounting:
    """多条目时渲染结果必须真的落在预算内（分隔符要算进去）。

    回归：``_fit_to_budget`` 曾按「每项 +1 字节」记账，而 ``_render_section``
    实际用 ``"\\n\\n"``（2 字节）连接相邻项、末尾再加 1 字节换行。n 项时
    记账少算 ``n-1`` 字节，于是「装得下」的结论在 n≥2 时是错的：3 项实测
    溢出 2 字节。模块 docstring 承诺的容量合同因此在多条目场景下失效。
    """

    @pytest.mark.parametrize("budget", [140, 200, 260, 320, 420, 600])
    def test_rendered_section_never_exceeds_budget(
        self, conn: sqlite3.Connection, budget: int
    ) -> None:
        for i in range(6):
            _insert(
                conn,
                MemoryScope.PROJECT,
                title=f"m{i}",
                body="y" * 30,
                score=1.0 - i * 0.1,
            )
        idx = MemoryIndex(conn, top_n=10, budget_bytes=budget)
        out = idx.retrieve(None)
        rendered = render_for_active_md(out)
        assert len(rendered.encode("utf-8")) <= budget, (
            f"budget={budget} kept={len(out)} rendered={len(rendered.encode('utf-8'))}"
        )


class TestRecordMemoryDoesNotCommitOuterTransaction:
    """store 层的写入不得提交调用方的事务。

    回归：``record_memory`` / ``expire_due`` / ``bump_score`` 用的是裸
    ``with conn:``，它会**提交**任何正在进行的外层事务。
    ``feedback.record_evaluation`` 正是在调用方自己的事务里调
    ``record_memory`` 的，于是评价行被提前落库，与随后的
    USER_EVALUATION_SUBMITTED 事件不再原子——中途失败会留下"有评价、
    没事件"的半截状态。
    """

    def _new_conn(self, tmp_path: Path):
        from longtask.persistence.store import StoreConfig, connect, ensure_schema

        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        ensure_schema(conn)
        return conn

    def test_rollback_after_record_memory_leaves_nothing(self, tmp_path: Path) -> None:
        from lhgp.memory.store import list_memories, record_memory
        from lhgp.memory.types import Memory, MemoryKind, MemoryScope
        from longtask.persistence.schema import transaction

        conn = self._new_conn(tmp_path)
        try:
            with transaction(conn):
                record_memory(
                    conn,
                    Memory(
                        scope=MemoryScope.PROJECT,
                        kind=MemoryKind.PATTERN,
                        title="doomed",
                        body_md="should roll back",
                        tags=(),
                        source_contract_id=None,
                        created_at=_MEM_NOW,
                        expires_at=_MEM_NOW + timedelta(days=180),
                        score=0.5,
                    ),
                )
                conn.rollback()
            assert list_memories(conn) == []
        finally:
            conn.close()

    def test_commit_still_persists(self, tmp_path: Path) -> None:
        """修的是"提前提交"，不是"不提交"——正常路径必须照常落库。"""
        from lhgp.memory.store import list_memories, record_memory
        from lhgp.memory.types import Memory, MemoryKind, MemoryScope
        from longtask.persistence.schema import transaction

        conn = self._new_conn(tmp_path)
        try:
            with transaction(conn):
                record_memory(
                    conn,
                    Memory(
                        scope=MemoryScope.PROJECT,
                        kind=MemoryKind.PATTERN,
                        title="kept",
                        body_md="stays",
                        tags=(),
                        source_contract_id=None,
                        created_at=_MEM_NOW,
                        expires_at=_MEM_NOW + timedelta(days=180),
                        score=0.5,
                    ),
                )
            assert len(list_memories(conn)) == 1
        finally:
            conn.close()

    def test_exception_in_outer_transaction_rolls_back(self, tmp_path: Path) -> None:
        from lhgp.memory.store import list_memories, record_memory
        from lhgp.memory.types import Memory, MemoryKind, MemoryScope
        from longtask.persistence.schema import transaction

        conn = self._new_conn(tmp_path)
        try:
            with pytest.raises(RuntimeError), transaction(conn):
                record_memory(
                    conn,
                    Memory(
                        scope=MemoryScope.PROJECT,
                        kind=MemoryKind.PATTERN,
                        title="doomed2",
                        body_md="x",
                        tags=(),
                        source_contract_id=None,
                        created_at=_MEM_NOW,
                        expires_at=_MEM_NOW + timedelta(days=180),
                        score=0.5,
                    ),
                )
                raise RuntimeError("boom")
            assert list_memories(conn) == []
        finally:
            conn.close()
