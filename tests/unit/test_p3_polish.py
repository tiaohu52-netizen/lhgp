"""P3 / non-blocking polish items from the 4-agent review.

Each test class is a single fix:
  - TestSearchMemoryLikeEscape      LIKE wildcard escape
  - TestMemoryBodySizeCap           64 KiB body cap on Memory
  - TestWalkSourceSizeGuard         2 MiB source cap on walk_source
  - TestMermaidFenceInit            ``\\`\\`\\`mermaid {theme: ...}`` accepted
  - TestExcalidrawContainerId        text containerId binding
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lhgp.flow import walk_source
from lhgp.flow.wiki_parser import extract_flow_section
from lhgp.memory import Memory, MemoryKind, MemoryScope

pytestmark = pytest.mark.unit


class TestSearchMemoryLikeEscape:
    """P3 security: a user-supplied ``%`` or ``_`` in the search needle
    must not act as a SQL LIKE wildcard. Otherwise ``%foo`` matches
    every row, and a malicious user can probe the table."""

    @pytest.fixture
    def conn(self, tmp_path):
        import sqlite3

        c = sqlite3.connect(tmp_path / "state.db")
        c.executescript(
            """
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
            );
            """
        )
        c.commit()
        return c

    def _setup(self, conn) -> None:
        from lhgp.memory import record_memory

        record_memory(
            conn,
            Memory(
                scope=MemoryScope.PROJECT,
                kind=MemoryKind.PATTERN,
                title="needle-target",
                body_md="matches literal `100%`",
                score=0.5,
                created_at=datetime.now(UTC),
                schema_version=4,
            ),
        )
        record_memory(
            conn,
            Memory(
                scope=MemoryScope.PROJECT,
                kind=MemoryKind.PATTERN,
                title="decoy",
                body_md="unrelated",
                score=0.5,
                created_at=datetime.now(UTC),
                schema_version=4,
            ),
        )

    def test_percent_in_needle_matches_literal_percent(self, tmp_path, conn) -> None:
        from lhgp.memory import search_memories

        self._setup(conn)
        # With ESCAPE '\\' set, a user searching for literal "%"
        # should match strings containing "%" (target has "100%")
        # and skip strings without it (decoy). The pre-fix behavior
        # was a wildcard match on every row.
        results = search_memories(conn, "%")
        titles = {m.title for m in results}
        assert "needle-target" in titles
        assert "decoy" not in titles

    def test_decoy_not_reachable_via_wildcard(self, tmp_path, conn) -> None:
        # A needle with no special chars should match the decoy by
        # its body, and the pre-fix wildcard trick ("") would
        # return everything. Post-fix it returns just the literal
        # match.
        from lhgp.memory import search_memories

        self._setup(conn)
        results = search_memories(conn, "unrelated")
        assert {m.title for m in results} == {"decoy"}

    def test_literal_percent_matches_body(self, tmp_path, conn) -> None:
        from lhgp.memory import search_memories

        self._setup(conn)
        results = search_memories(conn, "100%")
        # The literal "100%" only appears in the target row.
        assert len(results) == 1
        assert results[0].title == "needle-target"


class TestMemoryBodySizeCap:
    """P3 security: refuse a Memory whose body_md exceeds 64 KiB so
    a runaway record cannot bloat the SQLite row or the rendered
    MemoryIndex section."""

    def test_oversize_body_raises(self) -> None:
        big = "x" * (65 * 1024)  # 65 KiB
        with pytest.raises(ValueError, match="exceeds 65536 bytes"):
            Memory(
                scope=MemoryScope.PROJECT,
                kind=MemoryKind.PATTERN,
                title="too-big",
                body_md=big,
                schema_version=4,
            )

    def test_exact_cap_accepted(self) -> None:
        ok = "x" * (64 * 1024)  # exactly 64 KiB
        m = Memory(
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.PATTERN,
            title="at-cap",
            body_md=ok,
            schema_version=4,
        )
        assert m.title == "at-cap"


class TestWalkSourceSizeGuard:
    """P3 DoS guard: a 2 MiB Python file pushes ``ast.parse`` into
    GB-scale intermediate representations. The walker should refuse
    rather than OOM the process."""

    def test_oversize_source_rejected(self) -> None:
        big = "x = 1\n" * (400_000)  # ~2.4 MB
        assert len(big.encode("utf-8")) > 2 * 1024 * 1024
        with pytest.raises(ValueError, match="exceeds"):
            walk_source(big, "huge")

    def test_normal_source_accepted(self) -> None:
        flow = walk_source("def a(): return b()\n", "demo")
        assert any(n.id == "demo.a" for n in flow.nodes)


class TestMermaidFenceInit:
    """P3 parser: ``\\`\\`\\`mermaid {theme: neutral}`` (Mermaid's
    same-line init block) is supported by the official renderer; the
    parser must accept it instead of silently dropping the body."""

    def test_fence_with_init_block(self) -> None:
        page = "## flow\n\n```mermaid {theme: neutral}\nflowchart TD\n  A --> B\n```\n"
        sec = extract_flow_section(page, page="x")
        assert sec is not None
        assert "flowchart TD" in sec.mermaid
        assert sec.init == "{theme: neutral}"

    def test_fence_without_init_block(self) -> None:
        page = "## flow\n\n```mermaid\nflowchart TD\nA --> B\n```\n"
        sec = extract_flow_section(page, page="x")
        assert sec is not None
        assert sec.init == ""


class TestExcalidrawContainerId:
    """P3 schema completeness: text elements must carry ``containerId``
    pointing back at their rectangle so Excalidraw's import / move
    semantics follow the container (instead of leaving the text
    floating when the rect moves)."""

    def test_text_binds_container_id(self) -> None:
        from lhgp.flow import Flow, FlowNode
        from lhgp.flow.render_excalidraw import render_excalidraw

        flow = Flow(
            title="t",
            nodes=(FlowNode(id="a", label="alpha", kind="function"),),
            edges=(),
            source="ast:t",
        )
        scene = render_excalidraw(flow)
        elements = scene["elements"]
        rects = [e for e in elements if e["type"] == "rectangle"]
        texts = [e for e in elements if e["type"] == "text"]
        assert rects[0]["id"] == "node_0"
        assert texts[0]["id"] == "text_0"
        assert texts[0]["containerId"] == "node_0"
        # Both also report the new fields so the schema matches the
        # 2024 Excalidraw element spec.
        assert rects[0]["strokeStyle"] == "solid"
        assert texts[0]["strokeStyle"] == "solid"
