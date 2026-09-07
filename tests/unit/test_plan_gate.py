"""Plan-mode gate enforcement in ``src/longtask/cli/dispatch.py``.

The gate is opt-in per contract: when ``context.gate == "plan"`` is set
on a draft, ``_dispatch_attempt`` refuses to call any executor until
the contract has a recent ``PLAN_APPROVED`` event that is not
superseded by a later ``PLAN_REJECTED``. This prevents the gate from
being bypassed by any path that doesn't go through ``plan submit`` /
``tool_submit_plan``.

Branches covered:
- gate not set on contract → dispatch passes through (default off)
- gate set, no plan events at all → refused with DISPATCH_REFUSED
- gate set, only PLAN_REJECTED in window → refused
- gate set, PLAN_REJECTED newer than PLAN_APPROVED → refused
- gate set, PLAN_APPROVED newer than PLAN_REJECTED → passes
- gate set, only PLAN_APPROVED (no rejections) → passes
- stale approval outside lookback window → refused
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.base import ExecutorAdapter
from longtask.adapters.manifest import (
    Capabilities,
    Enforcement,
    SandboxCapability,
)
from longtask.adapters.registry import (
    CostHint,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.dispatch import (
    PLAN_GATE_LOOKBACK_SECONDS,
    _dispatch_attempt,
    _has_recent_plan_approval,
    _plan_gate_required,
)
from longtask.contracts.contract_view import ContractState
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
)
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from longtask.promoter.urgency import UrgencyTier

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)


def _make_draft(*, gate: str | None = None, root: Path | None = None) -> ContractDraft:
    workspace_root = str(root / "plan-gate-ws") if root is not None else "plan-gate-ws"
    ctx: dict[str, object] = {"required": True, "limits": {"max_bytes": 8000}}
    if gate is not None:
        ctx["gate"] = gate
    return ContractDraft(
        title="plan-gate test",
        objective="verify plan gate enforcement",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={
            "file_effects": {
                "mode": "workspace-write",
                "workspace_root": workspace_root,
            }
        },
        acceptance=Acceptance(standard="ok", checks=("ok",)),
        workload_initial_hours=2.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=60,
            max_output_bytes=65536,
        ),
        context=ctx,  # type: ignore[arg-type]
    )


def _open_store(root: Path) -> sqlite3.Connection:
    (root / "data").mkdir()
    conn = connect(StoreConfig(db_path=root / "data" / "state.db"))
    ensure_schema(conn)
    return conn


def _save_active(
    conn: sqlite3.Connection,
    cid: str,
    *,
    gate: str | None = None,
    root: Path | None = None,
):
    save_contract(conn, _make_draft(gate=gate, root=root), contract_id=cid, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    view = get_contract(conn, cid)
    assert view is not None
    return view


class _StubAdapter:
    def __init__(self) -> None:
        self.prepare_calls = 0

    def prepare(self, _input: object) -> None:
        self.prepare_calls += 1


def _stub_factory(adapter: _StubAdapter) -> Callable[[RegistryEntry], ExecutorAdapter | None]:
    def factory(_entry: RegistryEntry) -> ExecutorAdapter | None:
        return adapter

    return factory


def _candidate(executor_id: str = "exec-1") -> RegistryEntry:
    return RegistryEntry(
        id=executor_id,
        kind="stub",
        launch=LaunchSpec(argv=(sys.executable, "-c", "pass")),
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
        cost_hint=CostHint.MEDIUM,
        enabled=True,
        models=("*",),
    )


def _event_types(conn: sqlite3.Connection, cid: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT event_type FROM events WHERE contract_id = ?",
            (cid,),
        ).fetchall()
    ]


class TestPlanGateRequired:
    def test_default_off(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-no-gate-flag", gate=None, root=tmp_path)
            assert _plan_gate_required(view) is False
        finally:
            conn.close()

    def test_explicit_plan_gate(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-with-gate-flag", gate="plan", root=tmp_path)
            assert _plan_gate_required(view) is True
        finally:
            conn.close()

    def test_non_string_gate_value_ignored(self, tmp_path: Path) -> None:
        """Defensive: only the literal string "plan" enables the gate.
        Other values (None, booleans, ints) keep the gate off so a
        misconfigured contract doesn't accidentally lock itself out."""
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-other-gate", gate=None, root=tmp_path)
            assert _plan_gate_required(view) is False
        finally:
            conn.close()


