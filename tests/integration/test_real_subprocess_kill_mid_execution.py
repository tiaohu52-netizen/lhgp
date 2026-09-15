"""Real subprocess + simulated daemon kill mid-execution.

5th-round follow-up (review #2): the existing
``test_daemon_kill_recovery`` uses an in-memory ``FakeExecutor``
that mimics a vanished subprocess by raising ``OSError`` in
``cancel``.  That covers the ``cancel-raises → mark_orphaned``
branch in the runner, but it does NOT cover the path that
matters when the **daemon itself** is killed mid-execution:

  1. the runner's Popen is lost (no in-memory tracking);
  2. the subprocess may still be alive in the OS, or may
     have been reaped externally (admin ``kill -9``);
  3. the next ``reconcile_attempts`` call must detect this
     state, fence the attempt, leave the contract ACTIVE
     so it can re-dispatch on the next tick.

This test exercises that path end-to-end:

  - a real ``SubprocessAdapter`` spawns a Python script
    that hangs for 30 seconds (so the subprocess is
    unambiguously alive when we kill the daemon);
  - we capture the underlying ``Popen`` and **kill it
    from outside the runner** (a faithful stand-in for an
    operator running ``kill -9 <pid>`` or the OS reaping
    the daemon's children);
  - the test simulates a daemon restart: close the
    SQLite connection, drop the runner, GC, then
    reopen a fresh connection + a fresh ``AttemptRunner``
    and call ``reconcile_attempts`` exactly the way the
    real daemon's main loop does on cold start;
  - the reconcile pass must fence the attempt with an
    ``attempt/orphaned`` (or equivalent terminal) event,
    and the contract must remain ``ACTIVE`` so the
    next tick can re-dispatch.

The real subprocess flow is what ``SubprocessAdapter.spawn``
+ ``reattach`` were designed for: the persisted handle on
the ``attempts`` row carries the ``pid`` + ``start_time``,
``reattach`` re-binds the same OS process (now already
dead, since the operator killed it), and the
``_reconcile_collected`` branch collects the negative
returncode.
"""

from __future__ import annotations

import contextlib
import gc
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.adapters.subprocess_adapter import SubprocessAdapter
from longtask.contracts.authority import Authority, AuthorityBinding
from longtask.contracts.schema import Acceptance, Budget, ContractDraft, ContractState, Enforcement
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from tests.wait_budget import budget

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 17, 30, 0, tzinfo=UTC)


def _caps() -> Capabilities:
    return Capabilities(
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
        acceptance_evidence=False,
    )


def _hang_30s_code() -> str:
    """Subprocess body: hang long enough for the test to
    kill the daemon, then die, then reconcile to detect.

    Windows ``SIGTERM`` maps to ``TerminateProcess`` so
    we do not need a signal handler — Popen.kill()
    always reaches the kernel and the Python interpreter
    exits unconditionally.
    """
    return "import time, sys; sys.stdout.write('starting\\n'); sys.stdout.flush(); time.sleep(30)"


def _registry(workspace: Path) -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register(
        RegistryEntry(
            id="exec-killmid",
            kind="subprocess",
            launch=LaunchSpec(
                argv=(sys.executable, "-c", _hang_30s_code()),
                cwd=str(workspace),
                env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
            ),
            capabilities=_caps(),
            limits={"max_concurrent_attempts": 1},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    return registry


def _setup(root: Path, workspace: Path) -> tuple[Path, str]:
    db_path = root / "state.db"
    conn = connect(StoreConfig(db_path=db_path))
    ensure_schema(conn)
    cid = "lt-real-kill-mid-exec"
    save_contract(
        conn,
        ContractDraft(
            title="real subprocess kill mid-exec",
            objective=(
                "executor hangs; daemon is killed mid-execution; "
                "next reconcile must fence the attempt"
            ),
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(workspace),
                }
            },
            acceptance=Acceptance(standard="hang recovery", checks=()),
            workload_initial_hours=4.0,
            budget=Budget(
                max_dispatches=4,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=10,
                max_output_bytes=1048576,
            ),
            authority=Authority(
                executor_policy="explicit_allow",
                executors=(AuthorityBinding("exec-killmid", ("*",), ("executor",)),),
            ),
        ),
        contract_id=cid,
        now=NOW,
    )
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    conn.close()
    return db_path, cid


