"""attempt 消耗台账（§11.3 写回字段 + schema v5）。

预算控制的是「派几次」，台账记的是「烧了多少」。usage 由执行者写回时
自报（harness 最清楚自己的消耗），守护进程既无法复算也不该猜，所以只有
两道门：形状校验 fail-closed（usage.py），落库与审计并行（attempts 行 +
终态事件 payload）。自报是**下界**语义——压缩/缓存刷新可能使真实消耗更高。
"""

from __future__ import annotations

import hashlib as _hl
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp import PROTOCOL_VERSION
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.schema import STORE_SCHEMA_VERSION, ensure_schema
from lhgp.persistence.store import acquire_lease, save_contract
from lhgp.persistence.usage import UsageInvalidError, normalize_usage
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.executor_api import handle_attempt_write_back
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
TEST_TOKEN = "test-session-token-0123456789abcdef"  # noqa: S105 - test fixture
TEST_TOKEN_HASH = _hl.sha256(TEST_TOKEN.encode()).hexdigest()
CID = "lt-usage-01"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _contract(conn: sqlite3.Connection) -> None:
    save_contract(
        conn,
        contract_id=CID,
        draft=ContractDraft(
            title="usage",
            objective="o",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=3,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
            ),
        ),
        now=NOW,
        actor="user",
    )


def _attempt_with_lease(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
        " admitted_at, contract_revision, updated_at, session_token_hash)"
        " VALUES (?, ?, ?, 'executor', 'running', ?, 1, ?, ?)",
        ("att-usage", CID, CID, NOW.isoformat(), NOW.isoformat(), TEST_TOKEN_HASH),
    )
    acquire_lease(
        conn,
        contract_id=CID,
        holder_attempt_id="att-usage",
        heartbeat_at=NOW,
        timeout=timedelta(minutes=30),
        actor="daemon",
        payload={},
        role="executor",
        contract_revision=1,
        expected_generation=0,
    )
    conn.commit()
    return "att-usage"


def _write_back(
    conn: sqlite3.Connection, attempt_id: str, usage: Any, *, terminal: bool = False
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "contract_id": CID,
        "attempt_id": attempt_id,
        "write_generation": 1,
        "session_token": TEST_TOKEN,
        "usage": usage,
    }
    if terminal:
        params["attempt_state"] = "succeeded"
    envelope = RequestEnvelope(
        method=Method.ATTEMPT_WRITE_BACK,
        request_id="req-usage",
        client_id="executor",
        protocol_version=PROTOCOL_VERSION,
        params=params,
    )
    return handle_attempt_write_back(envelope, conn=conn, now=NOW)


class TestNormalizeUsage:
    @pytest.mark.parametrize(
        "bad",
        [
            {"input_tokens": -1, "output_tokens": 1},
            {"output_tokens": 1},
            {},
            {"input_tokens": True, "output_tokens": 1},
            {"input_tokens": "100", "output_tokens": 1},
            {"input_tokens": None, "output_tokens": 1},
            {"bogus": 1},
            "not-a-dict",
            {"input_tokens": 1, "output_tokens": 1, "cost_estimate": -0.5},
        ],
    )
    def test_invalid_shapes_are_refused(self, bad: Any) -> None:
        with pytest.raises(UsageInvalidError):
            normalize_usage(bad)

    def test_valid_minimal(self) -> None:
        assert normalize_usage({"input_tokens": 10, "output_tokens": 2}) == {
            "input_tokens": 10,
            "output_tokens": 2,
        }

    def test_valid_full(self) -> None:
        out = normalize_usage(
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_tokens": 3,
                "cache_write_tokens": 4,
                "cost_estimate": 0.5,
            }
        )
        assert out["cache_read_tokens"] == 3
        assert out["cost_estimate"] == 0.5

    def test_none_means_not_carried(self) -> None:
        assert normalize_usage(None) == {}


