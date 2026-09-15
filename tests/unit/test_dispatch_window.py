"""派工时间窗（execution.dispatch_window）单元/集成测试。

用户 2026-09-15 令（ZCode 夜间羊毛模式）：合同可声明只在本机墙钟窗口内派工，
窗口外置 next_decision_at 到窗口起点（白天不派工、不烧 dispatch 预算）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dt_time

import pytest

from longtask.adapters.registry import ExecutorRegistry
from longtask.cli.tick import (
    _dispatch_window,
    _parse_hhmm,
    _window_state,
    run_daemon_tick,
)
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
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

pytestmark = pytest.mark.unit

LOCAL = datetime.now().astimezone().tzinfo  # 本机时区（测试对同机确定性）


def local_dt(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL)


NIGHT = (dt_time(23, 0), dt_time(8, 0))


class TestParse:
    def test_valid(self) -> None:
        assert _parse_hhmm("23:00") == dt_time(23, 0)
        assert _parse_hhmm("08:05") == dt_time(8, 5)
        assert _parse_hhmm("7:00") == dt_time(7, 0)

    @pytest.mark.parametrize("bad", ["24:00", "23:60", "2300", "x:y", "", None, 2300])
    def test_invalid(self, bad: object) -> None:
        assert _parse_hhmm(bad) is None


class TestWindowState:
    def test_cross_midnight_inside_late(self) -> None:
        inside, nxt = _window_state(local_dt(2026, 9, 15, 23, 30), NIGHT)
        assert inside is True and nxt is None

    def test_cross_midnight_inside_early(self) -> None:
        inside, _ = _window_state(local_dt(2026, 9, 16, 1, 0), NIGHT)
        assert inside is True

    def test_cross_midnight_end_exclusive(self) -> None:
        inside, nxt = _window_state(local_dt(2026, 9, 15, 8, 0), NIGHT)
        assert inside is False
        assert nxt == local_dt(2026, 9, 15, 23, 0)

    def test_cross_midnight_daytime_next_is_tonight(self) -> None:
        inside, nxt = _window_state(local_dt(2026, 9, 15, 12, 0), NIGHT)
        assert inside is False
        assert nxt == local_dt(2026, 9, 15, 23, 0)

    def test_normal_window_before_open(self) -> None:
        window = (dt_time(9, 0), dt_time(17, 0))
        inside, nxt = _window_state(local_dt(2026, 9, 15, 8, 0), window)
        assert inside is False
        assert nxt == local_dt(2026, 9, 15, 9, 0)

    def test_normal_window_after_close_next_is_tomorrow(self) -> None:
        window = (dt_time(9, 0), dt_time(17, 0))
        inside, nxt = _window_state(local_dt(2026, 9, 15, 18, 0), window)
        assert inside is False
        assert nxt == local_dt(2026, 9, 16, 9, 0)

    def test_normal_window_inside(self) -> None:
        inside, _ = _window_state(local_dt(2026, 9, 15, 12, 0), (dt_time(9, 0), dt_time(17, 0)))
        assert inside is True


def _draft_with_window(deadline: datetime) -> ContractDraft:
    return ContractDraft(
        title="夜间窗口测试",
        objective="验证 dispatch_window 不派工",
        deadline_at=deadline,
        hard_constraints={},
        acceptance=Acceptance(standard="测试", checks=("通过",)),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1048576,
        ),
        execution={"dispatch_window": {"start": "23:00", "end": "08:00"}},
    )


def _setup(data_dir, cid: str, now: datetime) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        ensure_schema(conn)
        save_contract(conn, _draft_with_window(now + timedelta(hours=2)), contract_id=cid, now=now)
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=now)
    finally:
        conn.close()


def _deferred_count(conn, cid: str) -> int:
    return sum(
        1 for e in get_events(conn, contract_id=cid) if e.event_type == EventType.DISPATCH_DEFERRED
    )


class TestTickWindow:
    def test_daytime_dispatch_is_deferred_to_window_start(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        now = local_dt(2026, 9, 15, 12, 0)
        _setup(data_dir, "lt-win-day", now)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            res = run_daemon_tick(data_dir, conn, ExecutorRegistry(), now=now)
            assert res.get("attempts_started") == []
            view = get_contract(conn, "lt-win-day")
            assert view.next_decision_at == local_dt(2026, 9, 15, 23, 0)
            # 同一窗口只落一条 deferred：再来两轮 tick 不新增
            run_daemon_tick(data_dir, conn, ExecutorRegistry(), now=now)
            run_daemon_tick(data_dir, conn, ExecutorRegistry(), now=now)
            assert _deferred_count(conn, "lt-win-day") == 1
        finally:
            conn.close()

    def test_night_dispatch_is_not_deferred(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        now = local_dt(2026, 9, 15, 23, 30)
        _setup(data_dir, "lt-win-night", now)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            run_daemon_tick(data_dir, conn, ExecutorRegistry(), now=now)
            assert _deferred_count(conn, "lt-win-night") == 0
        finally:
            conn.close()

    def test_dispatch_window_parsing_contract(self, tmp_path) -> None:
        data_dir = tmp_path / "data"
        now = local_dt(2026, 9, 15, 12, 0)
        _setup(data_dir, "lt-win-parse", now)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        try:
            view = get_contract(conn, "lt-win-parse")
            assert _dispatch_window(view) == NIGHT
        finally:
            conn.close()
