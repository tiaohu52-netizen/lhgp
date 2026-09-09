"""Focused tests for ``lhgp.memory.cli.memory_command``.

The store layer has had coverage since P2; the CLI surface on top of it
did not, and the CLI is where the exit codes live.  Exit codes are the
contract: ``0`` = did what was asked, ``1`` = nothing found / store
rejected it, ``2`` = the *user* got the invocation wrong.  A future
change that collapses 2 into 1 breaks scripting against ``lhgp memory``
without breaking any assertion about printed text, so each path pins the
number, not just the message.
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp.memory.cli import memory_command
from lhgp.memory.store import MemoryStoreError, record_memory
from lhgp.memory.types import Memory, MemoryKind, MemoryScope

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


_MEM_DEFAULTS: dict[str, Any] = {
    "memory_cmd": "list",
    "title": None,
    "body": None,
    "scope": None,
    "kind": None,
    "tags": "",
    "score": 0.5,
    "source_contract": None,
    "source_event_id": None,
    "actor": "user",
    "expires_in_days": 180,
    "no_expire": False,
    "tags_any": None,
    "include_expired": False,
    "limit": 50,
    "json": False,
    "keyword": None,
    "id": None,
}


def _ns(**overrides: Any) -> argparse.Namespace:
    return argparse.Namespace(**{**_MEM_DEFAULTS, **overrides})


def _seed(
    conn: sqlite3.Connection,
    *,
    title: str = "retry policy",
    body: str = "executor timeouts retry twice",
    scope: MemoryScope = MemoryScope.PROJECT,
    kind: MemoryKind = MemoryKind.PATTERN,
    tags: tuple[str, ...] = (),
    score: float = 0.0,
    expires_at: datetime | None = None,
    source_contract_id: str | None = None,
) -> int:
    now = datetime.now(UTC)
    return record_memory(
        conn,
        Memory(
            scope=scope,
            kind=kind,
            title=title,
            body_md=body,
            tags=tags,
            source_contract_id=source_contract_id,
            source_event_id=None,
            source_actor="daemon",
            score=score,
            created_at=now,
            expires_at=expires_at,
        ),
    )


class TestAdd:
    def test_add_records_and_prints_id(self, conn: sqlite3.Connection, capsys) -> None:
        rc = memory_command(
            conn,
            _ns(
                memory_cmd="add",
                title="gate lesson",
                body="always pin the sha",
                scope="project",
                kind="gotcha",
                tags="ci, release",
                score=0.8,
            ),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("recorded memory id=")
        mid = int(out.rsplit("=", 1)[1].strip())
        row = conn.execute("SELECT title, tags_json, score FROM memories WHERE id = ?", (mid,))
        _, tags_json, score = row.fetchone()
        assert json.loads(tags_json) == ["ci", "release"]
        assert score == pytest.approx(0.8)

    def test_blank_title_is_a_usage_error(self, conn: sqlite3.Connection, capsys) -> None:
        rc = memory_command(
            conn, _ns(memory_cmd="add", title="   ", body="x", scope="project", kind="gotcha")
        )
        assert rc == 2
        assert "--title is required" in capsys.readouterr().err

    def test_blank_body_is_a_usage_error(self, conn: sqlite3.Connection, capsys) -> None:
        rc = memory_command(
            conn,
            _ns(memory_cmd="add", title="t", body="   ", scope="project", kind="gotcha"),
        )
        assert rc == 2
        assert "--body (or stdin) is required" in capsys.readouterr().err

    def test_body_falls_back_to_stdin(
        self, conn: sqlite3.Connection, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("从标准输入读入的正文\n第二行\n"))
        rc = memory_command(
            conn,
            _ns(
                memory_cmd="add",
                title="from stdin",
                body=None,
                scope="project",
                kind="heuristic",
            ),
        )
        assert rc == 0
        capsys.readouterr()
        mid = _id_of_last(conn)
        stored = conn.execute("SELECT body_md FROM memories WHERE id = ?", (mid,)).fetchone()[0]
        assert "从标准输入读入的正文" in str(stored)

    def test_empty_stdin_still_errors(
        self, conn: sqlite3.Connection, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
        rc = memory_command(
            conn,
            _ns(
                memory_cmd="add",
                title="t",
                body=None,
                scope="project",
                kind="heuristic",
            ),
        )
        assert rc == 2

    @pytest.mark.parametrize(("field", "value"), [("scope", "galaxy"), ("kind", "vibe")])
    def test_bad_enum_is_a_usage_error(
        self, conn: sqlite3.Connection, capsys, field: str, value: str
    ) -> None:
        overrides: dict[str, Any] = {"memory_cmd": "add", "title": "t", "body": "b"}
        overrides["scope"] = value if field == "scope" else "project"
        overrides["kind"] = value if field == "kind" else "pattern"
        rc = memory_command(conn, _ns(**overrides))
        assert rc == 2
        assert "error:" in capsys.readouterr().err

    def test_no_expire_leaves_expiry_null(self, conn: sqlite3.Connection, capsys) -> None:
        rc = memory_command(
            conn,
            _ns(
                memory_cmd="add",
                title="durable",
                body="keeps working",
                scope="global",
                kind="pattern",
                no_expire=True,
            ),
        )
        assert rc == 0
        capsys.readouterr()
        expires = conn.execute("SELECT expires_at FROM memories").fetchone()[0]
        assert expires is None

    def test_expires_in_days_is_applied(self, conn: sqlite3.Connection, capsys) -> None:
        rc = memory_command(
            conn,
            _ns(
                memory_cmd="add",
                title="short lived",
                body="soon stale",
                scope="global",
                kind="pattern",
                expires_in_days=3,
            ),
        )
        assert rc == 0
        capsys.readouterr()
        raw = conn.execute("SELECT expires_at FROM memories").fetchone()[0]
        assert raw is not None
        remaining = datetime.fromisoformat(str(raw)) - datetime.now(UTC)
        assert timedelta(days=2) < remaining <= timedelta(days=3)

    def test_store_failure_is_exit_one(
        self, conn: sqlite3.Connection, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> int:
            raise MemoryStoreError("simulated store failure")

        monkeypatch.setattr("lhgp.memory.cli.record_memory", _boom)
        rc = memory_command(
            conn,
            _ns(memory_cmd="add", title="t", body="b", scope="project", kind="pattern"),
        )
        assert rc == 1
        assert "simulated store failure" in capsys.readouterr().err


def _id_of_last(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) FROM memories").fetchone()
    assert row[0] is not None
    return int(row[0])


class TestList:
    def test_empty_list_prints_placeholder(self, conn: sqlite3.Connection, capsys) -> None:
        assert memory_command(conn, _ns(memory_cmd="list")) == 0
        assert capsys.readouterr().out.strip() == "(no memories)"

    def test_human_output_carries_tags_score_source_and_expiry(
        self, conn: sqlite3.Connection, capsys
    ) -> None:
        mid = _seed(
            conn,
            title="lease fencing",
            body="line one\nline two",
            tags=("lease", "safety"),
            score=0.75,
            source_contract_id="c-42",
            expires_at=datetime.now(UTC) + timedelta(days=9),
        )
        assert memory_command(conn, _ns(memory_cmd="list")) == 0
        out = capsys.readouterr().out
        assert f"[{mid}]" in out
        assert "project/pattern  lease fencing" in out
        assert "tags: #lease, #safety" in out
        assert "score: 0.750" in out
        assert "source: daemon:c-42" in out
        assert "expires:" in out
        assert "line one" in out

    def test_zero_score_and_no_source_lines_are_omitted(
        self, conn: sqlite3.Connection, capsys
    ) -> None:
        _seed(conn, title="quiet", body="b", score=0.0)
        assert memory_command(conn, _ns(memory_cmd="list")) == 0
        out = capsys.readouterr().out
        assert "score:" not in out
        assert "source:" not in out
        assert "tags:" not in out

    def test_long_bodies_are_truncated_to_six_lines(self, conn: sqlite3.Connection, capsys) -> None:
        _seed(conn, title="many lines", body="\n".join(f"L{i}" for i in range(10)))
        memory_command(conn, _ns(memory_cmd="list"))
        out = capsys.readouterr().out
        assert "L5" in out
        assert "L6" not in out

    def test_json_mode_emits_parsable_rows(self, conn: sqlite3.Connection, capsys) -> None:
        _seed(conn, title="structured")
        assert memory_command(conn, _ns(memory_cmd="list", json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert payload[0]["title"] == "structured"

    def test_filters_are_forwarded(self, conn: sqlite3.Connection, capsys) -> None:
        _seed(conn, title="proj one", scope=MemoryScope.PROJECT, tags=("a",))
        _seed(conn, title="glob one", scope=MemoryScope.GLOBAL, kind=MemoryKind.GOTCHA, tags=("b",))
        capsys.readouterr()
        memory_command(conn, _ns(memory_cmd="list", scope="global", kind="gotcha", tags_any=" b "))
        out = capsys.readouterr().out
        assert "glob one" in out
        assert "proj one" not in out

    @pytest.mark.parametrize(("field", "value"), [("scope", "nope"), ("kind", "nope")])
    def test_bad_filter_enum_is_exit_two(
        self, conn: sqlite3.Connection, capsys, field: str, value: str
    ) -> None:
        overrides: dict[str, Any] = {"memory_cmd": "list", field: value}
        assert memory_command(conn, _ns(**overrides)) == 2
        assert "error:" in capsys.readouterr().err

    def test_include_expired_shows_rows_that_list_hides(
        self, conn: sqlite3.Connection, capsys
    ) -> None:
        _seed(conn, title="gone", expires_at=datetime.now(UTC) - timedelta(days=1))
        assert memory_command(conn, _ns(memory_cmd="list")) == 0
        assert "gone" not in capsys.readouterr().out
        assert memory_command(conn, _ns(memory_cmd="list", include_expired=True)) == 0
        assert "gone" in capsys.readouterr().out


class TestSearchShowExpire:
    def test_search_hit_returns_zero(self, conn: sqlite3.Connection, capsys) -> None:
        _seed(conn, title="verifier contract", body="evidence must bind the payload")
        capsys.readouterr()
        assert memory_command(conn, _ns(memory_cmd="search", keyword="verifier")) == 0
        assert "verifier contract" in capsys.readouterr().out

    def test_search_miss_returns_one(self, conn: sqlite3.Connection, capsys) -> None:
        _seed(conn, title="verifier contract")
        capsys.readouterr()
        assert memory_command(conn, _ns(memory_cmd="search", keyword="nothingmatches")) == 1
        assert capsys.readouterr().out.strip() == "(no memories)"

    def test_search_honours_scope(self, conn: sqlite3.Connection, capsys) -> None:
        _seed(conn, title="needle here", scope=MemoryScope.GLOBAL)
        capsys.readouterr()
        rc = memory_command(
            conn, _ns(memory_cmd="search", keyword="needle", scope=MemoryScope.PROJECT.value)
        )
        assert rc == 1

    def test_show_prints_a_single_row(self, conn: sqlite3.Connection, capsys) -> None:
        mid = _seed(conn, title="shown one", body="正文内容")
        capsys.readouterr()
        assert memory_command(conn, _ns(memory_cmd="show", id=mid)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == mid
        assert payload["title"] == "shown one"
        assert payload["body_md"] == "正文内容"

    def test_show_without_id_is_exit_two(self, conn: sqlite3.Connection, capsys) -> None:
        assert memory_command(conn, _ns(memory_cmd="show", id=None)) == 2
        assert "--id is required" in capsys.readouterr().err

    def test_show_unknown_id_is_exit_one(self, conn: sqlite3.Connection, capsys) -> None:
        assert memory_command(conn, _ns(memory_cmd="show", id=999)) == 1
        assert "999 not found" in capsys.readouterr().out

    def test_expire_reports_the_count(self, conn: sqlite3.Connection, capsys) -> None:
        _seed(conn, title="stale", expires_at=datetime.now(UTC) - timedelta(days=2))
        _seed(conn, title="fresh", expires_at=datetime.now(UTC) + timedelta(days=2))
        capsys.readouterr()
        assert memory_command(conn, _ns(memory_cmd="expire")) == 0
        assert "expired 1 due memories" in capsys.readouterr().out
        remaining = conn.execute("SELECT title FROM memories").fetchall()
        assert [r[0] for r in remaining] == ["fresh"]


class TestDispatch:
    def test_unknown_subcommand_is_exit_two(self, conn: sqlite3.Connection, capsys) -> None:
        assert memory_command(conn, _ns(memory_cmd="frobnicate")) == 2
        assert "unknown subcommand frobnicate" in capsys.readouterr().out
