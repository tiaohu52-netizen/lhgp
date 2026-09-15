"""执行器并发额度的记账口径（审计 B4）。

``count_running_by_executor`` 决定 ``match_candidates`` 眼里「这个执行器还有
没有空位」。它原先写死 ``('admitted', 'running', 'orphaned')``，与状态机两头
都不一致。本文件把口径钉在 SPEC §7 的终态定义上：

- 终态（succeeded / failed / cancelled / stale / **orphaned**）不得占额度；
- 非终态（admitted / starting / running / waiting）必须占额度。

其中 orphaned 是本次修复的核心：reconcile 宽限到期只 fence 并释放租约，
**从不把行移出 orphaned**，所以只要它占额度，``max_concurrent_attempts=1``
（默认值）的执行器就被一条失联记录永久占死——该执行器再也不接活，所有依赖
它的合同永远 BLOCKED(CAPACITY_FULL)。宽限期内「不得重复 spawn」由代持租约
保证（SPEC §11.3 第 3 条），不靠额度计数。

反向的 starting / waiting 两个非终态原先被漏掉，额度会被超额放行，
``max_concurrent_attempts`` 形同虚设。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.contract_view import AttemptState
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import save_contract
from longtask.adapters.manifest import Capabilities, Enforcement, SandboxCapability
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.persistence.attempts import count_running_by_executor

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)
CID = "lt-cap-acct"
EXEC = "exec-cap"

# SPEC §7 的 attempt 终态。这里**独立抄写**作为判定口径，不从被测实现
# （ATTEMPT_TERMINAL_STATES）取值——否则断言的期望与实现同源，等于同义反复。
TERMINAL_PER_SPEC = frozenset({"succeeded", "failed", "cancelled", "stale", "orphaned"})


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _draft() -> ContractDraft:
    return ContractDraft(
        title="capacity accounting",
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
    )


def _save(conn: sqlite3.Connection) -> ContractDraft:
    draft = _draft()
    save_contract(conn, contract_id=CID, draft=draft, now=NOW, actor="user")
    return draft


def _attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    state: str,
    executor_id: str = EXEC,
) -> None:
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, executor_id,"
        " state, admitted_at, contract_revision, updated_at)"
        " VALUES (?, ?, ?, 'executor', ?, ?, ?, 1, ?)",
        (
            attempt_id,
            CID,
            CID,
            executor_id,
            state,
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()


def _entry(*, max_concurrent: int = 1) -> RegistryEntry:
    return RegistryEntry(
        id=EXEC,
        kind="stub",
        launch=LaunchSpec(argv=("python", "-c", "pass")),
        capabilities=Capabilities(
            spawn=True,
            observe=True,
            cancel=True,
            notify=False,
            followup=False,
            steer=False,
            interrupt=False,
            context="optional",
            sandbox=SandboxCapability(
                file_effects="workspace-write",
                network="unsupported",
                process="unsupported",
                enforcement=Enforcement.PARTIAL,
            ),
            acceptance_evidence=False,
        ),
        limits={"max_concurrent_attempts": max_concurrent},
        cost_hint=CostHint.MEDIUM,
        enabled=True,
        models=("*",),
    )


@pytest.mark.parametrize("state", [s.value for s in AttemptState])
def test_slot_is_held_by_non_terminal_states_only(state: str, tmp_path: Path) -> None:
    """遍历整个枚举双向钉住：终态不占额度，非终态必须占。"""
    conn = _conn(tmp_path)
    try:
        _save(conn)
        _attempt(conn, "att-x", state=state)
        counted = count_running_by_executor(conn).get(EXEC, 0)
        assert (counted == 1) is (state not in TERMINAL_PER_SPEC), (
            f"state={state!r} 的额度记账与 SPEC §7 不符："
            f"终态不得占额度、非终态必须占额度（实测 counted={counted}）"
        )
    finally:
        conn.close()


def test_every_state_is_classified_exactly_once(tmp_path: Path) -> None:
    """口径必须覆盖全部状态：未分类的状态会静默算作「不占额度」。"""
    conn = _conn(tmp_path)
    try:
        _save(conn)
        for i, state in enumerate(s.value for s in AttemptState):
            _attempt(conn, f"att-{i}", state=state)
        counted = count_running_by_executor(conn).get(EXEC, 0)
        assert counted == len(AttemptState) - len(TERMINAL_PER_SPEC)
    finally:
        conn.close()


def test_orphaned_attempt_does_not_make_the_executor_unavailable(tmp_path: Path) -> None:
    """用户可见后果：一条失联 attempt 不得让执行器再也接不到活。

    同一测试里带对照组——真正在跑的 attempt 必须挡住候选。没有这个对照，
    「orphaned 时选得出候选」可能只是因为门槛根本没生效（假绿）。
    """
    conn = _conn(tmp_path)
    try:
        draft = _save(conn)
        registry = ExecutorRegistry([_entry(max_concurrent=1)])

        _attempt(conn, "att-orphan", state="orphaned")
        survivors = registry.match_candidates(
            draft, running_attempts=count_running_by_executor(conn)
        )
        assert survivors, "orphaned 是终态，不得继续占用执行器额度"

        # 对照组：额度真的会被在跑的 attempt 占满。
        _attempt(conn, "att-running", state="running")
        assert (
            registry.match_candidates(draft, running_attempts=count_running_by_executor(conn)) == []
        )

        # 让 orphan 变回唯一记录，确认不是「多了一条就一定挡住」。
        conn.execute("UPDATE attempts SET state = 'stale' WHERE attempt_id = 'att-running'")
        conn.commit()
        assert registry.match_candidates(draft, running_attempts=count_running_by_executor(conn)), (
            "终态记录不占额度，执行器应重新可用"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["starting", "waiting"])
def test_newly_counted_states_were_previously_letting_work_through(
    state: str, tmp_path: Path
) -> None:
    """反向缺陷：starting/waiting 是非终态，必须占额度（原先被漏掉）。"""
    conn = _conn(tmp_path)
    try:
        draft = _save(conn)
        registry = ExecutorRegistry([_entry(max_concurrent=1)])
        _attempt(conn, "att-live", state=state)
        assert (
            registry.match_candidates(draft, running_attempts=count_running_by_executor(conn)) == []
        ), f"{state} 是非终态，已占用的额度不得再放行第二个 attempt"
    finally:
        conn.close()
