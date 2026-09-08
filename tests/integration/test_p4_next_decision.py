"""P4 next_decision_at（SPEC §9、§10）测试。

决策点三信号取最小（最早需要回头看）：
1. 租约到期点（租约死了才谈接管/重派）；
2. 档位复核点（QUEUED 1h / RESPAWN 5m）；
3. deadline 硬上限（越界仲裁不可错过）。

落库语义：不递增 revision、值不变幂等跳过、next-decision/set 事件可审计。
主循环：空闲时睡到最早决策点（不做 60s 盲轮询）；有活 attempt 保底心跳。
L1 RTC：next_decision_at 纳入注册目标。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.decisions import (
    earliest_next_decision_at,
    set_next_decision_at,
)
from longtask.persistence.events import EventType
from longtask.persistence.events_query import get_events
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from longtask.promoter.urgency import UrgencyTier

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def make_draft(deadline: datetime) -> ContractDraft:
    return ContractDraft(
        title="P4 测试合同",
        objective="验证决策点计算",
        deadline_at=deadline,
        hard_constraints={},
        acceptance=Acceptance(standard="测试", checks=("通过",)),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
        ),
    )


def setup_contract(data_dir: Path, cid: str, deadline: datetime) -> object:
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    save_contract(conn, make_draft(deadline), contract_id=cid, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    conn.close()
    return None


class TestComputeNextDecisionAt:
    def _contract_view(self, data_dir: Path, deadline: datetime):
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            return get_contract(conn, "lt-p4a")
        finally:
            conn.close()

    def test_lease_expiry_is_the_decision_point_when_healthy(self, tmp_path: Path) -> None:
        """租约健康：决策点 = 租约到期（不是按档位傻等 15 分钟）。"""
        from longtask.cli.tick import _compute_next_decision_at
        from longtask.persistence.store import acquire_lease

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        deadline = NOW + timedelta(hours=2)
        setup_contract(data_dir, "lt-p4a", deadline)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            lease = acquire_lease(
                conn,
                contract_id="lt-p4a",
                holder_attempt_id="att-1",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=10),
                actor="daemon",
            )
            view = get_contract(conn, "lt-p4a")
            next_at = _compute_next_decision_at(
                view, now=NOW, lease=lease, decision_tier=UrgencyTier.REMIND
            )
            # 租约到期 10 分钟后 vs REMIND 复核 15 分钟后 vs deadline 2h：
            # 最早是租约到期点
            assert next_at == NOW + timedelta(minutes=10)
        finally:
            conn.close()

    def test_tier_cadence_used_without_lease(self, tmp_path: Path) -> None:
        """无租约：按档位复核节奏（RESPAWN=5m）。"""
        from longtask.cli.tick import _compute_next_decision_at

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        deadline = NOW + timedelta(hours=2)
        setup_contract(data_dir, "lt-p4a", deadline)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            view = get_contract(conn, "lt-p4a")
            next_at = _compute_next_decision_at(
                view, now=NOW, lease=None, decision_tier=UrgencyTier.RESPAWN
            )
            assert next_at == NOW + timedelta(minutes=5)
        finally:
            conn.close()

    def test_deadline_is_hard_cap(self, tmp_path: Path) -> None:
        """临近 deadline：决策点封顶在 deadline 前（QUEUED 的 1h 复核也不能越过）。"""
        from longtask.cli.tick import _compute_next_decision_at

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        deadline = NOW + timedelta(minutes=30)
        setup_contract(data_dir, "lt-p4a", deadline)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            view = get_contract(conn, "lt-p4a")
            next_at = _compute_next_decision_at(
                view, now=NOW, lease=None, decision_tier=UrgencyTier.QUEUED
            )
            # QUEUED 复核 60 分钟 vs deadline 30 分钟 - 1s：deadline 封顶
            assert next_at == deadline - timedelta(seconds=1)
        finally:
            conn.close()

    def test_imminent_deadline_clamps_decision_to_now(self, tmp_path: Path) -> None:
        """不足安全边际时立即决策，绝不产生过去的调度时间。"""
        from longtask.cli.tick import _compute_next_decision_at

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        deadline = NOW + timedelta(milliseconds=500)
        setup_contract(data_dir, "lt-p4-imminent", deadline)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            view = get_contract(conn, "lt-p4-imminent")
            next_at = _compute_next_decision_at(
                view, now=NOW, lease=None, decision_tier=UrgencyTier.QUEUED
            )
            assert next_at == NOW
            assert next_at >= NOW
        finally:
            conn.close()


class TestSetNextDecisionAt:
    def test_write_does_not_bump_revision(self, tmp_path: Path) -> None:
        """决策点是调度簿记，不是合同修订：revision 不动。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4b", NOW + timedelta(hours=2))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            before = get_contract(conn, "lt-p4b")
            changed = set_next_decision_at(
                conn,
                contract_id="lt-p4b",
                when=NOW + timedelta(minutes=5),
                now=NOW,
                reason="u-tier 4: re-check at tier cadence",
            )
            assert changed is True
            after = get_contract(conn, "lt-p4b")
            assert after.revision == before.revision
            assert after.next_decision_at == NOW + timedelta(minutes=5)
            types = [str(e.event_type) for e in get_events(conn, contract_id="lt-p4b")]
            assert EventType.NEXT_DECISION_AT_SET.value in types
        finally:
            conn.close()

    def test_same_value_is_idempotent_no_event_spam(self, tmp_path: Path) -> None:
        """值未变：不写事件（重跑 tick 不刷屏）。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4c", NOW + timedelta(hours=2))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            when = NOW + timedelta(minutes=5)
            set_next_decision_at(conn, contract_id="lt-p4c", when=when, now=NOW, reason="r1")
            changed = set_next_decision_at(
                conn, contract_id="lt-p4c", when=when, now=NOW, reason="r2"
            )
            assert changed is False
            count = sum(
                1
                for e in get_events(conn, contract_id="lt-p4c")
                if str(e.event_type) == EventType.NEXT_DECISION_AT_SET.value
            )
            assert count == 1
        finally:
            conn.close()

    def test_unknown_contract_returns_false(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4d", NOW + timedelta(hours=2))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            assert (
                set_next_decision_at(conn, contract_id="lt-missing", when=NOW, now=NOW, reason="r")
                is False
            )
        finally:
            conn.close()


class TestEarliestAcrossContracts:
    def test_earliest_of_multiple_active(self, tmp_path: Path) -> None:
        """多合同取最早决策点（主循环的睡眠依据）。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4e", NOW + timedelta(hours=2))
        setup_contract(data_dir, "lt-p4f", NOW + timedelta(hours=3))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            set_next_decision_at(
                conn, contract_id="lt-p4e", when=NOW + timedelta(minutes=8), now=NOW, reason="a"
            )
            set_next_decision_at(
                conn, contract_id="lt-p4f", when=NOW + timedelta(minutes=3), now=NOW, reason="b"
            )
            earliest = earliest_next_decision_at(conn, now=NOW)
            assert earliest == NOW + timedelta(minutes=3)
        finally:
            conn.close()

    def test_past_points_are_returned_for_immediate_wake(self, tmp_path: Path) -> None:
        """过期决策点原样返回（调用方钳 0 立即唤醒）。

        曾返回 None 导致 daemon 回退整周期休眠：一个 blocked 合同的过期
        点会吞掉 active 合同的未来决策点，deadline 唤醒迟到整周期
        （分支 hardening/deadline-contract-guards，审查 R1）。
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4g", NOW + timedelta(hours=2))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            past = NOW - timedelta(minutes=5)
            set_next_decision_at(conn, contract_id="lt-p4g", when=past, now=NOW, reason="past")
            got = earliest_next_decision_at(conn, now=NOW)
            assert got is not None and got <= NOW
        finally:
            conn.close()


class TestTickIntegration:
    def test_forecast_semantics_ignore_subsecond_slack_drift(self) -> None:
        """Heartbeat-only slack drift must not create a new forecast fact."""
        from longtask.cli.tick import _forecast_semantic_payload

        first = {
            "computed_at": "2026-09-05T01:00:00+00:00",
            "slack_p50_minutes": 12.000,
            "slack_p90_minutes": -3.001,
            "risk": "orange",
        }
        second = {
            "computed_at": "2026-09-05T01:00:02+00:00",
            "slack_p50_minutes": 12.033,
            "slack_p90_minutes": -3.034,
            "risk": "orange",
        }
        assert _forecast_semantic_payload(first) == _forecast_semantic_payload(second)

    def test_forecast_duration_baseline_ignores_failed_attempts(self, tmp_path: Path) -> None:
        """Deadline 快照的历史时长基线只采纳 succeeded executor。"""
        from longtask.adapters.registry import ExecutorRegistry
        from longtask.cli.tick import run_daemon_tick
        from longtask.persistence.events import EventType

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4-history", NOW + timedelta(hours=2))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            # 失败样本只有 1 分钟，成功样本为 10/20/40 分钟；失败样本不能
            # 污染基线，且 p90 必须落到最慢的成功样本。
            conn.executemany(
                """
                INSERT INTO attempts (
                    attempt_id, goal_id, contract_id, contract_revision, role,
                    state, admitted_at, started_at, terminal_at, updated_at
                ) VALUES (?, ?, ?, 2, 'executor', ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "attempt-failed",
                        "lt-p4-history",
                        "lt-p4-history",
                        "failed",
                        NOW.isoformat(),
                        NOW.isoformat(),
                        (NOW + timedelta(minutes=1)).isoformat(),
                        (NOW + timedelta(minutes=1)).isoformat(),
                    ),
                    *[
                        (
                            f"attempt-succeeded-{minutes}",
                            "lt-p4-history",
                            "lt-p4-history",
                            "succeeded",
                            NOW.isoformat(),
                            NOW.isoformat(),
                            (NOW + timedelta(minutes=minutes)).isoformat(),
                            (NOW + timedelta(minutes=minutes)).isoformat(),
                        )
                        for minutes in (10, 20, 40)
                    ],
                ],
            )
            conn.commit()

            run_daemon_tick(data_dir, conn, ExecutorRegistry(), now=NOW)
            forecast_events = [
                event
                for event in get_events(conn, contract_id="lt-p4-history")
                if event.event_type == EventType.FORECAST_UPDATED
            ]
            assert len(forecast_events) == 1
            payload = json.loads(forecast_events[0].payload_json)
            assert payload["sample_count"] == 3
            assert payload["forecast_p50_minutes"] == pytest.approx(30.0)
            assert payload["forecast_p90_minutes"] == pytest.approx(55.0)
        finally:
            conn.close()

    def test_tick_sets_next_decision_for_active_contract(self, tmp_path: Path) -> None:
        """run_daemon_tick 端到端：active 合同跑完一轮后落了决策点。"""
        from longtask.adapters.registry import ExecutorRegistry

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4h", NOW + timedelta(hours=2))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            from longtask.cli.tick import run_daemon_tick

            run_daemon_tick(data_dir, conn, ExecutorRegistry(), now=NOW)
            view = get_contract(conn, "lt-p4h")
            assert view.next_decision_at is not None
            # u = 4h/2h = 2.0 -> RESPAWN(5m)；无租约 → 决策点 = 5 分钟后
            assert view.next_decision_at == NOW + timedelta(minutes=5)
        finally:
            conn.close()

    def test_repeated_ticks_do_not_shift_forecast_decision_or_spam_events(
        self, tmp_path: Path
    ) -> None:
        """高频 tick 保持未来决策点稳定，forecast 只记录事实变化。"""
        from longtask.adapters.registry import ExecutorRegistry
        from longtask.cli.tick import run_daemon_tick

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4i", NOW + timedelta(hours=2))
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            registry = ExecutorRegistry()
            run_daemon_tick(data_dir, conn, registry, now=NOW)
            first = get_contract(conn, "lt-p4i")
            assert first.next_decision_at is not None
            run_daemon_tick(data_dir, conn, registry, now=NOW + timedelta(seconds=1))
            second = get_contract(conn, "lt-p4i")
            assert second.next_decision_at == first.next_decision_at
            forecast_events = [
                event
                for event in get_events(conn, contract_id="lt-p4i")
                if event.event_type == EventType.FORECAST_UPDATED
            ]
            assert len(forecast_events) == 1
        finally:
            conn.close()


