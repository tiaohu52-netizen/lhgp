"""持久层骨架集成冒烟（DESIGN §13.3）。

真实 SQLite（tmp_path）：连接、WAL、事件表建表、未来版本拒写。
Developer Preview 补齐事务写入后，本文件扩展为一致性保证场景。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from longtask.persistence.store import (
    STORE_SCHEMA_VERSION,
    StoreConfig,
    StoreTamperedError,
    connect,
    ensure_schema,
)

pytestmark = pytest.mark.integration


def test_connect_and_ensure_schema(tmp_path: Path) -> None:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        ensure_schema(conn)
        row = conn.execute("PRAGMA user_version").fetchone()
        assert row[0] == STORE_SCHEMA_VERSION
        goal_cols = {r[1] for r in conn.execute("PRAGMA table_info(goals)").fetchall()}
        assert {"goal_id", "title", "objective", "created_at", "updated_at"} <= goal_cols
        # 事件表骨架存在且字段齐全（DESIGN §13.3 最小集）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        assert {
            "event_id",
            "contract_id",
            "attempt_id",
            "lease_generation",
            "event_type",
            "payload_json",
            "request_id",
            "created_at",
            "actor",
            "schema_version",
        } <= cols
        decision_cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        assert {"goal_id", "contract_id", "contract_revision", "decision_type"} <= decision_cols
    finally:
        conn.close()


def test_newer_schema_version_refused(tmp_path: Path) -> None:
    # 未知未来版本只读拒写，不猜测迁移（DESIGN §13.3）
    db = tmp_path / "state.db"
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(StoreTamperedError, match="newer than supported"):
        connect(StoreConfig(db_path=db))


def test_schema_reconciles_partial_v2_columns(tmp_path: Path) -> None:
    """A v2 marker must not skip additive columns from a partial upgrade."""

    db = tmp_path / "partial-v2.db"
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, goal_id TEXT, "
        "state TEXT NOT NULL, role TEXT, admitted_at TEXT)"
    )
    raw.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")
    raw.commit()
    raw.close()

    conn = connect(StoreConfig(db_path=db))
    try:
        ensure_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
        assert {
            "model_id",
            "contract_id",
            "external_run_id",
            "session_locator",
            "capability_snapshot_json",
        } <= columns
    finally:
        conn.close()