def _wait_subprocess_alive(adapter: SubprocessAdapter, attempt_id: str) -> subprocess.Popen:
    """Wait until the spawned subprocess has flushed its
    first stdout line.  That confirms the Popen is a
    real, distinct OS process and is past the import
    boundary (so its pid is stable and TerminateProcess
    will hit it).
    """
    monitored = adapter._procs[attempt_id]  # type: ignore[attr-defined]
    proc = monitored._proc  # type: ignore[attr-defined]
    deadline = time.monotonic() + budget(30.0)
    while time.monotonic() < deadline:
        if b"starting" in b"".join(monitored.stdout_buf):
            return proc
        time.sleep(0.05)
    raise AssertionError(f"subprocess did not start within 5s; stdout_buf={monitored.stdout_buf!r}")


def test_real_subprocess_daemon_kill_recoverable(tmp_path: Path) -> None:
    """Spawn a real subprocess, simulate a daemon kill
    (close conn + drop runner + Popen.kill() the child
    from outside the runner), then re-open the world
    and run ``reconcile_attempts`` the way the daemon's
    main loop does on cold start.

    Acceptance:

    - the attempt ends up in a fenced terminal state
      (``stale`` / ``orphaned`` / ``fenced`` /
      ``orphan-graced``), the precise label depending on
      the recoverability branch the reconciler took;
    - the contract itself stays ``ACTIVE`` so the next
      tick can re-dispatch;
    - an ``attempt/orphaned`` event is written to the
      audit log (or, when reattach binds the same pid
      and finds it dead, an ``attempt/collected``-class
      terminal event).
    """
    from longtask.cli.daemon import run_daemon_tick
    from longtask.cli.runner import AttemptRunner
    from longtask.promoter.reconcile import reconcile_attempts

    root = tmp_path / "data"
    root.mkdir()
    workspace = root / "ws"
    workspace.mkdir()

    db_path, cid = _setup(root, workspace)
    registry = _registry(workspace)
    registry.save_to_file(root / "registry.json")

    # ── Phase 1: walk the full dispatch path so the
    # ``attempts`` row + lease + ATTEMPT_STARTED event
    # land in the DB exactly the way the daemon would
    # on a normal tick.  We then inject our own
    # SubprocessAdapter so we can introspect the live
    # Popen.
    conn = connect(StoreConfig(db_path=db_path))
    try:
        runner = AttemptRunner(root, conn, registry)
        adapter = SubprocessAdapter(
            manifest=registry.get("exec-killmid").to_manifest(),  # type: ignore[union-attr]
            launch=registry.get("exec-killmid").launch,  # type: ignore[union-attr]
        )
        # Pre-inject so ``_adapter_for`` does not fall
        # through to ``build_adapter`` (which would
        # construct a fresh instance whose ``_procs``
        # table the test cannot reach).
        runner._adapters["exec-killmid"] = adapter

        tick = run_daemon_tick(root, conn, registry, now=NOW)
        assert tick["attempts_started"], (
            f"dispatch tick must start the executor attempt; got {tick!r}"
        )
        started = tick["attempts_started"][0]
        assert started["executor_id"] == "exec-killmid"
        assert runner.start_attempt(
            NOW + timedelta(seconds=1),
            contract_id=started["contract_id"],
            attempt_id=started["attempt_id"],
            executor_id=started["executor_id"],
        )
        attempt_id = started["attempt_id"]
        proc = _wait_subprocess_alive(adapter, attempt_id)

        # Sanity: the attempts row is in admitted /
        # handle-registered state before the kill.
        attempt_row = conn.execute(
            "SELECT state, external_run_id, session_locator FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert attempt_row is not None
        assert attempt_row[0] in {"admitted", "running"}, (
            f"attempts row must be admitted/running before kill; got {attempt_row[0]!r}"
        )
        assert attempt_row[1] and attempt_row[2], (
            "SubprocessAdapter MUST persist the handle (pid + start_time) "
            "so reconcile can re-attach after the daemon dies"
        )

        # ── Phase 2: simulate the daemon being killed.
        # We deliberately do NOT use adapter.cancel (that
        # would be a clean shutdown) — we kill the
        # underlying Popen from outside the adapter, then
        # drop the runner and close the connection.
        child_pid = proc.pid
        proc.kill()
        reap_budget = budget(30.0)
        try:
            proc.wait(timeout=reap_budget)
        except subprocess.TimeoutExpired as err:
            raise AssertionError(
                f"Popen.kill() did not reap child pid {child_pid} within {reap_budget:.0f}s"
            ) from err
        returncode = proc.poll()
        assert returncode is not None, (
            "Popen.poll() must report a returncode after proc.wait() returns"
        )

        # Drop the runner, the adapter, and the
        # connection.  This is the daemon's
        # "kill -9 daemon pid" boundary: in-process
        # state vanishes; only the SQLite file and the
        # subprocess identity persisted on the attempt
        # row survive.
        del runner
        del adapter
        conn.close()
        gc.collect()

        # ── Phase 3: cold-start reconciliation, exactly
        # as the daemon's main loop does on the first
        # tick after restart.  The attempt is non-terminal
        # in the DB (state=running or admitted) but the
        # subprocess is dead; reconcile must detect this
        # and fence the attempt.
        conn2 = connect(StoreConfig(db_path=db_path))
        registry2 = ExecutorRegistry.load_from_file(root / "registry.json")
        runner2 = AttemptRunner(root, conn2, registry2)

        def _resolve_adapter(executor_id):  # type: ignore[no-untyped-def]
            return runner2.adapter_for(executor_id)

        def _locally_tracked(attempt_id):  # type: ignore[no-untyped-def]
            return runner2.is_tracking(attempt_id)

        outcomes = reconcile_attempts(
            root,
            conn2,
            now=NOW + timedelta(seconds=10),
            resolve_adapter=_resolve_adapter,
            locally_tracked=_locally_tracked,
        )
        # The killed attempt must have been processed.
        processed = [o for o in outcomes if o.attempt_id == attempt_id]
        assert processed, (
            f"reconcile must process the killed-mid-exec attempt; "
            f"got outcomes={[o.branch.value for o in outcomes]}"
        )
        outcome = processed[0]
        # The OS already reaped the child, so the
        # reconciler must take the COLLECTED branch (it
        # can reattach the same pid, observe the negative
        # returncode, and collect).  On Windows the
        # reattach's start-time check sometimes fails
        # (TerminateProcess may not refresh start_time)
        # — in that case it falls through to
        # ORPHAN_GRACED / FENCED_REDISPATCHED.  All of
        # these are acceptable; what we forbid is
        # silently reattaching a still-running process.
        assert outcome.branch.value in {
            "collected",
            "orphan-graced",
            "fenced-redispatched",
        }, (
            f"killed subprocess must reach a terminal fencing branch; "
            f"got {outcome.branch.value!r}, detail={outcome.detail!r}"
        )

        # The attempt row in the DB is now terminal.
        attempt_row = conn2.execute(
            "SELECT state FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert attempt_row is not None
        assert attempt_row[0] in {
            "stale",
            "orphaned",
            "orphan-graced",
            "fenced",
            "failed",
            "cancelled",
        }, f"killed subprocess attempt must end in a terminal state; got {attempt_row[0]!r}"

        # The contract itself stays ACTIVE so the next
        # tick can re-dispatch a fresh executor.  This
        # is the load-bearing assertion: fencing the
        # attempt must not cancel the contract.
        view = get_contract(conn2, cid)
        assert view is not None
        assert view.state == ContractState.ACTIVE, (
            f"contract must remain ACTIVE after a killed-mid-exec fence; got {view.state!r}"
        )

        # The audit log records the fence (either
        # ATTEMPT_ORPHANED for the orphan branch, or
        # ATTEMPT_STALE / ATTEMPT_FAILED for the
        # collect-after-kill branch).  The contract
        # must NOT carry a CONTRACT_BLOCKED /
        # CONTRACT_CANCELLED event.
        from lhgp.persistence.events_query import get_events

        events = get_events(conn2, contract_id=cid)
        terminal_attempt_events = {str(e.event_type) for e in events if e.attempt_id == attempt_id}
        assert terminal_attempt_events & {
            "attempt/orphaned",
            "attempt/stale",
            "attempt/failed",
            "attempt/cancelled",
            "attempt/collected",
        }, f"fenced attempt must carry a terminal event; got {sorted(terminal_attempt_events)}"
        contract_level_events = {str(e.event_type) for e in events if e.attempt_id is None}
        assert "contract/cancelled" not in contract_level_events, (
            f"killed-mid-exec must NOT cancel the contract; got {sorted(contract_level_events)}"
        )
        assert "contract/blocked" not in contract_level_events, (
            f"killed-mid-exec must NOT block the contract; got {sorted(contract_level_events)}"
        )
    finally:
        # The conn may already be closed if the test
        # reached Phase 3; guard against that.
        with contextlib.suppress(Exception):
            conn2.close()  # type: ignore[possibly-undefined]
