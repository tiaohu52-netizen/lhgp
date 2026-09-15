"""verifier 派发不抢活租约 + kill switch 全覆盖（安全审查 调度-C1/C2）。

C1：_dispatch_verifier 曾直接 CAS 覆盖在跑 executor 的活租约——executor
    被误判 stale、外部进程失管、同 workspace 出现双写者。
C2：kill switch 只拦 tick 派发；daemon_loop 的 verifier 旁路
    （_consume_verification_requests / _finish_attempt）照常 spawn。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.runner import AttemptRunner
from longtask.contracts.schema import Acceptance, Budget, ContractDraft, ContractState
from longtask.persistence.events import EventType
from longtask.persistence.events_query import get_events
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_lease,
    save_contract,
    update_contract_state,
)
from longtask.promoter.killswitch import KILL_SWITCH_FILE

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _setup(tmp_path: Path) -> tuple[Path, Any, str, ExecutorRegistry]:
    root = tmp_path / "data"
    ws = root / "ws"
    ws.mkdir(parents=True)
    (ws / "result.txt").write_text("deliverable\n", encoding="utf-8")
    cid = "lt-verguard"
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="verifier guard",
            objective="verifier 不抢活租约",
            deadline_at=NOW + timedelta(hours=1),
            hard_constraints={
                "file_effects": {"mode": "workspace-write", "workspace_root": str(ws)}
            },
            acceptance=Acceptance(
                standard="全部通过", checks=({"kind": "file-exists", "target": "result.txt"},)
            ),
            workload_initial_hours=1.5,
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
                verification_attempts_reserved=3,
            ),
        ),
        contract_id=cid,
        now=NOW,
    )
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    reg = ExecutorRegistry()
    for exec_id in ("exec-a", "exec-b"):
        reg.register(
            RegistryEntry(
                id=exec_id,
                kind="fake",
                launch=LaunchSpec(),
                capabilities=FAKE_MANIFEST.capabilities,
                limits={"max_concurrent_attempts": 2},
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
    reg.save_to_file(root / "registry.json")
    return root, conn, cid, reg


def _running_executor(conn: Any, cid: str) -> str:
    """造一个持活租约的非终态 executor attempt。"""
    attempt_id = "att-running-1"
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id, state,"
        " admitted_at, contract_revision, updated_at, session_token_hash)"
        " VALUES (?, ?, ?, 'executor', 'exec-a', 'running', ?, 1, ?, 'test-session-hash')",
        (attempt_id, cid, cid, NOW.isoformat(), NOW.isoformat()),
    )
    from longtask.persistence.store import acquire_lease

    acquire_lease(
        conn,
        contract_id=cid,
        holder_attempt_id=attempt_id,
        expected_generation=0,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=30),
    )
    conn.commit()
    return attempt_id


def _deferred_count(conn: Any, cid: str) -> int:
    return len(
        [
            event
            for event in get_events(conn, contract_id=cid)
            if event.event_type == EventType.DISPATCH_DEFERRED
        ]
    )


def test_same_lease_generation_defers_are_logged_once(tmp_path: Path) -> None:
    """审计 B7 配套：同一租约代际的派发延后只记一次事件。

    修复 B7 后，被拒接的 verification 请求保持待兑现，daemon 每轮 tick 都会
    重试派发。若每次都写 dispatch/deferred，一次外部执行（默认 30 分钟、
    tick 数十轮）就会堆出几十条同义事件——在已知的「事件无界增长」问题上
    再加一份。代际变化说明情况确实变了，必须再记一次（对照组）。
    """
    root, conn, cid, reg = _setup(tmp_path)
    runner = AttemptRunner(root, conn, reg)
    _running_executor(conn, cid)
    try:
        for _ in range(3):
            assert runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-a") is False
        assert _deferred_count(conn, cid) == 1, "同一代际重复写了延后事件"

        # 对照组：换一个持活租约的非终态 attempt（代际 +1）→ 必须再记一次
        from longtask.persistence.store import acquire_lease

        conn.execute(
            "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id, state,"
            " admitted_at, contract_revision, updated_at, session_token_hash)"
            " VALUES (?, ?, ?, 'executor', 'exec-b', 'running', ?, 1, ?, 'test-session-hash')",
            ("att-running-2", cid, cid, NOW.isoformat(), NOW.isoformat()),
        )
        acquire_lease(
            conn,
            contract_id=cid,
            holder_attempt_id="att-running-2",
            expected_generation=1,
            heartbeat_at=NOW,
            timeout=timedelta(minutes=30),
        )
        conn.commit()
        assert runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-b") is False
        assert runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-b") is False
        assert _deferred_count(conn, cid) == 2, "新租约代际的延后必须重新记录"
    finally:
        conn.close()


def test_verifier_defers_when_executor_holds_live_lease(tmp_path: Path) -> None:
    root, conn, cid, reg = _setup(tmp_path)
    runner = AttemptRunner(root, conn, reg)
    executor_id = _running_executor(conn, cid)
    try:
        ok = runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-a")
        assert ok is False
        lease = get_lease(conn, cid)
        assert lease is not None
        assert lease.holder_attempt_id == executor_id, "verifier stole the live lease"
        state = conn.execute(
            "SELECT state FROM attempts WHERE attempt_id = ?", (executor_id,)
        ).fetchone()
        assert state[0] == "running", "executor was marked stale by verifier dispatch"
        deferred = [
            e for e in get_events(conn, contract_id=cid) if e.event_type == "dispatch/deferred"
        ]
        assert deferred, "no dispatch/deferred audit event written"
    finally:
        conn.close()


def test_kill_switch_blocks_verifier_dispatch(tmp_path: Path) -> None:
    root, conn, cid, reg = _setup(tmp_path)
    (root / KILL_SWITCH_FILE).write_text("stop\n", encoding="utf-8")
    runner = AttemptRunner(root, conn, reg)
    try:
        ok = runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-a")
        assert ok is False
        started = [
            e
            for e in get_events(conn, contract_id=cid)
            if e.event_type == EventType.ATTEMPT_STARTED
        ]
        assert not started, "verifier spawned despite active kill switch"
    finally:
        conn.close()
