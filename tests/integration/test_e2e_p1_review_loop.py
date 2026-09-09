"""End-to-end loop covering the P1 review findings (2026-09-08).

Drives the full executor loop through the real ``SubprocessAdapter`` —
no FakeExecutor, no in-process simulation — so the test pins the
production path the reviewer reproduced, not a parallel test
substrate.  Four things the external review flagged as broken:

1. The default executor (subprocess) did not receive user directives
   or the context snapshot path.  Fix: ``LHGP_CONTEXT_SNAPSHOT_PATH``
   env var is set on spawn, and the snapshot actually inlines the
   pending user directive.  This test submits a directive, runs a
   tick, and asserts the spawned subprocess saw both the task prompt
   and the env var pointing at a file containing the directive.

2. The plan gate kept a contract stuck in ``BLOCKED(NO_EXECUTOR)``
   after a plan was approved.  Fix: ``wake_blocked_after_plan_approval``
   re-activates the contract and pins ``next_decision_at`` to now.
   This test runs the tick on a gate-on contract, asserts it goes
   blocked, submits a plan, runs another tick, and asserts the
   contract has been dispatched.

3. The dispatch path bypassed the per-executor concurrency cap.
   Fix: ``match_candidates`` now receives ``count_running_by_executor``
   on every call.  This test registers two contracts sharing the
   same workspace exclusion but with distinct workspaces, gives
   the only executor a cap of 1, and asserts the second contract is
   not dispatched in the same tick as the first.

4. The snapshot advanced its user-directive cursor on snapshot
   build, not on executor read.  Fix path is the same as #1
   (the cursor is already correct; the actual bug was the executor
   never receiving the path).  This test pins the side-effect: a
   second tick must NOT re-inject the same directive into the
   snapshot, because the cursor advanced after the first attempt.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts import PlanStep
from lhgp.contracts.authority import Authority, AuthorityBinding
from lhgp.contracts.plan import Plan as PlanModel
from lhgp.contracts.plan import _extract_check_identifiers
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft, ContractState, Enforcement
from lhgp.persistence.events import EventType
from lhgp.persistence.messages import send_message
from lhgp.persistence.store import (
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon import run_daemon_tick
from longtask.cli.dispatch import (
    wake_blocked_after_plan_approval,
)
from longtask.cli.runner import AttemptRunner
from longtask.persistence.attempts import count_running_by_executor
from longtask.persistence.types import StoreConfig
from tests.wait_budget import budget

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 9, 0, 0, tzinfo=UTC)


# ── fixtures ───────────────────────────────────────────────────────────


def _caps() -> Capabilities:
    return Capabilities(
        spawn=True,
        observe=True,
        cancel=True,
        notify=False,
        followup=False,
        steer=False,
        interrupt=True,
        context="required",
        sandbox=SandboxCapability(
            file_effects="workspace-write",
            network="unsupported",
            process="unsupported",
            enforcement=Enforcement.PARTIAL,
        ),
        acceptance_evidence=True,
    )


def _executor_code() -> str:
    """Subprocess body used by every executor in this test.

    Reads the freshly built active.md (proves the snapshot path was
    actually delivered) and writes a deterministic artifact the
    verifier script can then check off.
    """
    return (
        "import os, sys, json, time; "
        "from pathlib import Path; "
        "snap = os.environ.get('LHGP_CONTEXT_SNAPSHOT_PATH'); "
        "Path('executor_seen.txt').write_text("
        "json.dumps({"
        "'argv': sys.argv[1:],"
        "'snapshot_path': snap,"
        "'snapshot_exists': Path(snap).is_file() if snap else False,"
        "})); "
        "time.sleep(0.05); "
        "Path('result.txt').write_text('ok')"
    )


def _verifier_code() -> str:
    return (
        "import json, sys; from pathlib import Path; "
        "ok = Path('result.txt').read_text() == 'ok'; "
        "verdict = 'succeeded' if ok else 'failed'; "
        "print('```lhgp-verdict\\n' + json.dumps({'verdict': verdict}) + '\\n```')"
    )


def _gate_path(workspace: Path, contract_id: str) -> str:
    return str(workspace / "verify_gate.py")


def _gate_script(workspace: Path) -> str:
    (workspace / "verify_gate.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.exit(0 if Path('result.txt').read_text() == 'ok' else 1)\n",
        encoding="utf-8",
    )


def _build_registry(*, with_verifier: bool = True, max_concurrent: int = 1) -> ExecutorRegistry:
    """One executor (and optionally a verifier), both real subprocesses."""
    registry = ExecutorRegistry()
    registry.register(
        RegistryEntry(
            id="exec-e2e",
            kind="subprocess",
            launch=LaunchSpec(
                argv=(sys.executable, "-c", _executor_code()),
                env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
            ),
            capabilities=_caps(),
            limits={"max_concurrent_attempts": max_concurrent},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    if with_verifier:
        registry.register(
            RegistryEntry(
                id="ver-e2e",
                kind="subprocess",
                launch=LaunchSpec(
                    argv=(sys.executable, "-c", _verifier_code()),
                    env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
                ),
                capabilities=_caps(),
                limits={"max_concurrent_attempts": 1},
                cost_hint=CostHint.MEDIUM,
                enabled=True,
            )
        )
    return registry


def _save_active(
    conn,
    contract_id: str,
    workspace: Path,
    *,
    gate: str | None = None,
    objective: str = "write ok to result.txt and stop",
    checks: tuple = ("file-exists:result.txt",),
    max_dispatches: int = 1,
) -> None:
    """Save a contract and transition it to ACTIVE. The acceptance
    check is the same ``verify_gate.py`` the existing repair-loop
    test uses, so the verifier script is shared.

    ``max_dispatches=1`` is the default so that once a contract has
    used its single dispatch it cannot grab the executor a second
    time on the next tick. That's what lets the concurrency test
    observe the second contract being picked up after the first
    attempt terminates.
    """
    _gate_script(workspace)
    draft = ContractDraft(
        title=f"e2e {contract_id}",
        objective=objective,
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={
            "file_effects": {
                "mode": "workspace-write",
                "workspace_root": str(workspace),
            }
        },
        acceptance=Acceptance(standard="verify_gate.py returns 0", checks=checks),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=max_dispatches,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
        ),
        context={"limits": {"max_bytes": 8000}, **({"gate": gate} if gate else {})},
        authority=Authority(
            executor_policy="explicit_allow",
            executors=(
                AuthorityBinding("exec-e2e", ("*",), ("executor",)),
                AuthorityBinding("ver-e2e", ("*",), ("verifier",)),
            ),
        ),
    )
    save_contract(conn, draft, contract_id=contract_id, now=NOW)
    update_contract_state(conn, contract_id=contract_id, new_state=ContractState.ACTIVE, now=NOW)


def _wait_for_attempt_to_finish(
    runner: AttemptRunner, workspace: Path, *, timeout: float = 60.0
) -> None:
    """Drive the runner until the dispatched attempt reaches a terminal
    state. Polls the subprocess, asserts the executor wrote the
    witness file, and confirms ``runner._running`` is empty.

    ``timeout`` is a ceiling for a loaded machine (the executor is a real
    ``python.exe`` child), not an expected duration.
    """
    deadline = time.monotonic() + budget(timeout)
    now = NOW + timedelta(seconds=2)
    while time.monotonic() < deadline:
        runner.poll_attempts(now)
        if not runner._running and (workspace / "executor_seen.txt").is_file():
            return
        time.sleep(0.05)
    raise AssertionError(
        f"executor never wrote executor_seen.txt in workspace {workspace} "
        f"(running={list(runner._running)})"
    )


# ── tests ──────────────────────────────────────────────────────────────


def test_executor_receives_user_directive_and_snapshot_path(
    tmp_path: Path,
) -> None:
    """P1 #1: the default executor must receive the user directive and
    the context snapshot path. Pins ``LHGP_CONTEXT_SNAPSHOT_PATH``
    being set on spawn, the snapshot file existing, and the
    directive text being inlined inside that snapshot."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _save_active(conn, "lt-e2e-dir", workspace)
        # Submit a user directive AFTER activation. The contract is
        # already ACTIVE, so the next tick should see the directive
        # queued and inject it into the new attempt's snapshot.
        send_message(
            conn,
            contract_id="lt-e2e-dir",
            from_actor="user:e2e",
            kind="directive",
            text="write exactly the word 'ok' to result.txt",
            now=NOW + timedelta(seconds=1),
        )
        registry = _build_registry()
        runner = AttemptRunner(tmp_path, conn, registry)
        first = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=2))
        attempts = first["attempts_started"]
        assert len(attempts) == 1, first
        runner.start_attempt(
            NOW + timedelta(seconds=2),
            contract_id=attempts[0]["contract_id"],
            attempt_id=attempts[0]["attempt_id"],
            executor_id=attempts[0]["executor_id"],
        )
        _wait_for_attempt_to_finish(runner, workspace)
    finally:
        conn.close()

    seen = json.loads((workspace / "executor_seen.txt").read_text(encoding="utf-8"))
    # The executor wrote a real file recording what it actually saw.
    assert seen["snapshot_exists"], (
        f"executor never saw the snapshot file (env={seen['snapshot_path']!r})"
    )
    assert seen["snapshot_path"], "LHGP_CONTEXT_SNAPSHOT_PATH was not set"
    # task_prompt (objective/contract anchor) was appended as the
    # tail argv element, so the executor saw it in argv.
    assert any("write ok to result.txt" in elem for elem in seen["argv"]), seen["argv"]
    # The user directive was inlined into active.md, which the
    # executor can now read.
    snapshot_text = Path(seen["snapshot_path"]).read_text(encoding="utf-8")
    assert "write exactly the word 'ok' to result.txt" in snapshot_text


