"""P6 deadline enforcement integration tests (DESIGN §6.4 / lhgp.enforcement).

The :func:`longtask.cli.daemon_loop._enforce_deadlines` hook is what makes
deadlines *binding* in the tick loop. These tests pin:

- BREACHED contract → DEADLINE_BREACH_LOCKED event written + projection rebuilt
- Idempotency: same level on a second call → no extra event
- NORMAL contract → no event, no rebuild
- emit() callback receives a render_text() report line for operator log
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from lhgp.enforcement import DeadlineLevel
from lhgp.persistence.events import EventType
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    get_events,
    save_contract,
    update_contract_state,
)
from longtask.cli.daemon_loop import _enforce_deadlines

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)
CID_BREACH = "lt-p6-enf-breach"
CID_WARNING = "lt-p6-enf-warn"
CID_NORMAL = "lt-p6-enf-normal"


def make_draft(deadline: datetime, *, title: str = "P6 enforcement") -> ContractDraft:
    return ContractDraft(
        title=title,
        objective="验证 deadline 升级/锁住",
        deadline_at=deadline,
        hard_constraints={},
        acceptance=Acceptance(standard="测试", checks=("通过",)),
        workload_initial_hours=2.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
        ),
    )


def _activate(data_dir: Path, cid: str, deadline: datetime) -> None:
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        ensure_schema(conn)
        save_contract(conn, make_draft(deadline), contract_id=cid, now=NOW)
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    finally:
        conn.close()


def _enforcement_events_for(conn, cid: str) -> list[tuple[EventType, dict]]:
    return [
        (e.event_type, _payload(e.payload_json))
        for e in get_events(conn, contract_id=cid)
        if e.event_type in (EventType.DEADLINE_LEVEL_ESCALATED, EventType.DEADLINE_BREACH_LOCKED)
    ]


def _payload(json_text: str) -> dict:
    import json as _json

    return _json.loads(json_text or "{}")


def _open(data_dir: Path):
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return connect(StoreConfig(db_path=data_dir / "state.db"))


class TestEnforceDeadlines:
    def test_breached_contract_writes_lock_event(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # 5 minutes in the past — already breached at NOW
        _activate(data_dir, CID_BREACH, NOW - timedelta(minutes=5))

        captured: list[str] = []
        conn = _open(data_dir)
        try:
            actions = _enforce_deadlines(data_dir, conn, NOW, captured.append)
        finally:
            conn.close()

        assert any(a.level == DeadlineLevel.BREACHED for a in actions)
        # Event written
        conn = _open(data_dir)
        try:
            events = _enforcement_events_for(conn, CID_BREACH)
        finally:
            conn.close()
        assert len(events) == 1
        evt_type, payload = events[0]
        assert evt_type == EventType.DEADLINE_BREACH_LOCKED
        assert payload["level"] == DeadlineLevel.BREACHED.value
        assert "lock_new_attempts" in payload["actions"]
        # render_text emitted something for the operator
        assert any("BREACHED" in line for line in captured)

    def test_idempotent_on_same_level(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _activate(data_dir, CID_BREACH, NOW - timedelta(minutes=5))

        # Two ticks in a row, both at NOW
        for _ in range(2):
            conn = _open(data_dir)
            try:
                _enforce_deadlines(data_dir, conn, NOW, None)
            finally:
                conn.close()

        conn = _open(data_dir)
        try:
            events = _enforcement_events_for(conn, CID_BREACH)
        finally:
            conn.close()
        # Idempotency: only one event written despite two ticks
        assert len(events) == 1, events

    def test_normal_contract_emits_nothing(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Deadline 1 day in the future — NORMAL by every threshold
        _activate(data_dir, CID_NORMAL, NOW + timedelta(days=1))

        captured: list[str] = []
        conn = _open(data_dir)
        try:
            actions = _enforce_deadlines(data_dir, conn, NOW, captured.append)
        finally:
            conn.close()

        assert actions == []
        assert captured == []
        conn = _open(data_dir)
        try:
            events = _enforcement_events_for(conn, CID_NORMAL)
        finally:
            conn.close()
        assert events == []

    def test_warning_contract_writes_escalation(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # 2h total window, 30s remaining — past the 5% urgent threshold but
        # still positive remaining → URGENT level (>=95% elapsed).
        deadline = NOW + timedelta(seconds=30)
        _activate(data_dir, CID_WARNING, deadline)

        # Override created_at to lie about contract start so elapsed_ratio
        # computation reads a meaningful window.
        conn = _open(data_dir)
        try:
            conn.execute(
                "UPDATE contracts SET created_at = ? WHERE contract_id = ?",
                ((NOW - timedelta(hours=2)).isoformat(), CID_WARNING),
            )
        finally:
            conn.close()

        captured: list[str] = []
        conn = _open(data_dir)
        try:
            actions = _enforce_deadlines(data_dir, conn, NOW, captured.append)
        finally:
            conn.close()

        # WARNING or URGENT both qualify; just require an action above NORMAL
        assert any(a.level != DeadlineLevel.NORMAL for a in actions)
        conn = _open(data_dir)
        try:
            events = _enforcement_events_for(conn, CID_WARNING)
        finally:
            conn.close()
        assert len(events) == 1
        evt_type, payload = events[0]
        assert evt_type in (
            EventType.DEADLINE_LEVEL_ESCALATED,
            EventType.DEADLINE_BREACH_LOCKED,
        )
        assert payload["level"] != DeadlineLevel.NORMAL.value

    def test_drops_terminal_contracts(self, tmp_path: Path) -> None:
        """CANCELLED/EXPIRED contracts are skipped — no event spam."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _activate(data_dir, CID_BREACH, NOW - timedelta(minutes=5))

        # Move to CANCELLED before enforcement
        conn = _open(data_dir)
        try:
            update_contract_state(
                conn,
                contract_id=CID_BREACH,
                new_state=ContractState.CANCELLED,
                now=NOW,
            )
        finally:
            conn.close()

        conn = _open(data_dir)
        try:
            actions = _enforce_deadlines(data_dir, conn, NOW, None)
        finally:
            conn.close()

        assert actions == []
        conn = _open(data_dir)
        try:
            events = _enforcement_events_for(conn, CID_BREACH)
        finally:
            conn.close()
        assert events == []


class TestEnforceDeadlinesReachesTickLoop:
    """Smoke-test the wiring: starting a real daemon runs the hook."""

    def test_daemon_loop_invokes_enforce(self, tmp_path: Path) -> None:
        from longtask.adapters.registry import ExecutorRegistry
        from longtask.cli.daemon_loop import run_daemon_loop

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _activate(data_dir, CID_BREACH, NOW - timedelta(minutes=5))

        # Empty executor registry — tick should still call _enforce_deadlines
        # before run_daemon_tick, and exit cleanly.
        reg = ExecutorRegistry()
        reg_path = data_dir / "registry.json"
        reg.save_to_file(reg_path)

        emit_calls: list[str] = []
        result = run_daemon_loop(
            data_dir,
            max_cycles=1,
            now_fn=lambda: NOW,
            interval_seconds=0,
            emit_fn=emit_calls.append,
        )
        assert result["cycles"] == 1

        conn = _open(data_dir)
        try:
            events = _enforcement_events_for(conn, CID_BREACH)
            view = get_contract(conn, CID_BREACH)
        finally:
            conn.close()
        assert len(events) == 1
        assert events[0][0] == EventType.DEADLINE_BREACH_LOCKED
        # Projection should now report deadline_status==MISSED
        assert view is not None
        from lhgp.contracts.contract_view import DeadlineStatus

        assert view.deadline_status == DeadlineStatus.MISSED
