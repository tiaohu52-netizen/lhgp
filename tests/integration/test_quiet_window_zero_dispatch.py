"""无动作窗口零派工（ROADMAP §六指标：无动作窗口 LLM 调用数 = 0）。

lhgp 的 LLM 调用只发生在 executor / verifier attempt 内部，守护进程的
调度核心是确定性代码、不调用任何模型。因此「无动作窗口内 LLM 调用数」
的运行时可观察面就是：**安静窗口内零新 attempt、零派工事件**。

本测试构造三类都「无事可做」的合同，连跑多轮 tick：

- ACTIVE + 活租约（执行者健康在跑，紧迫度 QUEUED）；
- ACTIVE + 无租约但紧迫度极低（远未到决策点）；
- BLOCKED(need-user)（等人，不能再派）。

此前这条核心声明在 ROADMAP 指标表里是「未度量」——它结构性成立
（tick 无 LLM 调用点），但没有一个可重复的运行时证据。本文件把它钉进
CI：任何让安静窗口产生派工的改动（误触发 RESPAWN、blocked 误唤醒、
QUEUED 误升级）都会在这里红。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.events import EventType
from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon import run_daemon_tick
from longtask.contracts.schema import ContractState
from longtask.persistence.store import (
    StoreConfig,
    acquire_lease,
    connect,
    ensure_schema,
    save_contract,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
CID_LEASED = "lt-quiet-leased"  # 活租约在跑
CID_IDLE = "lt-quiet-idle"  # 无租约但远未到决策点
CID_BLOCKED = "lt-quiet-blocked"  # 等人裁决

TICKS = 5


def _contract(conn: sqlite3.Connection, contract_id: str) -> None:
    save_contract(
        conn,
        contract_id=contract_id,
        draft=ContractDraft(
            title="quiet",
            objective="安静窗口合同",
            # 48h 后截止、工作量 1h → u = 1/48 < 0.25 → QUEUED（不打扰）
            deadline_at=NOW + timedelta(hours=48),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=5,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
            ),
        ),
        now=NOW,
        actor="user",
    )
    from longtask.persistence.store import update_contract_state

    if contract_id == CID_BLOCKED:
        # 状态机（审计 B1）：DRAFTED 不能直达 blocked（生产里由 handler 拒绝），
        # 经 active 落下。
        update_contract_state(
            conn, contract_id=contract_id, new_state=ContractState.ACTIVE, now=NOW, actor="user"
        )
        update_contract_state(
            conn,
            contract_id=contract_id,
            new_state=ContractState.BLOCKED,
            now=NOW,
            blocked_reason=__import__(
                "lhgp.contracts.contract_view", fromlist=["BlockReason"]
            ).BlockReason.NEED_USER,
            actor="user",
        )
    else:
        update_contract_state(
            conn, contract_id=contract_id, new_state=ContractState.ACTIVE, now=NOW
        )


def _running_attempt_with_lease(conn: sqlite3.Connection, contract_id: str) -> None:
    """给 leased 合同挂一个健康在跑的 attempt（活租约、QUEUED 紧迫度）。"""
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
        " admitted_at, contract_revision, updated_at)"
        " VALUES (?, ?, ?, 'executor', 'running', ?, 1, ?)",
        (f"att-{contract_id}", contract_id, contract_id, NOW.isoformat(), NOW.isoformat()),
    )
    acquire_lease(
        conn,
        contract_id=contract_id,
        holder_attempt_id=f"att-{contract_id}",
        heartbeat_at=NOW,
        timeout=timedelta(minutes=30),
        actor="daemon",
        payload={},
        role="executor",
        contract_revision=1,
        expected_generation=0,
    )
    conn.commit()


def _attempt_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])


def _events(conn: sqlite3.Connection, *types: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in types)
    # S608 已核：placeholders 只是 "?" 占位符重复，type 元组走参数绑定
    sql = f"SELECT event_type, payload_json FROM events WHERE event_type IN ({placeholders})"  # noqa: S608
    return conn.execute(sql, types).fetchall()


def test_quiet_window_produces_zero_dispatches(tmp_path: Path) -> None:
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
    try:
        _contract(conn, CID_LEASED)
        _contract(conn, CID_IDLE)
        _contract(conn, CID_BLOCKED)
        _running_attempt_with_lease(conn, CID_LEASED)

        attempts_before = _attempt_count(conn)
        started_before = len(_events(conn, EventType.ATTEMPT_STARTED.value))

        registry = ExecutorRegistry.load_from_file(root / "registry.json")
        for tick in range(TICKS):
            # 每轮推进 60s（tick 心跳），窗口内始终无决策到期
            run_daemon_tick(
                root,
                conn,
                registry,
                now=NOW + timedelta(seconds=60 * (tick + 1)),
            )

        # ① 零新 attempt：没有 executor 被拉起，也没有 verifier 被派生
        assert _attempt_count(conn) == attempts_before, (
            "安静窗口内产生了新 attempt（= 一次 LLM 调用）：调度核心在无事可做时"
            "做了派工，违反 ROADMAP §六『无动作窗口 LLM 调用数 = 0』"
        )
        # ② 零派工/升级事件
        noisy = _events(
            conn,
            EventType.ATTEMPT_STARTED.value,
            EventType.ESCALATION_HANDED_TO_USER.value,
            EventType.DISPATCH_DEFERRED.value,
        )
        assert len(noisy) == started_before, f"安静窗口出现派工/升级事件: {[r[0] for r in noisy]}"
        # ③ 合同状态纹丝不动：blocked 不被误唤醒，active 不被误转
        assert get_state(conn, CID_LEASED) == "active"
        assert get_state(conn, CID_IDLE) == "active"
        assert get_state(conn, CID_BLOCKED) == "blocked"
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, cid: str) -> str:
    row = conn.execute("SELECT state FROM contracts WHERE contract_id = ?", (cid,)).fetchone()
    assert row is not None
    return str(row[0])
