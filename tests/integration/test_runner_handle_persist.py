"""AttemptRunner 句柄持久化集成测试（SPEC §11.3 MUST 持久返回）。

spawn 成功必须立刻把外部句柄写进 attempts 行（Popen 存内存不算数）：
- 句柄四元组落库（external_run_id / session_locator / recovery_strategy /
  process_identity）；
- handle/registered 事件可审计；
- 适配器拿不出句柄时如实记 handle-unavailable，不伪造可恢复假象；
- fake 适配器（纯内存）声明 nonrecoverable——reconcile 据此走 orphan grace。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.fake_executor import FakeExecutor
from longtask.persistence.attempts import get_attempt
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_events,
    get_lease,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def make_contract(data_dir: Path, cid: str, *, workspace_root: str | None = None) -> None:
    from longtask.contracts.schema import (
        Acceptance,
        Budget,
        ContractDraft,
        ContractState,
    )

    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    hard: dict[str, object] = {}
    if workspace_root is not None:
        hard["file_effects"] = {"mode": "workspace-write", "workspace_root": workspace_root}
    draft = ContractDraft(
        title="句柄持久化测试合同",
        objective="验证 spawn 后句柄立刻落库",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints=hard,
        acceptance=Acceptance(standard="测试通过", checks=("通过",)),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
        ),
    )
    save_contract(conn, draft, contract_id=cid, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    conn.close()


def seed_attempt_row(
    data_dir: Path, cid: str, attempt_id: str, *, executor_id: str = "exec-fake"
) -> None:
    """按 dispatch 落库现场插入 attempts 行（role=executor, state=admitted）。"""
    import json

    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        conn.execute(
            """
            INSERT INTO attempts (
                attempt_id, goal_id, contract_revision, role, executor_id,
                state, admitted_at, started_at, payload_json, updated_at
            ) VALUES (?, ?, 1, 'executor', ?, 'admitted', ?, ?, ?, ?)
            """,
            (
                attempt_id,
                cid,
                executor_id,
                NOW.isoformat(),
                NOW.isoformat(),
                json.dumps({}, ensure_ascii=False),
                NOW.isoformat(),
            ),
        )
        from longtask.persistence.store import acquire_lease

        acquire_lease(
            conn,
            contract_id=cid,
            holder_attempt_id=attempt_id,
            expected_generation=0,
            heartbeat_at=NOW,
            timeout=timedelta(minutes=10),
            actor="daemon",
        )
        conn.commit()
    finally:
        conn.close()


def event_types(conn, cid: str) -> list[str]:
    return [str(e.event_type) for e in get_events(conn, contract_id=cid)]


def test_spawn_persists_fake_handle_with_nonrecoverable_strategy(tmp_path: Path) -> None:
    """fake 适配器 spawn：句柄落库且如实声明 nonrecoverable。"""
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.cli.runner import AttemptRunner

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cid = "lt-20260902-h01"
    make_contract(data_dir, cid)
    seed_attempt_row(data_dir, cid, "att-h01")

    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        registry = ExecutorRegistry()
        runner = AttemptRunner(data_dir, conn, registry)
        runner._adapters["exec-fake"] = FakeExecutor()
        ok = runner.start_attempt(
            NOW,
            contract_id=cid,
            attempt_id="att-h01",
            executor_id="exec-fake",
        )
        assert ok is True
        attempt = get_attempt(conn, "att-h01")
        assert attempt is not None
        # 句柄四元组已落库（§11.3：spawn 持久返回）
        assert attempt.external_run_id == "fake-run-att-h01"
        assert attempt.session_locator == "att-h01"
        assert attempt.recovery_strategy == "nonrecoverable"
        # 纯内存适配器如声明：重启后无从找回
        assert EventType.HANDLE_REGISTERED.value in event_types(conn, cid)
        # 状态已推进 running（句柄注册即观察关系建立）
        assert attempt.state == "running"
    finally:
        conn.close()


def test_spawn_persists_real_subprocess_handle(tmp_path: Path) -> None:
    """subprocess 适配器 spawn：pid+启动时间身份落库，reattach 策略。"""
    from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
    from longtask.cli.runner import AttemptRunner
    from longtask.contracts.schema import Enforcement

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ws").mkdir()
    cid = "lt-20260902-h02"
    make_contract(data_dir, cid, workspace_root=str(data_dir / "ws"))
    seed_attempt_row(data_dir, cid, "att-h02", executor_id="exec-sub")

    manifest = ExecutorManifest(
        executor_id="exec-sub",
        adapter_version="0.1.0a0",
        transport="subprocess",
        capabilities=Capabilities(
            spawn=True,
            observe=True,
            cancel=True,
            notify=False,
            followup=False,
            steer=False,
            interrupt=True,
            context="optional",
            sandbox=SandboxCapability(
                file_effects="workspace-write",
                network="unsupported",
                process="unsupported",
                enforcement=Enforcement.PARTIAL,
            ),
            acceptance_evidence=True,
        ),
    )
    adapter = SubprocessAdapter(
        manifest,
        launch=LaunchSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            env_allowlist=("PATH", "SYSTEMROOT"),
        ),
    )
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        runner = AttemptRunner(data_dir, conn, ExecutorRegistry())
        runner._adapters["exec-sub"] = adapter
        ok = runner.start_attempt(
            NOW,
            contract_id=cid,
            attempt_id="att-h02",
            executor_id="exec-sub",
        )
        assert ok is True, "spawn 应成功：硬约束声明 workspace 且 argv 结构化"
        attempt = get_attempt(conn, "att-h02")
        assert attempt is not None
        assert attempt.recovery_strategy == "reattach"
        assert attempt.session_locator == "att-h02"
        pid = attempt.process_identity.get("pid")
        start = attempt.process_identity.get("start_time")
        assert isinstance(pid, (int, float)) and pid > 0
        assert start is not None
        # 事件 payload 带完整句柄（可审计）
        events = [
            e
            for e in get_events(conn, contract_id=cid)
            if str(e.event_type) == EventType.HANDLE_REGISTERED.value
        ]
        assert len(events) == 1
        import json

        payload = json.loads(events[0].payload_json or "{}")
        assert payload["process_identity"]["pid"] == pid
        assert payload["recovery_strategy"] == "reattach"
    finally:
        # 收尾：取消挂起进程（挂起剧本）防止泄漏；收尾失败不影响断言语义
        try:
            adapter.cancel("att-h02", "测试收尾")
            adapter.collect("att-h02")
        except Exception:  # noqa: S110 —— 清理性收尾，失败无须记日志
            pass
        conn.close()


def test_spawn_without_handle_records_unavailable(tmp_path: Path) -> None:
    """适配器拿不出句柄：记 handle-unavailable 事件，不伪造句柄行。"""

    class _NoHandleFake(FakeExecutor):
        def run_handle(self, attempt_id: str):
            return None

    from longtask.adapters.registry import ExecutorRegistry
    from longtask.cli.runner import AttemptRunner

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cid = "lt-20260902-h03"
    make_contract(data_dir, cid)
    seed_attempt_row(data_dir, cid, "att-h03")

    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        runner = AttemptRunner(data_dir, conn, ExecutorRegistry())
        runner._adapters["exec-fake"] = _NoHandleFake()
        ok = runner.start_attempt(
            NOW,
            contract_id=cid,
            attempt_id="att-h03",
            executor_id="exec-fake",
        )
        assert ok is True
        attempt = get_attempt(conn, "att-h03")
        assert attempt is not None
        # 不伪造：句柄列留空，reconcile 按「状态未知」处理
        assert attempt.external_run_id is None
        assert EventType.HANDLE_REGISTERED.value not in event_types(conn, cid)
    finally:
        conn.close()


def test_daemon_loop_reattach_after_restart(tmp_path: Path) -> None:
    """全链路：runner 拉起（句柄落库）→ 模拟重启 → reconcile 分支 1 重绑。

    真实 SubprocessAdapter（句柄策略=reattach）：新进程通过 attempt_handle()
    从库还原句柄，交给新 runner 的适配器重绑并续租。FakeExecutor 不适用
    于本场景——它如实声明 nonrecoverable，restart 后只能走 orphan grace。
    """
    from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
    from longtask.cli.runner import AttemptRunner
    from longtask.contracts.schema import Enforcement
    from longtask.promoter.reconcile import ReconcileBranch, reconcile_attempts

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ws").mkdir()
    cid = "lt-20260902-h04"
    make_contract(data_dir, cid, workspace_root=str(data_dir / "ws"))
    seed_attempt_row(data_dir, cid, "att-h04", executor_id="exec-sub")

    manifest = ExecutorManifest(
        executor_id="exec-sub",
        adapter_version="0.1.0a0",
        transport="subprocess",
        capabilities=Capabilities(
            spawn=True,
            observe=True,
            cancel=True,
            notify=False,
            followup=False,
            steer=False,
            interrupt=True,
            context="optional",
            sandbox=SandboxCapability(
                file_effects="workspace-write",
                network="unsupported",
                process="unsupported",
                enforcement=Enforcement.PARTIAL,
            ),
            acceptance_evidence=True,
        ),
    )
    launch = LaunchSpec(
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        env_allowlist=("PATH", "SYSTEMROOT"),
    )
    old_adapter = SubprocessAdapter(manifest, launch=launch)

    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        runner = AttemptRunner(data_dir, conn, ExecutorRegistry())
        runner._adapters["exec-sub"] = old_adapter
        assert runner.start_attempt(
            NOW, contract_id=cid, attempt_id="att-h04", executor_id="exec-sub"
        )
        stored = get_attempt(conn, "att-h04")
        assert stored is not None
        assert stored.external_run_id is not None
        assert stored.recovery_strategy == "reattach"

        # 模拟守护进程重启：内存 Popen 全部消失，只靠库里的句柄
        reborn_runner = AttemptRunner(data_dir, conn, ExecutorRegistry())
        reborn_runner._adapters["exec-sub"] = SubprocessAdapter(manifest, launch=launch)
        later = NOW + timedelta(minutes=1)
        results = reconcile_attempts(
            data_dir,
            conn,
            now=later,
            resolve_adapter=reborn_runner.adapter_for,
        )
        assert {r.branch.value for r in results} == {ReconcileBranch.REATTACHED.value}
        # 绑定生效：新 runner 的适配器能观察同一外部 run
        assert reborn_runner.adapter_for("exec-sub").observe("att-h04")["alive"] is True  # type: ignore[union-attr]
        # 续租兑现：租约仍是该 attempt 持有
        lease = get_lease(conn, cid)
        assert lease is not None and lease.holder_attempt_id == "att-h04"
        assert lease.heartbeat_at == later
    finally:
        try:
            old_adapter.cancel("att-h04", "测试收尾")
            old_adapter.collect("att-h04")
        except Exception:  # noqa: S110 —— 清理性收尾，失败无须记日志
            pass
        conn.close()
