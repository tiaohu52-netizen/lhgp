"""P3 reconcile 四分支集成测试（SPEC §8、§9 步骤 2、§11.3）。

守护进程重启后对外部 attempt 的恢复语义：
- 分支 1 reattached：能确认同一外部 run 活着 → 重绑 + 续租；
- 分支 2 collected：能确认已终止 → collect 结算 + 释放租约；
- 分支 3 orphan-graced：状态未知 → 标记 orphaned，代持租约阻止重复 spawn；
- 分支 4 fenced-redispatched：宽限到期仍未知 → fence 旧代次，让位重派。

全部用真实 SQLite + FakeExecutor（reattachable_runs 显式声明能力），
不 mock 存储——租约代持与幂等语义必须过真库才可信。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.fake_executor import FakeAttemptScript, FakeExecutor
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.attempts import get_attempt
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    acquire_lease,
    connect,
    ensure_schema,
    get_events,
    get_lease,
    save_contract,
    update_contract_state,
)
from longtask.promoter.reconcile import (
    DEFAULT_RECOVERY_GRACE,
    ReconcileBranch,
    reconcile_attempts,
)
from tests.wait_budget import budget

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def make_contract(data_dir: Path, cid: str) -> None:
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    draft = ContractDraft(
        title="reconcile 测试合同",
        objective="验证四分支恢复语义",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
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


def seed_attempt(
    data_dir: Path,
    *,
    cid: str,
    attempt_id: str,
    state: str,
    handle: dict[str, str] | None = None,
    payload: dict[str, str] | None = None,
    lease_holder: bool = True,
) -> None:
    """模拟重启前的落库现场：attempt 行 + 句柄列 + 活租约。

    handle 为 None 时写老式 session_ref（legacy 路径），否则写 P3 句柄列。
    """
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        payload_json = dict(payload or {})
        external_run_id = session_locator = recovery = None
        if handle is not None:
            external_run_id = handle["external_run_id"]
            session_locator = handle["session_locator"]
            recovery = handle.get("recovery_strategy", "reattach")
        else:
            payload_json.setdefault("session_ref", f"subprocess:{attempt_id}:9999")
        conn.execute(
            """
            INSERT INTO attempts (
                attempt_id, goal_id, contract_revision, role, executor_id,
                state, lease_generation, admitted_at, started_at,
                payload_json, external_run_id, session_locator, recovery_strategy,
                handle_registered_at, updated_at
            ) VALUES (?, ?, 1, 'executor', 'exec-1', ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                cid,
                state,
                NOW.isoformat(),
                NOW.isoformat(),
                _dumps(payload_json),
                external_run_id,
                session_locator,
                recovery,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        if lease_holder:
            acquire_lease(
                conn,
                contract_id=cid,
                holder_attempt_id=attempt_id,
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
                actor="executor",
            )
        conn.commit()
    finally:
        conn.close()


def test_legacy_attempt_with_ambiguous_goal_is_skipped_fail_safe(tmp_path: Path) -> None:
    """旧 attempt 无 contract_id 且 Goal 多合同，不得猜测恢复目标。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    make_contract(data_dir, "contract-a")
    make_contract(data_dir, "contract-b")
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        conn.execute(
            "UPDATE contracts SET goal_id='goal-shared' "
            "WHERE contract_id IN ('contract-a', 'contract-b')"
        )
        conn.commit()
    finally:
        conn.close()
    seed_attempt(data_dir, cid="goal-shared", attempt_id="legacy-ambiguous", state="running")
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        messages: list[str] = []
        outcomes = reconcile_attempts(
            data_dir,
            conn,
            now=NOW + timedelta(minutes=1),
            resolve_adapter=lambda _eid: None,
            emit=messages.append,
        )
        assert outcomes[0].branch == ReconcileBranch.SKIPPED
        assert get_attempt(conn, "legacy-ambiguous").state == "running"
        assert any("ambiguous-contract" in message for message in messages)
    finally:
        conn.close()


def _dumps(data: dict[str, str]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)


def event_types(conn, cid: str) -> list[str]:
    return [str(e.event_type) for e in get_events(conn, contract_id=cid)]


def outcome_map(results: list) -> dict[str, str]:
    return {r.attempt_id: r.branch.value for r in results}


class TestBranch1Reattach:
    def test_alive_external_run_rebinds_and_renews_lease(self, tmp_path: Path) -> None:
        """分支 1：同一外部 run 确认活着 → 状态回 running + 租约续期。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r01"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r1",
            state="running",
            handle={
                "external_run_id": "fake-run-att-r1",
                "session_locator": "att-r1",
            },
        )
        # 模拟重启后「仍联系得到」的外部 run（§11.3 分支 1 的前提）
        executor = FakeExecutor(
            scripts={"att-r1": FakeAttemptScript(outcome="hang")},
            reattachable_runs={"fake-run-att-r1": "att-r1"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            later = NOW + timedelta(minutes=1)
            results = reconcile_attempts(
                data_dir,
                conn,
                now=later,
                resolve_adapter=lambda _eid: executor,
            )
            assert outcome_map(results) == {"att-r1": ReconcileBranch.REATTACHED.value}
            attempt = get_attempt(conn, "att-r1")
            assert attempt is not None and attempt.state == "running"
            # 旧 holder 续租成功：租约仍由该 attempt 持有且心跳已刷新
            lease = get_lease(conn, cid)
            assert lease is not None and lease.holder_attempt_id == "att-r1"
            assert lease.heartbeat_at == later
            assert EventType.RECONCILE_REATTACHED.value in event_types(conn, cid)
        finally:
            conn.close()

    def test_reattach_with_unheld_lease_leaves_lease_untouched(self, tmp_path: Path) -> None:
        """分支 1 但租约不归它（已被回收）：重绑成功，不动租约。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r02"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r2",
            state="running",
            handle={"external_run_id": "fake-run-att-r2", "session_locator": "att-r2"},
            lease_holder=False,
        )
        executor = FakeExecutor(
            scripts={"att-r2": FakeAttemptScript(outcome="hang")},
            reattachable_runs={"fake-run-att-r2": "att-r2"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: executor,
            )
            assert outcome_map(results) == {"att-r2": ReconcileBranch.REATTACHED.value}
            assert get_lease(conn, cid) is None
        finally:
            conn.close()

    def test_not_alive_after_reattach_is_collected_not_assumed(self, tmp_path: Path) -> None:
        """reattach 确认过同一 run，但 observe 报已收尾 → 分支 2 结算。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r03"
        make_contract(data_dir, cid)
        # 剧本 outcome=failed：reattach 成功后 observe 确认外部 run 已以失败收尾
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r3",
            state="running",
            handle={"external_run_id": "fake-run-att-r3", "session_locator": "att-r3"},
        )
        executor = FakeExecutor(
            scripts={"att-r3": FakeAttemptScript(outcome="failed", returncode=3)},
            reattachable_runs={"fake-run-att-r3": "att-r3"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: executor,
            )
            assert outcome_map(results) == {"att-r3": ReconcileBranch.COLLECTED.value}
            attempt = get_attempt(conn, "att-r3")
            assert attempt is not None
            assert attempt.state == "failed"
            assert attempt.return_code == 3
            # 结算后租约释放：写权交还，重派不再被旧代次阻塞
            assert get_lease(conn, cid) is None
        finally:
            conn.close()


class TestBranch2Collect:
    def test_succeeded_external_run_settles_and_releases(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r04"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r4",
            state="running",
            handle={"external_run_id": "fake-run-att-r4", "session_locator": "att-r4"},
        )
        executor = FakeExecutor(
            scripts={"att-r4": FakeAttemptScript(stdout="done")},
            reattachable_runs={"fake-run-att-r4": "att-r4"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            later = NOW + timedelta(minutes=1)
            results = reconcile_attempts(
                data_dir,
                conn,
                now=later,
                resolve_adapter=lambda _eid: executor,
            )
            assert outcome_map(results) == {"att-r4": ReconcileBranch.COLLECTED.value}
            attempt = get_attempt(conn, "att-r4")
            assert attempt is not None and attempt.state == "succeeded"
            assert attempt.return_code == 0
            assert get_lease(conn, cid) is None
            types = event_types(conn, cid)
            assert EventType.ATTEMPT_SUCCEEDED.value in types
            assert EventType.RECONCILE_COLLECTED.value in types
        finally:
            conn.close()

    def test_collect_failure_settles_failed_with_unknown_exit_code(self, tmp_path: Path) -> None:
        """collect 抛错（detached run 常见）：如实结算 failed，退出码不可得不猜。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r05"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r5",
            state="running",
            handle={"external_run_id": "fake-run-att-r5", "session_locator": "att-r5"},
        )

        class _CollectBroken(FakeExecutor):
            def collect(self, attempt_id: str) -> dict[str, object]:
                raise RuntimeError("管道已随原进程消失")

        executor = _CollectBroken(
            scripts={"att-r5": FakeAttemptScript(outcome="failed", returncode=3)},
            reattachable_runs={"fake-run-att-r5": "att-r5"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: executor,
            )
            assert outcome_map(results) == {"att-r5": ReconcileBranch.COLLECTED.value}
            attempt = get_attempt(conn, "att-r5")
            assert attempt is not None
            assert attempt.state == "failed"
            # 退出码不可得 ≠ 成功：return_code 必须为空，绝不猜 0
            assert attempt.return_code is None
            assert attempt.error_class is not None
        finally:
            conn.close()


class TestBranch3OrphanGrace:
    def test_adapter_unavailable_marks_orphaned_and_holds_lease(self, tmp_path: Path) -> None:
        """分支 3：观察不到（适配器没了）→ orphaned + 代持租约。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r06"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r6",
            state="running",
            handle={"external_run_id": "run-6", "session_locator": "att-r6"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            later = NOW + timedelta(minutes=1)
            results = reconcile_attempts(
                data_dir,
                conn,
                now=later,
                resolve_adapter=lambda _eid: None,
            )
            assert outcome_map(results) == {"att-r6": ReconcileBranch.ORPHAN_GRACED.value}
            attempt = get_attempt(conn, "att-r6")
            assert attempt is not None
            assert attempt.state == "orphaned"
            assert attempt.orphaned_at is not None
            # 代持：租约仍是该 attempt 的且心跳已刷新——分发层不会另起会话
            lease = get_lease(conn, cid)
            assert lease is not None and lease.holder_attempt_id == "att-r6"
            assert lease.heartbeat_at == later
            types = event_types(conn, cid)
            assert EventType.ATTEMPT_ORPHANED.value in types
            assert EventType.RECONCILE_ORPHAN_GRACED.value in types
        finally:
            conn.close()

    def test_reattach_refused_marks_orphaned(self, tmp_path: Path) -> None:
        """分支 3：句柄在但适配器无法确认同一 run → orphaned（不是已终止）。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r07"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r7",
            state="running",
            handle={"external_run_id": "run-7", "session_locator": "att-r7"},
        )
        # 未声明 reattachable：FakeExecutor.reattach 返回 False
        executor = FakeExecutor()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: executor,
            )
            assert outcome_map(results) == {"att-r7": ReconcileBranch.ORPHAN_GRACED.value}
            assert get_attempt(conn, "att-r7").state == "orphaned"  # type: ignore[union-attr]
        finally:
            conn.close()

    def test_nonrecoverable_strategy_marks_orphaned(self, tmp_path: Path) -> None:
        """分支 3：句柄声明 nonrecoverable → 无法确认 → orphaned。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r08"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r8",
            state="running",
            handle={
                "external_run_id": "run-8",
                "session_locator": "att-r8",
                "recovery_strategy": "nonrecoverable",
            },
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: FakeExecutor(),
            )
            assert outcome_map(results) == {"att-r8": ReconcileBranch.ORPHAN_GRACED.value}
            assert get_attempt(conn, "att-r8").state == "orphaned"  # type: ignore[union-attr]
        finally:
            conn.close()

    def test_no_handle_at_all_marks_orphaned(self, tmp_path: Path) -> None:
        """分支 3：无句柄无 legacy ref → 状态未知 → orphaned（fail-closed）。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r09"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r9",
            state="running",
            handle={"external_run_id": "", "session_locator": ""},
            payload={},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: FakeExecutor(),
            )
            assert outcome_map(results) == {"att-r9": ReconcileBranch.ORPHAN_GRACED.value}
        finally:
            conn.close()

    def test_grace_is_idempotent_no_event_spam(self, tmp_path: Path) -> None:
        """幂等：宽限期内重跑不重复落事件、不把宽限期续期。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r10"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r10",
            state="running",
            handle={"external_run_id": "run-10", "session_locator": "att-r10"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            t1 = NOW + timedelta(minutes=1)
            t2 = t1 + timedelta(seconds=30)
            reconcile_attempts(data_dir, conn, now=t1, resolve_adapter=lambda _eid: None)
            orphaned_at = get_attempt(conn, "att-r10").orphaned_at  # type: ignore[union-attr]
            reconcile_attempts(data_dir, conn, now=t2, resolve_adapter=lambda _eid: None)
            # orphaned_at 不被重扫覆盖（否则宽限期永远走不完）
            assert get_attempt(conn, "att-r10").orphaned_at == orphaned_at  # type: ignore[union-attr]
            # 事件只落一组
            types = event_types(conn, cid)
            assert types.count(EventType.RECONCILE_ORPHAN_GRACED.value) == 1
            assert types.count(EventType.ATTEMPT_ORPHANED.value) == 1
        finally:
            conn.close()

    def test_locally_tracked_attempt_is_skipped(self, tmp_path: Path) -> None:
        """本进程仍持有活句柄 → 跳过（对活 Popen 重新 reattach 会弄丢 collect 通道）。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r11"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r11",
            state="running",
            handle={"external_run_id": "run-11", "session_locator": "att-r11"},
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: FakeExecutor(),
                locally_tracked=lambda aid: aid == "att-r11",
            )
            assert results == []
            assert get_attempt(conn, "att-r11").state == "running"  # type: ignore[union-attr]
        finally:
            conn.close()


class TestBranch4FenceRedispatch:
    def _seed_orphan(self, data_dir: Path, cid: str, attempt_id: str) -> None:
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id=attempt_id,
            state="running",
            handle={"external_run_id": f"run-{attempt_id}", "session_locator": attempt_id},
        )
        # 先走一轮分支 3 起算宽限
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            reconcile_attempts(data_dir, conn, now=NOW, resolve_adapter=lambda _eid: None)
        finally:
            conn.close()

    def test_within_grace_lease_held_no_fence(self, tmp_path: Path) -> None:
        """宽限未到期：继续代持，不 fence。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r12"
        self._seed_orphan(data_dir, cid, "att-r12")
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            within = NOW + (DEFAULT_RECOVERY_GRACE - timedelta(seconds=1))
            results = reconcile_attempts(
                data_dir, conn, now=within, resolve_adapter=lambda _eid: None
            )
            assert outcome_map(results) == {"att-r12": ReconcileBranch.ORPHAN_GRACED.value}
            assert get_lease(conn, cid) is not None
            assert EventType.RECONCILE_FENCED_REDISPATCHED.value not in event_types(conn, cid)
        finally:
            conn.close()

    def test_grace_expired_fences_lease(self, tmp_path: Path) -> None:
        """宽限到期仍未知：fence 旧 generation，让位重派（分支 4）。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r13"
        self._seed_orphan(data_dir, cid, "att-r13")
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            expired = NOW + DEFAULT_RECOVERY_GRACE + timedelta(seconds=1)
            results = reconcile_attempts(
                data_dir, conn, now=expired, resolve_adapter=lambda _eid: None
            )
            assert outcome_map(results) == {"att-r13": ReconcileBranch.FENCED_REDISPATCHED.value}
            # 租约已释放：旧代次写回会被 LEASE_FENCED 拒绝
            assert get_lease(conn, cid) is None
            assert EventType.RECONCILE_FENCED_REDISPATCHED.value in event_types(conn, cid)
        finally:
            conn.close()

    def test_orphan_with_moved_lease_is_skipped(self, tmp_path: Path) -> None:
        """租约已转给新 holder：旧 attempt 已被 fence 过，本轮跳过。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r14"
        self._seed_orphan(data_dir, cid, "att-r14")
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            # 模拟租约已被重派流程转给新 attempt
            from longtask.persistence.store import reclaim_lease

            reclaim_lease(
                conn,
                contract_id=cid,
                expected_generation=1,
                heartbeat_at=NOW + timedelta(minutes=1),
                timeout=timedelta(minutes=10),
                new_holder_attempt_id="att-new",
                actor="daemon",
                reason="test",
            )
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + DEFAULT_RECOVERY_GRACE + timedelta(minutes=1),
                resolve_adapter=lambda _eid: None,
            )
            assert outcome_map(results) == {"att-r14": ReconcileBranch.SKIPPED.value}
        finally:
            conn.close()


class TestRealSubprocessReconcile:
    """真实 SubprocessAdapter 端到端：spawn → 退出 → 重启（新适配器）→ reconcile。"""

    def _spawn_and_get_handle(self, data_dir: Path, cid: str, attempt_id: str):
        import sys as _sys

        from longtask.adapters.base import AttemptInput
        from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
        from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
        from longtask.contracts.schema import Enforcement

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
        adapter = SubprocessAdapter(manifest, launch=LaunchSpec(argv=(_sys.executable, "-c")))
        input_ = AttemptInput(
            attempt_id=attempt_id,
            contract_id=cid,
            revision=1,
            lease_generation=1,
            role="executor",
            contract_snapshot={
                "objective": "端到端 reconcile",
                "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
            },
            handover_path="handover.md",
            workspace_root=str(data_dir / "ws"),
            budget_remaining={},
            task_prompt="import pathlib; pathlib.Path('done.txt').write_text('ok')",
        )
        (data_dir / "ws").mkdir(exist_ok=True)
        adapter.spawn(input_, adapter.prepare(input_))
        handle = adapter.run_handle(attempt_id)
        assert handle is not None
        # 等外部 run 自行退出（本进程收尸），句柄身份不变
        deadline = time.monotonic() + budget(60.0)
        while time.monotonic() < deadline:
            if not adapter.observe(attempt_id)["alive"]:
                break
            time.sleep(0.2)
        assert adapter.observe(attempt_id)["alive"] is False
        return adapter, handle

    def test_dead_real_run_settles_via_branch2_without_grace(self, tmp_path: Path) -> None:
        """真实死 run：重绑成功（身份证明）→ observe 报 failed → 分支 2 结算。

        这是 _DetachedProcess 死活语义的端到端验收：确认终止的 run 不进
        orphan grace 白等 5 分钟，直接结算。
        """
        import sys as _sys

        from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
        from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
        from longtask.contracts.schema import Enforcement

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r17"
        make_contract(data_dir, cid)
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r17",
            state="running",
        )
        _old_adapter, handle = self._spawn_and_get_handle(data_dir, cid, "att-r17")
        # 把句柄写进 attempt 行（模拟 spawn 时 _persist_handle 已落库）
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            from longtask.persistence.attempts import register_attempt_handle

            register_attempt_handle(
                conn,
                attempt_id="att-r17",
                external_run_id=handle.external_run_id,
                session_locator=handle.session_locator,
                recovery_strategy=handle.recovery_strategy,
                process_identity=handle.process_identity,
                capability_snapshot=handle.capability_snapshot,
                now=NOW,
            )
            conn.commit()
        finally:
            conn.close()
        # 模拟守护进程重启：新适配器实例（内存里没有原 Popen）
        reborn = SubprocessAdapter(
            ExecutorManifest(
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
            ),
            launch=LaunchSpec(argv=(_sys.executable, "-c")),
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: reborn,
            )
            assert outcome_map(results) == {"att-r17": ReconcileBranch.COLLECTED.value}
            attempt = get_attempt(conn, "att-r17")
            assert attempt is not None
            assert attempt.state == "failed"
            # detached collect 不可得：退出码为空不猜
            assert attempt.return_code is None
            assert get_lease(conn, cid) is None
        finally:
            conn.close()

    def test_alive_real_run_reattaches_via_branch1(self, tmp_path: Path) -> None:
        import sys as _sys

        from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
        from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
        from longtask.contracts.schema import Enforcement

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "ws").mkdir()
        cid = "lt-20260902-r18"
        make_contract(data_dir, cid)
        seed_attempt(data_dir, cid=cid, attempt_id="att-r18", state="running")

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
        old_adapter = SubprocessAdapter(manifest, launch=LaunchSpec(argv=(_sys.executable, "-c")))
        from longtask.adapters.base import AttemptInput

        input_ = AttemptInput(
            attempt_id="att-r18",
            contract_id=cid,
            revision=1,
            lease_generation=1,
            role="executor",
            contract_snapshot={
                "objective": "端到端 reattach",
                "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
            },
            handover_path="handover.md",
            workspace_root=str(data_dir / "ws"),
            budget_remaining={},
            task_prompt="import time; time.sleep(30)",
        )
        old_adapter.spawn(input_, old_adapter.prepare(input_))
        handle = old_adapter.run_handle("att-r18")
        assert handle is not None
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            from longtask.persistence.attempts import register_attempt_handle

            register_attempt_handle(
                conn,
                attempt_id="att-r18",
                external_run_id=handle.external_run_id,
                session_locator=handle.session_locator,
                recovery_strategy=handle.recovery_strategy,
                process_identity=handle.process_identity,
                capability_snapshot=handle.capability_snapshot,
                now=NOW,
            )
            conn.commit()
        finally:
            conn.close()
        reborn = SubprocessAdapter(manifest, launch=LaunchSpec(argv=(_sys.executable, "-c")))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            later = NOW + timedelta(minutes=1)
            results = reconcile_attempts(
                data_dir,
                conn,
                now=later,
                resolve_adapter=lambda _eid: reborn,
            )
            assert outcome_map(results) == {"att-r18": ReconcileBranch.REATTACHED.value}
            lease = get_lease(conn, cid)
            assert lease is not None and lease.holder_attempt_id == "att-r18"
            assert lease.heartbeat_at == later
        finally:
            conn.close()
            old_adapter.cancel("att-r18", "测试收尾")
            old_adapter.collect("att-r18")

    def test_legacy_session_ref_uses_poll_strategy(self, tmp_path: Path) -> None:
        """老 attempt（subprocess:<aid>:<pid>）→ poll 句柄，观察不到即 orphaned。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r15"
        make_contract(data_dir, cid)
        seed_attempt(data_dir, cid=cid, attempt_id="att-r15", state="running")
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            # FakeExecutor.reattach 对 poll 句柄（无声明）返回 False → orphan
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: FakeExecutor(),
            )
            assert outcome_map(results) == {"att-r15": ReconcileBranch.ORPHAN_GRACED.value}
        finally:
            conn.close()


class TestScope:
    def test_terminal_attempts_not_scanned(self, tmp_path: Path) -> None:
        """终态 attempt 不进扫描集（重跑不会重复结算）。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r16"
        make_contract(data_dir, cid)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id, goal_id, contract_revision, role, executor_id,
                    state, admitted_at, started_at, terminal_at, payload_json, updated_at
                ) VALUES ('att-r16', ?, 1, 'executor', 'exec-1', 'succeeded',
                          ?, ?, ?, '{}', ?)
                """,
                (cid, NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
            conn.commit()
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: FakeExecutor(),
            )
            assert results == []
        finally:
            conn.close()


class TestLegacyCompat:
    def test_legacy_session_ref_uses_poll_strategy(self, tmp_path: Path) -> None:
        """老 attempt（subprocess:<aid>:<pid>）→ poll 句柄，观察不到即 orphaned。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r15"
        make_contract(data_dir, cid)
        seed_attempt(data_dir, cid=cid, attempt_id="att-r15", state="running")
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            # FakeExecutor.reattach 对 poll 句柄（无声明）返回 False → orphan
            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: FakeExecutor(),
            )
            assert outcome_map(results) == {"att-r15": ReconcileBranch.ORPHAN_GRACED.value}
        finally:
            conn.close()


