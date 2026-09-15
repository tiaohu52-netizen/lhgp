"""P2: Memory dataclass boundary tests.

Covers the frozen-dataclass row roundtrip, JSON tag serialization,
datetime handling, and the Row/tuple compat that P6 burned us on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lhgp.memory.types import Memory, MemoryKind, MemoryScope

pytestmark = pytest.mark.unit


def _sample_memory(**overrides: object) -> Memory:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    base: dict[str, object] = dict(
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.PATTERN,
        title="tuple/Row compat",
        body_md="`r[col]` on a plain tuple is TypeError; always try Row first.",
        tags=("topic/sql", "audience/ai"),
        source_contract_id="lt-p2-1",
        source_event_id=42,
        source_actor="eval:狐条",
        score=0.7,
        id=1,
        created_at=now,
        expires_at=now.replace(month=now.month + 1) if now.month < 12 else now,
        schema_version=4,
    )
    base.update(overrides)
    return Memory(**base)  # type: ignore[arg-type]


class TestMemorySerialization:
    def test_to_db_row_roundtrip(self) -> None:
        m = _sample_memory()
        row = m.to_db_row()
        # json-encoded tag list survives
        assert json.loads(row["tags_json"]) == ["topic/sql", "audience/ai"]
        # datetime stays as ISO string
        assert isinstance(row["created_at"], str)
        assert row["created_at"].startswith("2026-09-07T12:00:00")
        # enum values are stored as plain strings
        assert row["scope"] == "project"
        assert row["kind"] == "pattern"

    def test_from_db_row_with_sqlite3_row(self, tmp_path: Path) -> None:
        # Real sqlite3.Row path — verify the happy path.
        db = tmp_path / "m.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE t ("
                "id INTEGER, scope TEXT, kind TEXT, title TEXT, body_md TEXT, "
                "tags_json TEXT, source_contract_id TEXT, source_event_id INTEGER, "
                "source_actor TEXT, score REAL, created_at TEXT, expires_at TEXT, "
                "schema_version INTEGER)"
            )
            conn.execute(
                "INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    1,
                    "project",
                    "pattern",
                    "x",
                    "body",
                    '["t1"]',
                    "lt-1",
                    7,
                    "auto",
                    0.5,
                    "2026-09-07T12:00:00",
                    "2027-03-06T12:00:00",
                    4,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM t").fetchone()
        finally:
            conn.close()
        m = Memory.from_db_row(row)
        assert m.id == 1
        assert m.scope is MemoryScope.PROJECT
        assert m.kind is MemoryKind.PATTERN
        assert m.title == "x"
        assert m.body_md == "body"
        assert m.tags == ("t1",)
        assert m.source_contract_id == "lt-1"
        assert m.source_event_id == 7
        assert m.score == 0.5
        assert m.created_at == datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
        assert m.expires_at == datetime(2027, 3, 6, 12, 0, 0, tzinfo=UTC)

    def test_from_db_row_with_plain_tuple(self) -> None:
        # The P6 regression: cursor returns plain tuple when no row_factory
        # is set. The from_db_row helper must fall through to index access
        # if `r["col"]` raises TypeError (KeyError on plain tuple).
        # Build a sqlite3.Row first to get a real tuple-backed result.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(Path(td) / "m.db")
            try:
                conn.execute(
                    "CREATE TABLE t ("
                    "id INTEGER, scope TEXT, kind TEXT, title TEXT, body_md TEXT, "
                    "tags_json TEXT, source_contract_id TEXT, source_event_id INTEGER, "
                    "source_actor TEXT, score REAL, created_at TEXT, expires_at TEXT, "
                    "schema_version INTEGER)"
                )
                conn.execute(
                    "INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        2,
                        "global",
                        "rule",
                        "t",
                        "b",
                        "[]",
                        None,
                        None,
                        None,
                        0.0,
                        "2026-09-07T12:00:00",
                        None,
                        4,
                    ),
                )
                conn.commit()
                # Plain tuple path: no row_factory set, so fetchone returns
                # a tuple. The from_db_row helper should still work.
                row = conn.execute("SELECT * FROM t").fetchone()
            finally:
                conn.close()
        assert isinstance(row, tuple)
        assert not hasattr(row, "keys")
        m = Memory.from_db_row(row)
        assert m.id == 2
        assert m.scope is MemoryScope.GLOBAL
        assert m.kind is MemoryKind.RULE
        assert m.tags == ()
        assert m.source_contract_id is None

    def test_from_db_row_handles_corrupt_tags_json(self) -> None:
        # Robustness: if tags_json is garbage, fall back to empty list,
        # don't crash the read path. The memory is preserved, just
        # without tags.
        corrupt = (
            99,
            "project",
            "pattern",
            "x",
            "body",
            "{not valid",
            None,
            None,
            None,
            0.0,
            "2026-09-07T12:00:00",
            None,
            4,
        )
        m = Memory.from_db_row(corrupt)
        assert m.tags == ()

    def test_from_db_row_handles_bad_datetime(self) -> None:
        # A bad ISO string in created_at/expires_at must NOT crash; the
        # field becomes None.
        bad = (
            99,
            "project",
            "pattern",
            "x",
            "body",
            "[]",
            None,
            None,
            None,
            0.0,
            "not-a-date",
            "also-not-a-date",
            4,
        )
        m = Memory.from_db_row(bad)
        assert m.created_at is None
        assert m.expires_at is None


class TestMemoryEquality:
    def test_same_data_yields_equal(self) -> None:
        a = _sample_memory()
        b = _sample_memory()
        assert a == b
        assert hash(a) == hash(b)

    def test_different_scope_yields_different(self) -> None:
        a = _sample_memory(scope=MemoryScope.PROJECT)
        b = _sample_memory(scope=MemoryScope.GLOBAL)
        assert a != b
