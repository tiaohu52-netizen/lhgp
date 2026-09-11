"""Insights/maintenance/proposal 扩展回归（分支 feature/plugin-expansion）。

覆盖四组新能力：brief 接手包、board 风险看板、stats 成本台账、
diff 修订差异、prune-events 终态事件清理、goal/proposed 提案事件。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import (
    save_contract,
    update_contract_state,
)

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _make_contract(
    conn: sqlite3.Connection,
    cid: str = "lt-insight01",
    *,
    state: str | None = None,
) -> None:
    save_contract(
        conn,
        contract_id=cid,
        draft=ContractDraft(
            title="insight fixture",
            objective="o",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=2.0,
            budget=Budget(
                max_dispatches=4,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
                verification_attempts_reserved=2,
            ),
        ),
        now=NOW,
        actor="user",
    )
    if state:
        from lhgp.contracts.schema import ContractState

        # 状态机（审计 B1）：DRAFTED 的出边只有 active/cancelled，所以经 active 落到
        # 目标态。本 fixture 以前直接写目标态（drafted→blocked/complete），走的是
        # handler 早就拒绝、生产不可达的转移。
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
        if state != ContractState.ACTIVE.value:
            update_contract_state(conn, contract_id=cid, new_state=ContractState(state), now=NOW)


def _add_attempt(
    conn: sqlite3.Connection,
    cid: str,
    attempt_id: str,
    *,
    role: str = "executor",
    state: str = "succeeded",
    executor_id: str = "exec-a",
    wall_minutes: int = 12,
) -> None:
    admitted = NOW.isoformat()
    terminal = (NOW + timedelta(minutes=wall_minutes)).isoformat()
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id,"
        " state, admitted_at, terminal_at, return_code, contract_revision, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (attempt_id, cid, cid, role, executor_id, state, admitted, terminal, 0, terminal),
    )
    conn.commit()


class TestBoard:
    def test_board_excludes_terminal_by_default(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-live01", state="active")
            _make_contract(conn, "lt-done01", state="complete")
            from lhgp.persistence.insights import build_board

            rows = build_board(conn, now=NOW)
            ids = [r["contract_id"] for r in rows]
            assert "lt-live01" in ids and "lt-done01" not in ids
            rows_all = build_board(conn, now=NOW, include_terminal=True)
            ids_all = [r["contract_id"] for r in rows_all]
            assert {"lt-live01", "lt-done01"} <= set(ids_all)
        finally:
            conn.close()

    def test_board_rows_carry_risk_and_budget(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-live02", state="active")
            from lhgp.persistence.insights import build_board

            rows = build_board(conn, now=NOW)
            row = next(r for r in rows if r["contract_id"] == "lt-live02")
            assert row["max_dispatches"] == 4
            assert "risk" in row
        finally:
            conn.close()


class TestBrief:
    def test_brief_unknown_contract(self, tmp_path: Path) -> None:
        from lhgp.persistence.insights import build_brief

        conn = _make_conn(tmp_path)
        try:
            brief = build_brief(conn, contract_id="lt-missing", now=NOW)
            assert brief["found"] is False
        finally:
            conn.close()

    def test_brief_reports_state_and_attempts(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-brief01", state="blocked")
            from lhgp.contracts.schema import BlockReason

            update_contract_state(
                conn,
                contract_id="lt-brief01",
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.NEED_USER,
            )
            _add_attempt(conn, "lt-brief01", "att-b1", state="failed", wall_minutes=8)
            from lhgp.persistence.insights import build_brief

            brief = build_brief(conn, contract_id="lt-brief01", now=NOW)
            assert brief["found"] is True
            assert brief["state"] == "blocked"
            assert brief["blocked_reason"] == "need-user"
            assert brief["latest_attempt"]["state"] == "failed"
            assert brief["recent_attempts"][0]["attempt_id"] == "att-b1"
            assert "handover.md" in brief["handover_hint"]
        finally:
            conn.close()


class TestStats:
    def test_stats_aggregates_wall_time_and_roles(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-stats01")
            _add_attempt(conn, "lt-stats01", "att-s1", wall_minutes=10)
            _add_attempt(
                conn,
                "lt-stats01",
                "att-s2",
                role="verifier",
                state="failed",
                executor_id="exec-b",
                wall_minutes=30,
            )
            from lhgp.persistence.insights import build_stats

            stats = build_stats(conn, contract_id="lt-stats01")
            assert stats["total_attempts"] == 2
            assert stats["by_role"] == {"executor": 1, "verifier": 1}
            assert stats["by_executor"]["exec-a"] == 1
            assert stats["wall_seconds_p50"] == 600.0
            assert stats["wall_seconds_max"] == 1800.0
        finally:
            conn.close()

    def test_stats_all_scope(self, tmp_path: Path) -> None:
        from lhgp.persistence.insights import build_stats

        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-stats02")
            _add_attempt(conn, "lt-stats02", "att-x1")
            all_scope = build_stats(conn)
            assert all_scope["scope"] == "all"
            assert all_scope["total_attempts"] >= 1
        finally:
            conn.close()


class TestRevisionDiff:
    def test_diff_reports_acceptance_change_only(self, tmp_path: Path) -> None:
        """修订 1→2 只改 soft_guidance：diff 应只报该字段（JSON 语义比较）。"""
        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-diff01")
            # 用真实 patch 产生修订 2
            from lhgp.persistence.store import patch_contract

            patch_contract(
                conn,
                contract_id="lt-diff01",
                expected_revision=1,
                now=NOW,
                soft_guidance={"note": "用户补充"},
            )
            from lhgp.persistence.maintenance import diff_revisions

            result = diff_revisions(conn, contract_id="lt-diff01", from_revision=1, to_revision=2)
            assert result["found"] is True
            assert result["changed"] is True
            fields = [c["field"] for c in result["changes"]]
            assert fields == ["soft_guidance_json"]
            assert result["changes"][0]["to"] == {"note": "用户补充"}
        finally:
            conn.close()

    def test_diff_missing_revision(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-diff02")
            from lhgp.persistence.maintenance import diff_revisions

            result = diff_revisions(conn, contract_id="lt-diff02", from_revision=9, to_revision=2)
            assert result["found"] is False
        finally:
            conn.close()


class TestPrune:
    def test_prune_dry_run_then_delete(self, tmp_path: Path) -> None:
        from lhgp.persistence.maintenance import prune_terminal_events

        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-prune01", state="complete")
            old_at = (NOW - timedelta(days=120)).isoformat()
            conn.execute(
                "INSERT INTO events (contract_id, event_type, payload_json,"
                " created_at, actor, schema_version)"
                " VALUES ('lt-prune01', 'attempt/succeeded', '{}', ?, 'daemon', 2)",
                (old_at,),
            )
            conn.execute(
                "INSERT INTO events (contract_id, event_type, payload_json,"
                " created_at, actor, schema_version)"
                " VALUES ('lt-prune01', 'contract/completed', '{}', ?, 'daemon', 2)",
                (NOW.isoformat(),),
            )
            conn.commit()
            dry = prune_terminal_events(conn, now=NOW, keep_days=30, dry_run=True)
            assert dry["candidate_events"] == 1 and dry["deleted"] == 0
            real = prune_terminal_events(conn, now=NOW, keep_days=30, dry_run=False)
            assert real["deleted"] == 1
            old_left = conn.execute(
                "SELECT COUNT(*) FROM events WHERE contract_id='lt-prune01' AND created_at < ?",
                ((NOW - timedelta(days=30)).isoformat(),),
            ).fetchone()[0]
            fresh_left = conn.execute(
                "SELECT COUNT(*) FROM events WHERE contract_id='lt-prune01' AND created_at >= ?",
                ((NOW - timedelta(days=30)).isoformat(),),
            ).fetchone()[0]
            assert old_left == 0, "old event survived prune"
            assert fresh_left >= 1, "fresh events must be retained"
        finally:
            conn.close()

    def test_prune_never_touches_active_contracts(self, tmp_path: Path) -> None:
        from lhgp.persistence.maintenance import prune_terminal_events

        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-prune02", state="active")
            old_at = (NOW - timedelta(days=120)).isoformat()
            conn.execute(
                "INSERT INTO events (contract_id, event_type, payload_json,"
                " created_at, actor, schema_version)"
                " VALUES ('lt-prune02', 'contract/approved', '{}', ?, 'user', 2)",
                (old_at,),
            )
            conn.commit()
            real = prune_terminal_events(conn, now=NOW, keep_days=30, dry_run=False)
            assert real["deleted"] == 0
        finally:
            conn.close()