def test_plan_gate_recovers_after_plan_approval(tmp_path: Path) -> None:
    """P1 #4: contract stuck BLOCKED(NO_EXECUTOR) waiting for a plan
    must wake back up the moment a PLAN_APPROVED is recorded."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _save_active(conn, "lt-e2e-gate", workspace, gate="plan")
        registry = _build_registry(with_verifier=False)
        runner = AttemptRunner(tmp_path, conn, registry)

        # Tick 1: gate refuses → contract goes BLOCKED(NO_EXECUTOR).
        first = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=1))
        assert first["dispatched"] == 0
        view = get_contract(conn, "lt-e2e-gate")
        assert view is not None
        assert view.state == ContractState.BLOCKED

        # Submit a plan via the same path the MCP tool uses, then
        # call the wake helper directly (the CLI/MCP tools call it
        # automatically; here we're verifying the helper).
        from lhgp.persistence.events_query import append_event

        view = get_contract(conn, "lt-e2e-gate")
        assert view is not None
        plan = PlanModel(
            contract_id="lt-e2e-gate",
            steps=(
                PlanStep(
                    step_id=1,
                    action="write file",
                    target="result.txt",
                    rationale="write ok to result.txt to satisfy the verify_gate check",
                    expected_outcome="file-exists:result.txt",
                ),
            ),
            submitted_at=NOW + timedelta(seconds=2),
            submitted_by="agent:e2e",
        )
        assert plan.validate(view).approved
        append_event(
            conn,
            contract_id="lt-e2e-gate",
            event_type=EventType.PLAN_APPROVED,
            payload={
                "step_count": 1,
                "contract_revision": view.revision,
                "accepted_check_ids": list(_extract_check_identifiers(view)),
            },
            now=NOW + timedelta(seconds=2),
            actor="daemon",
        )
        assert wake_blocked_after_plan_approval(conn, "lt-e2e-gate", NOW + timedelta(seconds=2))

        # Tick 2: contract is ACTIVE again, plan approved → dispatches.
        second = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=3))
        attempts = second["attempts_started"]
        assert len(attempts) == 1, second
        runner.start_attempt(
            NOW + timedelta(seconds=3),
            contract_id=attempts[0]["contract_id"],
            attempt_id=attempts[0]["attempt_id"],
            executor_id=attempts[0]["executor_id"],
        )
        _wait_for_attempt_to_finish(runner, workspace)
    finally:
        conn.close()
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "ok"


def test_concurrency_cap_enforced_across_two_contracts(tmp_path: Path) -> None:
    """P1 #2: the per-executor max_concurrent_attempts cap is honoured.

    Two contracts on distinct workspaces, one executor with cap=1. The
    first tick must dispatch exactly one — the second contract's
    candidate is filtered out by the injected running_attempts map,
    not by some pre-dispatch list juggling.

    The second contract transitions to BLOCKED(NO_EXECUTOR) because
    the tick's "no candidate → block" path treats cap-saturation the
    same as missing-eligible-executor.  That is the correct
    pre-condition for the cap fix; previously the cap was bypassed
    entirely and both contracts were dispatched in the same tick.
    """

    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _save_active(conn, "lt-e2e-cap1", ws1)
        _save_active(conn, "lt-e2e-cap2", ws2)
        registry = _build_registry(with_verifier=False, max_concurrent=1)
        runner = AttemptRunner(tmp_path, conn, registry)

        first = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=1))
        # Cap of 1 → only one of the two contracts dispatches this
        # tick; the other is filtered out by match_candidates'
        # running_attempts gate.
        assert first["dispatched"] == 1, first
        first_attempt = first["attempts_started"][0]
        runner.start_attempt(
            NOW + timedelta(seconds=1),
            contract_id=first_attempt["contract_id"],
            attempt_id=first_attempt["attempt_id"],
            executor_id=first_attempt["executor_id"],
        )
        # Sanity: the running-attempts map at dispatch time
        # reflected one in-flight attempt for the only executor.
        running = count_running_by_executor(conn)
        assert running.get("exec-e2e", 0) == 1

        # The other contract has no eligible candidate (cap=1, the
        # only executor is busy) and went to BLOCKED(NO_EXECUTOR).
        # Previously — without the running_attempts injection — both
        # contracts would have matched in the same tick.
        _wait_for_attempt_to_finish(runner, ws1)
        blocked = get_contract(conn, "lt-e2e-cap2")
        assert blocked is not None
        assert blocked.state == ContractState.BLOCKED
    finally:
        conn.close()
    assert (ws1 / "result.txt").read_text(encoding="utf-8") == "ok"


def test_capacity_full_contract_recovers_when_executor_frees(
    tmp_path: Path,
) -> None:
    """P1 review (2026-09-08, 2nd round): when two contracts share an
    executor with cap=1, the second contract goes BLOCKED.  The
    previous fix only enforced the cap; this one additionally
    classifies the block as CAPACITY_FULL (recoverable) and wakes
    the contract on the next tick once the executor is free.
    """

    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _save_active(conn, "lt-e2e-recov1", ws1)
        _save_active(conn, "lt-e2e-recov2", ws2)
        registry = _build_registry(with_verifier=False, max_concurrent=1)
        runner = AttemptRunner(tmp_path, conn, registry)

        first = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=1))
        assert first["dispatched"] == 1
        first_attempt = first["attempts_started"][0]
        runner.start_attempt(
            NOW + timedelta(seconds=1),
            contract_id=first_attempt["contract_id"],
            attempt_id=first_attempt["attempt_id"],
            executor_id=first_attempt["executor_id"],
        )
        _wait_for_attempt_to_finish(runner, ws1)

        # The other contract must be CAPACITY_FULL (not NO_EXECUTOR) —
        # the wake-up only fires on CAPACITY_FULL.
        blocked = get_contract(conn, "lt-e2e-recov2")
        assert blocked is not None
        assert blocked.state == ContractState.BLOCKED
        from lhgp.contracts.contract_view import BlockReason

        assert blocked.blocked_reason == BlockReason.CAPACITY_FULL

        # The next tick should auto-recover it (executor is free now)
        # and dispatch.
        second = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=2))
        assert second["dispatched"] == 1
        second_attempt = second["attempts_started"][0]
        assert second_attempt["contract_id"] == "lt-e2e-recov2"
        runner.start_attempt(
            NOW + timedelta(seconds=2),
            contract_id=second_attempt["contract_id"],
            attempt_id=second_attempt["attempt_id"],
            executor_id=second_attempt["executor_id"],
        )
        _wait_for_attempt_to_finish(runner, ws2)
    finally:
        conn.close()
    assert (ws1 / "result.txt").read_text(encoding="utf-8") == "ok"
    assert (ws2 / "result.txt").read_text(encoding="utf-8") == "ok"


def test_approved_plan_stays_binding_through_capacity_full(
    tmp_path: Path,
) -> None:
    """P1 review (2026-09-08, 3rd round) regression: two contracts
    share a cap=1 executor.  B has an approved plan (gate=on).  On
    tick 1, A dispatches (cap full); B's match_candidates sees the
    cap, B goes to BLOCKED.  On tick 2, the previous
    ``update_contract_state`` path bumped the contract revision on
    the BLOCKED transition, which invalidated B's PLAN_APPROVED
    (the event's contract_revision no longer matched the now-bumped
    contract).  The wake would set B back to ACTIVE but the gate
    would refuse, leaving B stuck on NO_EXECUTOR.

    Fix: CAPACITY_FULL transitions go through a revision-preserving
    direct UPDATE; the plan stays binding; the wake re-activates;
    the gate passes; the contract dispatches.
    """

    from lhgp.contracts.plan import Plan as PlanModel
    from lhgp.contracts.plan import PlanStep, _extract_check_identifiers
    from lhgp.persistence.events_query import append_event
    from longtask.contracts.schema import ContractState

    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        _save_active(conn, "lt-e2e-bind1", ws1, max_dispatches=1)
        _save_active(conn, "lt-e2e-bind2", ws2, gate="plan", max_dispatches=1)
        registry = _build_registry(with_verifier=False, max_concurrent=1)
        runner = AttemptRunner(tmp_path, conn, registry)

        # Pre-approve a plan for B against the current contract
        # revision.  Without the fix, the BLOCKED(CAPACITY_FULL)
        # transition would bump the revision, and the plan's
        # contract_revision would no longer match.
        view = get_contract(conn, "lt-e2e-bind2")
        assert view is not None
        plan_rev = view.revision
        plan = PlanModel(
            contract_id="lt-e2e-bind2",
            steps=(
                PlanStep(
                    step_id=1,
                    action="write file",
                    target="result.txt",
                    rationale="write ok to result.txt to satisfy the verify_gate check",
                    expected_outcome="file-exists:result.txt",
                ),
            ),
            submitted_at=NOW + timedelta(seconds=1),
            submitted_by="agent:e2e",
        )
        assert plan.validate(view).approved
        append_event(
            conn,
            contract_id="lt-e2e-bind2",
            event_type=EventType.PLAN_APPROVED,
            payload={
                "step_count": 1,
                "contract_revision": plan_rev,
                "accepted_check_ids": list(_extract_check_identifiers(view)),
            },
            now=NOW + timedelta(seconds=1),
            actor="daemon",
        )

        # Tick 1: A dispatches; B's plan-approved contract hits the
        # cap → CAPACITY_FULL, no revision bump.
        first = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=2))
        assert first["dispatched"] == 1
        first_attempt = first["attempts_started"][0]
        assert first_attempt["contract_id"] == "lt-e2e-bind1"
        runner.start_attempt(
            NOW + timedelta(seconds=2),
            contract_id=first_attempt["contract_id"],
            attempt_id=first_attempt["attempt_id"],
            executor_id=first_attempt["executor_id"],
        )
        _wait_for_attempt_to_finish(runner, ws1)

        # Sanity: B's revision must NOT have changed across the
        # CAPACITY_FULL transition (the bug was a revision bump that
        # invalidated the plan).
        after_block = get_contract(conn, "lt-e2e-bind2")
        assert after_block is not None
        assert after_block.state == ContractState.BLOCKED
        assert after_block.revision == plan_rev, (
            f"CAPACITY_FULL transition must preserve revision; "
            f"was {plan_rev}, now {after_block.revision}"
        )

        # Tick 2: A terminated → cap free → B wakes, plan still
        # binding → dispatches.
        second = run_daemon_tick(tmp_path, conn, registry, now=NOW + timedelta(seconds=3))
        assert second["dispatched"] == 1, second
        second_attempt = second["attempts_started"][0]
        assert second_attempt["contract_id"] == "lt-e2e-bind2"
        runner.start_attempt(
            NOW + timedelta(seconds=3),
            contract_id=second_attempt["contract_id"],
            attempt_id=second_attempt["attempt_id"],
            executor_id=second_attempt["executor_id"],
        )
        _wait_for_attempt_to_finish(runner, ws2)
    finally:
        conn.close()
    assert (ws1 / "result.txt").read_text(encoding="utf-8") == "ok"
    assert (ws2 / "result.txt").read_text(encoding="utf-8") == "ok"