class TestDaemonLoopSleepsUntilDecisionPoint:
    """主循环自适应休眠（P4）：空闲时按最早决策点睡，不做 60s 盲轮询。

    模式统一：3 轮（2 次睡眠）+ 空注册表（attempt 起不来 → runner 空闲）。
    决策点由 tick 每轮真实计算（不手动 seed，避免与 tick 重算打架）。
    """

    def _run(self, data_dir: Path, deadline: datetime) -> tuple[dict, list[float]]:
        from longtask.cli.daemon_loop import run_daemon_loop

        # submit-and-leave (2026-09-08): the daemon's startup
        # scan now consumes one ``now_fn`` call before the
        # per-tick loop begins, so provide an extra timestamp
        # for the three subsequent cycles.
        times = iter([NOW, NOW, NOW, NOW])
        sleeps: list[float] = []
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            result = run_daemon_loop(
                data_dir,
                interval_seconds=60.0,
                max_cycles=3,
                now_fn=lambda: next(times),
                sleep_fn=sleeps.append,
                power_port=_NullPower(),
                schedule_port=_NullSchedule(),
            )
        finally:
            conn.close()
        return result, sleeps

    def test_near_deadline_sleeps_less_than_interval(self, tmp_path: Path) -> None:
        """临近 deadline（30s）：决策点被 deadline 封顶 → 睡 29s 而非 60s。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4i", NOW + timedelta(seconds=30))
        result, sleeps = self._run(data_dir, NOW + timedelta(seconds=30))
        assert result["cycles"] == 3
        # 决策点 = deadline - 1s = 29s < interval 60s → 睡 29s
        assert sleeps == [29.0, 29.0]

    def test_active_attempt_still_wakes_for_earlier_deadline_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live attempt must not hide an imminent deadline decision.

        Heartbeat renewal still happens on the next cycle, but sleeping the
        full interval here could cross the contract's hard deadline cap.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4-active", NOW + timedelta(seconds=30))
        monkeypatch.setattr("longtask.cli.daemon_loop.AttemptRunner.is_idle", lambda _runner: False)
        _result, sleeps = self._run(data_dir, NOW + timedelta(seconds=30))
        assert sleeps == [29.0, 29.0]

    def test_far_decision_point_caps_at_interval(self, tmp_path: Path) -> None:
        """决策点很远（deadline 4h，QUEUED/远期）：不睡超过 interval。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        setup_contract(data_dir, "lt-p4j", NOW + timedelta(hours=4))
        result, sleeps = self._run(data_dir, NOW + timedelta(hours=4))
        assert result["cycles"] == 3
        # 决策点远于 interval：按 60s 睡（新事件/新合同的响应窗口）
        assert sleeps == [60.0, 60.0]

    def test_mid_deadline_sleeps_min_of_interval(self, tmp_path: Path) -> None:
        """决策点 45s（介于 29/60 之间）：睡 45s——min 语义的直接验证。"""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        deadline = NOW + timedelta(seconds=45)
        setup_contract(data_dir, "lt-p4k", deadline)
        result, sleeps = self._run(data_dir, deadline)
        assert result["cycles"] == 3
        assert sleeps == [44.0, 44.0]  # deadline - 1s


class _NullPower:
    """无操作电源端口（避免 L0 真实调用）。"""

    held = False

    def update(self, *args, **kwargs) -> None:
        pass


class _NullSchedule:
    """不可用调度端口（L1 走 degraded 路径，不真实注册）。"""

    def is_available(self) -> bool:
        return False

    def arm(self, task_id: str, at: datetime) -> None:
        raise OSError("null schedule")

    def disarm(self, task_id: str) -> None:
        raise OSError("null schedule")
