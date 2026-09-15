"""poll_attempts 防崩溃回归（安全审查 进程-C2）。

一个 fenced 租约曾能击穿 poll_attempts 顶层：
- 路径 1：attempt 运行中租约被释放 → renew_lease 抛 LeaseFencedError，
  未捕获 → daemon 主循环无兜底 → 常驻进程死亡，所有合同调度停摆。
- 路径 2：spawn 前租约消失 → info["generation"] 为 None → int(None) 抛
  TypeError，同样击穿。
attempt 级的租约异常必须留在 attempt 级（标 stale），不得升级为进程崩溃。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

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
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _setup(tmp_path: Path) -> tuple[Path, Any, str, ExecutorRegistry]:
    root = tmp_path / "data"
    ws = root / "ws"
    ws.mkdir(parents=True)
    cid = "lt-pollguard"
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="poll guard",
            objective="租约异常不得击穿 daemon",
            deadline_at=NOW + timedelta(hours=1),
            hard_constraints={
                "file_effects": {"mode": "workspace-write", "workspace_root": str(ws)}
            },
            acceptance=Acceptance(standard="s", checks=("file-exists:x",)),
            workload_initial_hours=1.0,
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
    reg.register(
        RegistryEntry(
            id="exec-1",
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


def test_renew_fenced_marks_stale_instead_of_raising(tmp_path: Path) -> None:
    """路径 1：RUNNING attempt 的租约中途被释放 → poll 不得抛异常。"""
    root, conn, cid, reg = _setup(tmp_path)
    runner = AttemptRunner(root, conn, reg)
    attempt_id = "att-fenced-1"
    adapter = Mock()
    adapter.observe.return_value = {"state": "running"}
    runner._adapters["exec-1"] = adapter
    runner._running[attempt_id] = {
        "contract_id": cid,
        "executor_id": "exec-1",
        "model": "*",
        "role": "executor",
        "contract_revision": 1,
        "session_ref": "test",
        "generation": 1,
        "started_at": NOW,
    }
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id, state,"
        " admitted_at, contract_revision, updated_at, session_token_hash)"
        " VALUES (?, ?, ?, 'executor', 'exec-1', 'running', ?, 1, ?, 'test-session-hash')",
        (attempt_id, cid, cid, NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    # 租约已不存在（被释放/被别的路径清掉）——旧代码 renew_lease 直接抛
    try:
        runner.poll_attempts(NOW + timedelta(minutes=1))
        raised = False
    except Exception as exc:  # pragma: no cover - 修复后不应到达
        raised = True
        print(f"unexpected raise: {exc}")
    assert not raised, "poll_attempts raised on a fenced/released lease"
    state = conn.execute(
        "SELECT state FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    assert state[0] == "stale", "attempt should be marked stale, not crash the loop"


def test_missing_generation_falls_back_without_type_error(tmp_path: Path) -> None:
    """路径 2：info['generation'] 为 None 不得 int(None) 崩溃。"""
    root, conn, cid, reg = _setup(tmp_path)
    runner = AttemptRunner(root, conn, reg)
    attempt_id = "att-nogen-1"
    adapter = Mock()
    adapter.observe.return_value = {"state": "running"}
    runner._adapters["exec-1"] = adapter
    runner._running[attempt_id] = {
        "contract_id": cid,
        "executor_id": "exec-1",
        "model": "*",
        "role": "executor",
        "contract_revision": 1,
        "session_ref": "test",
        "generation": None,
        "started_at": NOW,
    }
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id, state,"
        " admitted_at, contract_revision, updated_at, session_token_hash)"
        " VALUES (?, ?, ?, 'executor', 'exec-1', 'running', ?, 1, ?, 'test-session-hash')",
        (attempt_id, cid, cid, NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    try:
        runner.poll_attempts(NOW + timedelta(minutes=1))
        raised = False
    except TypeError as exc:
        raised = True
        print(f"TypeError: {exc}")
    assert not raised, "int(None) TypeError leaked from poll_attempts"