class TestHasRecentPlanApproval:
    def test_no_events_returns_false(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            assert _has_recent_plan_approval(conn, "lt-no-events", NOW) is False
        finally:
            conn.close()

    def test_only_approved_returns_true(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-app",
                event_type=EventType.PLAN_APPROVED,
                payload={"step_count": 3},
                now=NOW,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-app", NOW) is True
        finally:
            conn.close()

    def test_rejection_after_approval_supersedes(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-rej",
                event_type=EventType.PLAN_APPROVED,
                payload={},
                now=NOW - timedelta(minutes=5),
                actor="daemon",
            )
            append_event(
                conn,
                contract_id="lt-rej",
                event_type=EventType.PLAN_REJECTED,
                payload={"rejection_reasons": ["no rationale"]},
                now=NOW,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-rej", NOW) is False
        finally:
            conn.close()

    def test_approval_after_rejection_reinstates(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            append_event(
                conn,
                contract_id="lt-fix",
                event_type=EventType.PLAN_REJECTED,
                payload={},
                now=NOW - timedelta(minutes=5),
                actor="daemon",
            )
            append_event(
                conn,
                contract_id="lt-fix",
                event_type=EventType.PLAN_APPROVED,
                payload={},
                now=NOW,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-fix", NOW) is True
        finally:
            conn.close()

    def test_stale_approval_outside_lookback_returns_false(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            stale = NOW - timedelta(seconds=PLAN_GATE_LOOKBACK_SECONDS + 60)
            append_event(
                conn,
                contract_id="lt-stale",
                event_type=EventType.PLAN_APPROVED,
                payload={},
                now=stale,
                actor="daemon",
            )
            assert _has_recent_plan_approval(conn, "lt-stale", NOW) is False
        finally:
            conn.close()


class TestDispatchGateEnforcement:
    def test_gate_off_lets_dispatch_proceed(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-no-gate", gate=None, root=tmp_path)
            adapter = _StubAdapter()
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=lambda _msg: None,
            )
            assert started is not None
            assert adapter.prepare_calls == 1
        finally:
            conn.close()

    def test_gate_on_without_plan_refuses(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-refused", gate="plan", root=tmp_path)
            adapter = _StubAdapter()
            messages: list[str] = []
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=messages.append,
            )
            assert started is None
            assert adapter.prepare_calls == 0
            types = _event_types(conn, "lt-d-refused")
            assert EventType.DISPATCH_REFUSED in types
            assert any("plan-gate" in m for m in messages), messages
        finally:
            conn.close()

    def test_gate_on_with_approval_proceeds(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-ok", gate="plan", root=tmp_path)
            append_event(
                conn,
                contract_id="lt-d-ok",
                event_type=EventType.PLAN_APPROVED,
                payload={"step_count": 3},
                now=NOW - timedelta(seconds=30),
                actor="daemon",
            )
            adapter = _StubAdapter()
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=lambda _msg: None,
            )
            assert started is not None
            assert adapter.prepare_calls == 1
        finally:
            conn.close()

    def test_gate_on_with_rejection_after_approval_refuses(self, tmp_path: Path) -> None:
        conn = _open_store(tmp_path)
        try:
            view = _save_active(conn, "lt-d-supersede", gate="plan", root=tmp_path)
            append_event(
                conn,
                contract_id="lt-d-supersede",
                event_type=EventType.PLAN_APPROVED,
                payload={},
                now=NOW - timedelta(minutes=5),
                actor="daemon",
            )
            append_event(
                conn,
                contract_id="lt-d-supersede",
                event_type=EventType.PLAN_REJECTED,
                payload={"rejection_reasons": ["stale action"]},
                now=NOW,
                actor="daemon",
            )
            adapter = _StubAdapter()
            started = _dispatch_attempt(
                root=tmp_path / "data",
                conn=conn,
                contract=view,
                candidates=[_candidate()],
                now=NOW,
                tier=UrgencyTier.RESPAWN,
                attempt_seq="r001",
                adapter_factory=_stub_factory(adapter),
                emit=lambda _msg: None,
            )
            assert started is None
            assert adapter.prepare_calls == 0
        finally:
            conn.close()
