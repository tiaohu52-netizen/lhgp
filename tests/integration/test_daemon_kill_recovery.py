"""Daemon fencing of an attempt whose process vanished mid-execution.

5th-round follow-up (review #1): the existing
``test_real_subprocess_repair_loop`` exercises the
``fail → repair → reverify`` loop, but the restart
happens *after* the subprocess has exited cleanly.  A
``kill -9`` mid-execution leaves the attempt in
``state=RUNNING`` with no terminal event and no
heartbeat.  The daemon's recovery depends on the runner
detecting the vanished process and calling
``mark_attempt_orphaned`` so the contract can be
re-dispatched on the next tick.

This test pins the timeout-fence + orphan-mark path:

1. Registers an adapter that mimics a vanished
   subprocess: ``observe`` reports ``RUNNING`` +
   silent heartbeat; ``cancel`` raises ``OSError`` (the
   process is gone).
2. Dispatches the contract at ``NOW``; the runner
   records the attempt as ``running``.
3. Polls past the contract's ``max_attempt_minutes``
   budget.  The runner's timeout branch tries to
   ``adapter.cancel``; the cancel raises; the runner
   falls into the ``mark_attempt_orphaned`` branch.
4. The attempt is fenced (``orphan-graced`` within
   the grace window, or ``stale`` after the grace
   expires); the runner drops the attempt from its
   in-memory map so the next tick can re-dispatch.
5. The contract itself remains ``ACTIVE`` — fencing
   the attempt is not the same as cancelling the
   contract.

Real-subprocess coverage of the same loop lives in
``test_real_subprocess_repair_loop``; this test uses
the in-memory adapter to keep the runner state
deterministic and fast.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from longtask.adapters.fake_executor import FAKE_MANIFEST, FakeAttemptScript, FakeExecutor
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.contracts.authority import Authority, AuthorityBinding
from longtask.contracts.schema import ContractState
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _seed_active_contract(tmp_path: Path) -> tuple:
    """Persist an ACTIVE contract whose executor_grant covers
    ``exec-fake`` and whose ``max_attempt_minutes`` is 1
    (so the runner's poll can trip the timeout-fence
    branch within a few seconds of test wall time).
    Returns ``(conn, contract_id, workspace)``.
    """
    root = tmp_path / "data"
    root.mkdir()
    workspace = root / "ws"
    workspace.mkdir()
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    cid = "lt-kill-mid-exec"
    save_contract(
        conn,
        ContractDraft(
            title="kill mid exec",
            objective="executor disappears mid-execution; daemon must recover",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard="fake verifies succeeded",
                checks=(),
            ),
            workload_initial_hours=4.0,
            budget=Budget(
                max_dispatches=4,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=1,  # short — runner's poll times out fast
                max_output_bytes=1048576,
            ),
            authority=Authority(
                executor_policy="explicit_allow",
                executors=(AuthorityBinding("exec-fake", ("*"), ("executor", "verifier")),),
            ),
        ),
        contract_id=cid,
        now=NOW,
    )
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    return conn, cid, workspace


def _registry_with_kill_recovery() -> ExecutorRegistry:
    """Build a registry whose ``exec-fake`` entry points at
    a scriptable ``FakeExecutor``.
    """
    registry = ExecutorRegistry()
    registry.register(
        RegistryEntry(
            id="exec-fake",
            kind="fake",
            launch=LaunchSpec(),
            capabilities=FAKE_MANIFEST.capabilities,
            limits={"max_concurrent_attempts": 1},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    return registry


def test_daemon_fences_attempt_after_cancel_raises(tmp_path: Path) -> None:
    """The runner's timeout path with a cancel that raises
    must take the ``mark_attempt_orphaned`` branch — the
    contract stays ACTIVE, the attempt is fenced, the
    runner drops it from ``_running`` so the next tick
    can re-dispatch.
    """
    from longtask.cli.daemon import run_daemon_tick
    from longtask.cli.runner import AttemptRunner
    from longtask.promoter.reconcile import reconcile_attempts

    conn, cid, _ = _seed_active_contract(tmp_path)

    class _UncancellableFake(FakeExecutor):
        """Fake that mimics a vanished subprocess: observe
        reports ``RUNNING`` + silent heartbeat, and cancel
        raises ``OSError`` (the process is gone)."""

        def cancel(self, attempt_id: str, reason: str) -> None:  # type: ignore[override]
            raise OSError(f"process {attempt_id} is gone (simulated kill -9)")

    fake = _UncancellableFake(
        default_script=FakeAttemptScript(
            outcome="hang",
            heartbeat_silent=True,
            cancel_accepted=False,
        ),
    )
    registry = _registry_with_kill_recovery()
    try:
        runner = AttemptRunner(tmp_path / "data", conn, registry)
        runner._adapters["exec-fake"] = fake

        # Tick 1: dispatch.  First attempt is the
        # "killed" one.
        tick1 = run_daemon_tick(tmp_path / "data", conn, registry, now=NOW)
        assert tick1["attempts_started"], (
            "daemon must dispatch a fresh attempt for the active contract"
        )
        first = tick1["attempts_started"][0]
        assert runner.start_attempt(
            NOW + timedelta(seconds=1),
            contract_id=first["contract_id"],
            attempt_id=first["attempt_id"],
            executor_id=first["executor_id"],
        )

        # Poll past the 1-minute timeout.  The cancel
        # raises; the runner takes the
        # ``mark_attempt_orphaned`` branch.
        later = NOW + timedelta(minutes=2, seconds=5)

        def _resolve_adapter(executor_id: str):  # type: ignore[no-untyped-def]
            return fake

        def _locally_tracking(attempt_id: str) -> bool:
            return runner.is_tracking(attempt_id)

        runner.poll_attempts(later)
        reconcile_attempts(
            tmp_path / "data",
            conn,
            now=later,
            resolve_adapter=_resolve_adapter,
            locally_tracked=_locally_tracking,
        )

        # The attempt is fenced.
        attempts_for_first = conn.execute(
            "SELECT attempt_id, state FROM attempts WHERE contract_id = ?",
            (cid,),
        ).fetchall()
        first_state = next(
            (s for aid, s in attempts_for_first if aid == first["attempt_id"]),
            None,
        )
        assert first_state in {
            "stale",
            "orphan-graced",
            "orphaned",
            "fenced",
            "cancelled",
        }, f"timeout-reached attempt with cancel-raising must be fenced; got state={first_state!r}"
        # The runner drops the attempt.
        assert first["attempt_id"] not in runner._running, (
            "the runner must drop a fenced attempt from its "
            "in-memory map so the next tick re-dispatches"
        )
        # The contract itself stays ACTIVE.
        view = get_contract(conn, cid)
        assert view is not None
        assert view.state == ContractState.ACTIVE, (
            f"contract must remain ACTIVE after a vanished-process fence; got {view.state!r}"
        )
        # ATTEMPT_ORPHANED event was written (audit
        # trail of the vanished-process recovery).
        from lhgp.persistence.events_query import get_events

        events = get_events(conn, contract_id=cid)
        orphaned_events = [e for e in events if str(e.event_type) == "attempt/orphaned"]
        assert orphaned_events, (
            "a vanished-process recovery must write ATTEMPT_ORPHANED; "
            f"events={[str(e.event_type) for e in events]}"
        )
    finally:
        conn.close()