class TestSchemaV5:
    def test_store_schema_version_is_5(self) -> None:
        assert STORE_SCHEMA_VERSION == 5

    def test_store_config_default_tracks_the_schema_constant(self) -> None:
        """漂移守护：StoreConfig 的手写默认值必须与 STORE_SCHEMA_VERSION 一致。

        v3→v4 迁移时这里漏升过一次（4==4 恰好活着），升到 5 才炸出来——
        原因是打开旧于配置的库会被 StoreTamperedError 拒收，所有 runner
        集成测试集体红。两处值必须一起动，这条测试保证不再靠巧合。
        """
        from longtask.persistence.types import StoreConfig

        assert StoreConfig(db_path=Path(".")).schema_version == STORE_SCHEMA_VERSION

    def test_fresh_db_has_usage_json(self, tmp_path: Path) -> None:
        conn = _conn(tmp_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
            assert "usage_json" in cols
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        finally:
            conn.close()

    def test_v4_db_is_upgraded_idempotently(self, tmp_path: Path) -> None:
        """旧库（无 usage_json 列、user_version=4）升级后补列且可重复执行。"""
        conn = sqlite3.connect(tmp_path / "state.db")
        conn.execute(
            "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,"
            " contract_revision INTEGER NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL,"
            " admitted_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute("PRAGMA user_version=4")
        conn.commit()
        try:
            ensure_schema(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
            assert "usage_json" in cols
            ensure_schema(conn)  # 幂等：再跑一遍不抛
        finally:
            conn.close()


class TestWriteBackCarriesUsage:
    def test_usage_lands_on_attempt_row_and_event(self, tmp_path: Path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            attempt_id = _attempt_with_lease(conn)
            usage = {"input_tokens": 1200, "output_tokens": 340, "cost_estimate": 0.42}
            _write_back(conn, attempt_id, usage, terminal=True)
            row = conn.execute(
                "SELECT usage_json FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert json.loads(row[0]) == usage
            events = conn.execute(
                "SELECT payload_json FROM events WHERE attempt_id = ?"
                " AND event_type = 'attempt/succeeded'",
                (attempt_id,),
            ).fetchall()
            payloads = [json.loads(e[0]) for e in events]
            assert any(p.get("usage") == usage for p in payloads)
        finally:
            conn.close()

    def test_invalid_usage_is_refused_without_side_effects(self, tmp_path: Path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            attempt_id = _attempt_with_lease(conn)
            with pytest.raises(RpcError) as excinfo:
                _write_back(conn, attempt_id, {"input_tokens": -5, "output_tokens": 1})
            assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
            # 拒收必须发生在任何落库之前：attempt 行无 usage、无终态事件
            row = conn.execute(
                "SELECT usage_json, state FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert row[0] is None
            assert row[1] == "running"
        finally:
            conn.close()

    def test_omitted_usage_still_writes_back(self, tmp_path: Path) -> None:
        """不携带台账的写回照常工作（向后兼容）。"""
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            attempt_id = _attempt_with_lease(conn)
            envelope = RequestEnvelope(
                method=Method.ATTEMPT_WRITE_BACK,
                request_id="req-no-usage",
                client_id="executor",
                protocol_version=PROTOCOL_VERSION,
                params={
                    "contract_id": CID,
                    "attempt_id": attempt_id,
                    "write_generation": 1,
                    "session_token": TEST_TOKEN,
                },
            )
            result = handle_attempt_write_back(envelope, conn=conn, now=NOW)
            assert result["ok"] is True
        finally:
            conn.close()


class TestStatsUsageAggregation:
    def test_stats_aggregates_usage_across_attempts(self, tmp_path: Path) -> None:
        """两个 attempt 各自记账，stats 跨行求和；同一行重复写回按最后一次为准。"""
        from lhgp.persistence.insights import build_stats

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            first = _attempt_with_lease(conn)
            _write_back(conn, first, {"input_tokens": 1200, "output_tokens": 340}, terminal=True)
            # 第二个 attempt：直接插行 + 挂新租约（generation 递增）
            conn.execute(
                "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
                " admitted_at, contract_revision, updated_at, session_token_hash,"
                " usage_json)"
                " VALUES (?, ?, ?, 'executor', 'succeeded', ?, 1, ?, ?, ?)",
                (
                    "att-usage-2",
                    CID,
                    CID,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    TEST_TOKEN_HASH,
                    json.dumps({"input_tokens": 80, "output_tokens": 60, "cost_estimate": 0.1}),
                ),
            )
            conn.commit()
            stats = build_stats(conn, contract_id=CID)
            assert stats["usage_totals"] == {
                "input_tokens": 1280.0,
                "output_tokens": 400.0,
                "cost_estimate": 0.1,
            }
        finally:
            conn.close()

    def test_stats_without_usage_has_no_usage_key(self, tmp_path: Path) -> None:
        """没记过台账的库不出现空 usage_totals——空字段是噪音不是信息。"""
        from lhgp.persistence.insights import build_stats

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            stats = build_stats(conn, contract_id=CID)
            assert "usage_totals" not in stats
        finally:
            conn.close()

    def test_stats_skips_malformed_legacy_usage(self, tmp_path: Path) -> None:
        """存量脏数据（手改库/旧版本写入）跳过不抛——台账透明但不脆弱。"""
        from lhgp.persistence.insights import build_stats

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            attempt_id = _attempt_with_lease(conn)
            _write_back(conn, attempt_id, {"input_tokens": 5, "output_tokens": 5}, terminal=True)
            conn.execute(
                "UPDATE attempts SET usage_json = '{not json' WHERE attempt_id = ?",
                (attempt_id,),
            )
            conn.commit()
            stats = build_stats(conn, contract_id=CID)
            assert "usage_totals" not in stats
        finally:
            conn.close()


class TestBudgetMaxCostRoundTrip:
    def test_max_cost_survives_save_and_load(self, tmp_path: Path) -> None:
        """max_cost 必须过 save_contract → budget_json → _row_to_contract_view 回环。

        本轮端到端测试曾因此静默失效：Budget 数据类有字段、解析有字段，
        但 store 的手写序列化/读回两处字段清单都没它——能力在场，钱线却
        消失在存取之间。这条测试把「写入即读回」钉死。
        """
        from lhgp.persistence.store import get_contract

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            save_contract(
                conn,
                contract_id="lt-usage-mc",
                draft=ContractDraft(
                    title="mc",
                    objective="o",
                    deadline_at=NOW + timedelta(hours=2),
                    hard_constraints={"file_effects": {"mode": "workspace-write"}},
                    acceptance=Acceptance(standard="s", checks=("c1",)),
                    workload_initial_hours=1.0,
                    budget=Budget(
                        max_dispatches=3,
                        max_escalations=1,
                        max_concurrent_attempts=1,
                        max_attempt_minutes=30,
                        max_output_bytes=1024,
                        max_cost=9.75,
                    ),
                ),
                now=NOW,
                actor="user",
            )
            view = get_contract(conn, "lt-usage-mc")
            assert view is not None
            assert view.draft.budget.max_cost == 9.75
        finally:
            conn.close()

    def test_absent_max_cost_round_trips_as_none(self, tmp_path: Path) -> None:
        from lhgp.persistence.store import get_contract

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            view = get_contract(conn, CID)
            assert view is not None
            assert view.draft.budget.max_cost is None
        finally:
            conn.close()
