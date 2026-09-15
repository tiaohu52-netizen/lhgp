"""schema 迁移回归（安全审查 持久化-C1/C2）。

C1：真实 v1 结构库（events 无 goal_id 列）上 ensure_schema 曾因先建
    goal_id 索引后补列而 OperationalError——索引必须在迁移之后建。
C2：complete→satisfied 是一次性历史语义迁移，曾无条件执行于每次
    ensure_schema，把 daemon 刚写入的合法终态静默改写（无事件、无 CAS）。
"""

from __future__ import annotations

import sqlite3

from longtask.persistence.schema import ensure_schema

V1_EVENTS = """
CREATE TABLE events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL,
    attempt_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
V1_CONTRACTS = """
CREATE TABLE contracts (
    contract_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    state TEXT NOT NULL,
    next_wakeup_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL
)
"""


def _make_v1_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=1")
    conn.execute(V1_EVENTS)
    conn.execute(V1_CONTRACTS)
    conn.commit()
    return conn


def test_v1_database_opens_cleanly(tmp_path) -> None:
    """C1：真实 v1 结构的库必须能被 ensure_schema 平滑升级，不崩。"""
    conn = _make_v1_db(tmp_path / "state.db")
    conn.close()
    conn2 = sqlite3.connect(tmp_path / "state.db")
    try:
        ensure_schema(conn2)  # 修复前：no such column: goal_id
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 2
        cols = [r[1] for r in conn2.execute("PRAGMA table_info(events)")]
        assert "goal_id" in cols
        idx = [r[1] for r in conn2.execute("PRAGMA index_list(events)")]
        assert "idx_events_goal_id" in idx
    finally:
        conn2.close()


def test_v1_complete_rows_migrate_to_satisfied_once(tmp_path) -> None:
    """C2 正向：真正的 v1 complete 行在升级时一次性迁移为 satisfied。"""
    conn = _make_v1_db(tmp_path / "state.db")
    conn.execute(
        "INSERT INTO contracts (contract_id, title, objective, deadline_at, state,"
        " created_at, updated_at, schema_version)"
        " VALUES ('c1', 't', 'o', '2030-01-01T00:00:00+00:00', 'complete',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 1)"
    )
    conn.commit()
    conn.close()
    conn2 = sqlite3.connect(tmp_path / "state.db")
    try:
        ensure_schema(conn2)
        state = conn2.execute("SELECT state FROM contracts WHERE contract_id='c1'").fetchone()[0]
        assert state == "satisfied"
    finally:
        conn2.close()


def test_v2_complete_rows_survive_repeated_ensure_schema(tmp_path) -> None:
    """C2 防回归：v2 库的合法 complete 终态不得被后续 ensure_schema 改写。

    修复前每次打开库（CLI/每个 RPC 连接/daemon 启动）都会把 complete
    静默改写为 satisfied，绕过事件流与 revision CAS。
    """
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        ensure_schema(conn)
        # 直接构造 v2 complete 行（模拟 daemon 验收通过后的合法终态）：
        # 显式列名 + 全参数占位符，NOT NULL 列全给值，可空列由 DEFAULT 兜底。
        conn.execute(
            """
            INSERT INTO contracts (
                contract_id, goal_id, revision, state, deadline_status,
                acceptance_status, title, objective, deadline_at,
                hard_constraints_json, acceptance_json, workload_initial_hours,
                budget_json, created_at, updated_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "c1",
                "c1",
                7,
                "complete",
                "not_due",
                "pending",
                "t",
                "o",
                "2030-01-01T00:00:00+00:00",
                "[]",
                "{}",
                1.0,
                "{}",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                2,
            ),
        )
        conn.commit()
        row_before = conn.execute(
            "SELECT state, revision FROM contracts WHERE contract_id='c1'"
        ).fetchone()
        assert row_before == ("complete", 7)
    finally:
        conn.close()

    for _ in range(3):  # 多次重启模拟
        conn2 = sqlite3.connect(tmp_path / "state.db")
        try:
            ensure_schema(conn2)
            row = conn2.execute(
                "SELECT state, revision FROM contracts WHERE contract_id='c1'"
            ).fetchone()
            assert row == ("complete", 7), f"state rewritten: {row}"
        finally:
            conn2.close()
