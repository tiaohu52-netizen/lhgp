"""budget.max_cost 强制的端到端（tick 级）验证。

成本线是台账（SPEC §12.3.1）的强制面：合同声明 max_cost 后，其全部
attempt 的 cost_estimate 自报合计触线时，tick 必须 HAND_TO_USER（blocked
need-user）且**不派工**——即使紧迫度已达 RESPAWN、预算还有 dispatch 次数。

对照合同（未声明 max_cost）在同一轮 tick 里正常派工，证明门只拦成本
触线者、决策链路本身健康。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from lhgp.persistence.events import EventType
from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon import run_daemon_tick
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
CID_CAPPED = "lt-cost-capped"  # 成本触线
CID_FREE = "lt-cost-free"  # 未声明 max_cost（对照组）


def _draft(contract_id: str, *, max_cost: float | None) -> ContractDraft:
    return ContractDraft(
        title="cost gate",
        objective="验证成本预算线",
        # 30min 截止 / 2h 工作量 → u = 4 ≥ RESPAWN：若无成本线必派工
        deadline_at=NOW + timedelta(minutes=30),
        hard_constraints={"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="测试", checks=("c1",)),
        workload_initial_hours=2.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
            max_cost=max_cost,
        ),
    )


def _setup(tmp_path: Path) -> tuple[Path, sqlite3.Connection, ExecutorRegistry]:
    root = tmp_path / "data"
    root.mkdir()
    reg = ExecutorRegistry()
    reg.register(
        RegistryEntry(
            id="exec-a",
            kind="fake",
            launch=LaunchSpec(),
            capabilities=FAKE_MANIFEST.capabilities,
            limits={"max_concurrent_attempts": 2},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    reg.save_to_file(root / "registry.json")
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    return root, conn, reg


def _active_contract(conn: sqlite3.Connection, cid: str, *, max_cost: float | None) -> None:
    save_contract(
        conn,
        contract_id=cid,
        draft=_draft(cid, max_cost=max_cost),
        now=NOW,
        actor="user",
    )
    from longtask.persistence.store import update_contract_state

    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)


def _spent_usage(conn: sqlite3.Connection, cid: str, cost: float) -> None:
    """给合同挂一个已终局的 attempt，自报 cost_estimate = cost。"""
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
        " admitted_at, terminal_at, contract_revision, updated_at, usage_json)"
        " VALUES (?, ?, ?, 'executor', 'succeeded', ?, ?, 1, ?, ?)",
        (
            f"att-{cid}-spent",
            cid,
            cid,
            (NOW - timedelta(minutes=20)).isoformat(),
            (NOW - timedelta(minutes=5)).isoformat(),
            NOW.isoformat(),
            json.dumps({"input_tokens": 100, "output_tokens": 50, "cost_estimate": cost}),
        ),
    )
    conn.commit()


def _attempt_count(conn: sqlite3.Connection, cid: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM attempts WHERE contract_id = ?", (cid,)).fetchone()[0]
    )


def test_cost_budget_exhaustion_blocks_dispatch_but_not_the_sibling(
    tmp_path: Path,
) -> None:
    root, conn, reg = _setup(tmp_path)
    try:
        _active_contract(conn, CID_CAPPED, max_cost=1.0)
        _spent_usage(conn, CID_CAPPED, cost=2.0)  # 自报 2.0 ≥ max_cost 1.0
        _active_contract(conn, CID_FREE, max_cost=None)

        run_daemon_tick(root, conn, reg, now=NOW + timedelta(minutes=1))

        # 成本触线者：blocked need-user，reason 指明成本，且零新 attempt
        capped = get_contract(conn, CID_CAPPED)
        assert capped is not None and capped.state == ContractState.BLOCKED
        assert capped.blocked_reason is not None and capped.blocked_reason == "need-user"
        blocked_events = [
            e for e in get_blocked_events(conn, CID_CAPPED) if "cost budget exhausted" in e
        ]
        assert blocked_events, "blocked 事件未写明成本原因"
        assert _attempt_count(conn, CID_CAPPED) == 1, "成本触线者仍被派工"

        # 对照组：未声明 max_cost 的合同在同一轮正常派工（决策链健康）
        assert _attempt_count(conn, CID_FREE) == 1, "对照组未派工：成本门误伤了不设成本线的合同"
    finally:
        conn.close()


def test_cost_budget_not_yet_exhausted_still_dispatches(tmp_path: Path) -> None:
    """未触线（余量为正）→ 正常派工，成本线不提前收紧。"""
    root, conn, reg = _setup(tmp_path)
    try:
        _active_contract(conn, CID_CAPPED, max_cost=5.0)
        _spent_usage(conn, CID_CAPPED, cost=2.0)

        run_daemon_tick(root, conn, reg, now=NOW + timedelta(minutes=1))

        capped = get_contract(conn, CID_CAPPED)
        assert capped is not None and capped.state == ContractState.ACTIVE
        assert _attempt_count(conn, CID_CAPPED) == 2, "余量充足却未派工"
    finally:
        conn.close()


def get_blocked_events(conn: sqlite3.Connection, cid: str) -> list[str]:
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE contract_id = ? AND event_type = ?",
        (cid, EventType.CONTRACT_BLOCKED.value),
    ).fetchall()
    return [str(r[0]) for r in rows]