class TestPostReapSettlement:
    """收尸后窗口：reattach 拒绝（start_time 读不到）但 pid 确认消失 → 分支 2 如实结算。

    场景：runner 进程死亡后外部 run 也退出且已被系统收尸——身份不可证
    （start_time None），但「进程不在」这个事实可以判。直接结算 failed
    （退出码不可得），不白烧 5 分钟 orphan grace。
    """

    def test_dead_pid_after_reattach_refusal_settles_immediately(self, tmp_path: Path) -> None:
        import subprocess
        import sys as _sys

        # 真实子进程：退出 + wait 收尸 → start_time 读不到 + pid（大概率）复用前
        proc = subprocess.Popen(  # noqa: S603 —— 测试固定 argv
            (_sys.executable, "-c", "pass"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=budget(30.0))
        dead_pid = proc.pid

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cid = "lt-20260902-r18"
        make_contract(data_dir, cid)
        # 种 attempt：句柄指向已收尸死进程（身份字段齐全但已失效）
        seed_attempt(
            data_dir,
            cid=cid,
            attempt_id="att-r18",
            state="running",
            handle={
                "external_run_id": str(dead_pid),
                "session_locator": "att-r18",
            },
        )
        # process_identity 需要带 pid（无 start_time 或已失效值都行）
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            from longtask.persistence.attempts import register_attempt_handle

            register_attempt_handle(
                conn,
                attempt_id="att-r18",
                external_run_id=str(dead_pid),
                session_locator="att-r18",
                recovery_strategy="reattach",
                process_identity={"pid": float(dead_pid), "start_time": 1.0},
                capability_snapshot={},
                now=NOW,
            )
            conn.commit()
            # SubprocessAdapter.reattach 因启动时间对不上拒绝（pid 已收尸）
            from longtask.adapters.manifest import Capabilities, ExecutorManifest, SandboxCapability
            from longtask.adapters.subprocess_adapter import LaunchSpec, SubprocessAdapter
            from longtask.contracts.schema import Enforcement

            reborn = SubprocessAdapter(
                ExecutorManifest(
                    executor_id="exec-sub",
                    adapter_version="0",
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
                ),
                launch=LaunchSpec(argv=(_sys.executable, "-c")),
            )
            # pid 死活探测是分支语义的决定因素（真实世界两种都可能）：
            # - False（确认消失）→ 分支 2 立即结算，退出码不可得不猜；
            # - None/True（pid 被复用或权限读不到）→ 身份不可证 → 分支 3。
            from longtask.adapters.processes import process_alive as _pa

            results = reconcile_attempts(
                data_dir,
                conn,
                now=NOW + timedelta(minutes=1),
                resolve_adapter=lambda _eid: reborn,
            )
            if _pa(dead_pid) is False:
                assert outcome_map(results) == {"att-r18": ReconcileBranch.COLLECTED.value}
                attempt = get_attempt(conn, "att-r18")
                assert attempt is not None and attempt.state == "failed"
                assert attempt.return_code is None  # 退出码不可得，不猜
                assert get_lease(conn, cid) is None  # 租约释放，让位重派
            else:
                # pid 被复用/读不到：身份不可证 → orphan grace（正确语义）
                assert outcome_map(results) == {"att-r18": ReconcileBranch.ORPHAN_GRACED.value}
        finally:
            conn.close()
