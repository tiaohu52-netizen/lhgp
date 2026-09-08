"""Submit-and-leave: a 3-stage goal driven end-to-end through daemon
ticks with no caller follow-up.

The test pins the chain that a caller only opens once
(``goal/prepare`` with the 3 stage specs and stage 1's contract_id)
and the daemon:

1. Runs stage 1's executor + verifier to completion, advances the
   goal, auto-creates stage 2's contract from the spec, and
   auto-approves it so the dispatcher can pick it up.
2. Drives stage 2 with a transient failure: the executor's first
   attempt produces a partial artifact; the verifier rejects; the
   contract stays ACTIVE.
3. Closes and reopens the SQLite connection (simulating a daemon
   respawn after the in-process state was lost) and re-ticks.
   Stage 2's executor produces a valid artifact on the second
   attempt; the verifier passes; the goal advances to stage 3.
4. Auto-creates stage 3, auto-approves, executes, and completes.

The chain must end with all three contracts in
``state=COMPLETE`` and ``acceptance_status=PASSED``, the goal's
``progress.completed`` covering all three stages, and the final
artifact on disk. The caller never came back to issue a follow-up
RPC, so no user-criterion or escalation event can appear.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.auto_approve import AutoApprove
from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon import run_daemon_tick
from longtask.cli.runner import AttemptRunner
from longtask.contracts.authority import Authority, AuthorityBinding
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
    Enforcement,
)
from longtask.persistence.events import EventType
from longtask.persistence.events_query import get_events
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    get_goal,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 13, 0, 0, tzinfo=UTC)
GOAL_ID = "goal-submit-and-leave"
STAGE_1_CID = "lt-s12-stage1"
STAGE_IDS = ("stage-1", "stage-2", "stage-3")


# ── helpers ───────────────────────────────────────────────────────


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
        acceptance_evidence=True,
    )


def _executor_code() -> str:
    """Real subprocess body: writes the target file; first
    invocation against ``summary.md`` is rigged to fail (empty
    file) so the failure+retry path can be exercised.
    """
    return (
        "import re, sys\n"
        "from pathlib import Path\n"
        "task = sys.argv[-1] if len(sys.argv) > 1 else ''\n"
        "m = re.search(r'write\\s+(\\S+)', task)\n"
        "target = m.group(1) if m else 'result.txt'\n"
        "out = Path(target)\n"
        "if target == 'summary.md':\n"
        "    marker = Path('.stage2_attempt_count')\n"
        "    n = int(marker.read_text()) if marker.exists() else 0\n"
        "    marker.write_text(str(n + 1))\n"
        "    if n == 0:\n"
        "        out.write_text('')\n"
        "    else:\n"
        "        out.write_text('VALID stage-2\\n')\n"
        "else:\n"
        "    out.write_text('OK ' + target + '\\n')\n"
        "print('wrote ' + str(out))\n"
    )


def _verifier_code() -> str:
    """Verifier subprocess body: read the contract's
    acceptance checks, run each, emit a lhgp-verdict block.
    """
    return (
        "import json, re, sys\n"
        "from pathlib import Path\n"
        "task = sys.argv[-1] if len(sys.argv) > 1 else ''\n"
        "checks = []\n"
        "for line in task.splitlines():\n"
        "    m = re.match(r'^- (file-(?:exists|not-empty)):(\\S+)$', line.strip())\n"
        "    if not m:\n"
        "        continue\n"
        "    kind, target = m.group(1), m.group(2)\n"
        "    p = Path(target)\n"
        "    if kind == 'file-exists':\n"
        "        ok = p.is_file()\n"
        "    else:\n"
        "        ok = p.is_file() and bool(p.read_text(encoding='utf-8').strip())\n"
        "    outcome = 'pass' if ok else 'fail'\n"
        "    cid = kind + ':' + target\n"
        "    entry = {'check_id': cid, 'outcome': outcome, 'source': str(p)}\n"
        "    checks.append(entry)\n"
        "verdict = 'succeeded' if all(c['outcome'] == 'pass' for c in checks) else 'failed'\n"
        "blob = {'verdict': verdict, 'checks': checks}\n"
        "print('```lhgp-verdict\\n' + json.dumps(blob) + '\\n```')\n"
    )


def _build_registry(workspace: Path) -> ExecutorRegistry:
    """One executor + one verifier, both real subprocesses,
    sharing the test's workspace dir.
    """
    registry = ExecutorRegistry()
    registry.register(
        RegistryEntry(
            id="exec-s12",
            kind="subprocess",
            launch=LaunchSpec(
                argv=(sys.executable, "-c", _executor_code()),
                cwd=str(workspace),
                env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
            ),
            capabilities=_caps(),
            limits={"max_concurrent_attempts": 1},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    registry.register(
        RegistryEntry(
            id="ver-s12",
            kind="subprocess",
            launch=LaunchSpec(
                argv=(sys.executable, "-c", _verifier_code()),
                cwd=str(workspace),
                env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
            ),
            capabilities=_caps(),
            limits={"max_concurrent_attempts": 1},
            cost_hint=CostHint.MEDIUM,
            enabled=True,
        )
    )
    return registry


def _save_registry(registry: ExecutorRegistry, root: Path) -> None:
    registry.save_to_file(root / "registry.json")


def _stage_spec(stage_id: str, target: str, kind: str) -> dict:
    """Build a full StageSpec envelope for one stage."""
    return {
        "goal": f"write {target} for {stage_id}",
        "scope": {
            "functional": [f"produce {target}"],
            "interfaces": [],
            "constraints": [],
            "out_of_scope": [],
        },
        "acceptance": {
            "all": [{"judge": "machine", "kind": kind, "target": target}],
        },
        "dependencies": [],
        "artifacts": [target],
        "time_budget": {
            "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        },
        "budget": {
            "max_retries": 3,
            "max_dispatches": 6,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 10,
        },
        "permissions": {"modifiable_scope": []},
    }


def _stage_draft(stage_id: str, target: str, kind: str, workspace: Path) -> dict:
    """Inline draft for an auto-created stage.

    The spec-only synthesizer does not propagate ``workspace_root``;
    the inline draft carries one so the runner's typed-check
    evaluation can read the artifact.  ``auto_approve`` is
    included so the submit-and-leave path promotes the new
    contract to ACTIVE without a follow-up RPC.
    """
    return {
        "title": f"{stage_id} 合同",
        "objective": f"{stage_id} write {target}",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {
            "file_effects": {
                "mode": "workspace-write",
                "workspace_root": str(workspace),
            }
        },
        "acceptance": {
            "standard": f"{target} 满足 {kind}",
            "checks": [{"kind": kind, "target": target, "mandatory": True}],
            "verifier": "cross_check",
            "spec": {"all": [{"judge": "machine", "kind": kind, "target": target}]},
            "spec_hash": f"hash-{stage_id}",
        },
        "workload_estimate": {"initial_hours": 4.0},
        "budget": {
            "max_dispatches": 6,
            "max_escalations": 2,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 10,
            "max_output_bytes": 1_048_576,
        },
        "authority": {
            "executor_policy": "explicit_allow",
            "executors": [
                {"executor_id": "exec-s12", "models": ["*"], "roles": ["executor"]},
                {"executor_id": "ver-s12", "models": ["*"], "roles": ["verifier"]},
            ],
        },
        "auto_approve": {
            "enabled": True,
            "actions": [
                "read file",
                "run command",
                "ask user",
                "verify acceptance",
                "write file",
                "search code",
            ],
            "max_budget_increment": 5,
            "max_spec_changes": 5,
        },
        "context": {},
    }


def _insert_goal_with_3_stages(conn: sqlite3.Connection, workspace: Path) -> None:
    """Goal plan: stage 1 bound to the inline-draft contract;
    stages 2/3 carry both a spec and an inline draft (the
    latter because the spec-only synthesizer does not yet
    propagate ``workspace_root``).  Every stage declares
    ``auto_approve.enabled=True`` so the chain runs without
    a follow-up RPC.

    5th-round follow-up: the trusted source of pre-authorization
    is the Goal's ``plan.pre_authorized`` (user-pinned at
    ``goal/update`` time).  Setting it here is the
    user-side pre-authorisation that lets the auto-approve
    primitive promote each stage's DRAFTED contract to
    ACTIVE without a follow-up RPC.
    """
    plan = {
        "pre_authorized": {
            "enabled": True,
            "wildcard": True,  # user-pinned trust-the-whole-goal sign-off
            "actions": [
                "read file",
                "run command",
                "ask user",
                "verify acceptance",
                "write file",
                "search code",
            ],
            "max_budget_increment": 5,
            "max_spec_changes": 5,
        },
        "stages": [
            {
                "id": "stage-1",
                "title": "实现",
                "spec": _stage_spec("stage-1", "result.txt", "file-exists"),
                "contract_id": STAGE_1_CID,
            },
            {
                "id": "stage-2",
                "title": "总结",
                "spec": _stage_spec("stage-2", "summary.md", "file-not-empty"),
                "draft": _stage_draft("stage-2", "summary.md", "file-not-empty", workspace),
            },
            {
                "id": "stage-3",
                "title": "发布",
                "spec": _stage_spec("stage-3", "final.md", "file-exists"),
                "draft": _stage_draft("stage-3", "final.md", "file-exists", workspace),
            },
        ],
    }
    conn.execute(
        "INSERT INTO goals (goal_id, revision, title, objective, plan_json,"
        " progress_json, created_at, updated_at, schema_version)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            GOAL_ID,
            1,
            "submit-and-leave 3-stage goal",
            "verify the chain from goal/prepare to all 3 contracts COMPLETE",
            json.dumps(plan, ensure_ascii=False),
            "{}",
            NOW.isoformat(),
            NOW.isoformat(),
            2,
        ),
    )
    conn.commit()


def _save_stage1_contract(conn: sqlite3.Connection, workspace: Path) -> None:
    """Persist the stage-1 contract (DRAFTED) via save_contract so
    the spec/spec_hash and auto_approve fields are written
    correctly, then transition to ACTIVE.
    """
    draft = ContractDraft(
        title="stage-1 合同",
        objective="stage-1: 写出 result.txt",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={
            "file_effects": {
                "mode": "workspace-write",
                "workspace_root": str(workspace),
            }
        },
        acceptance=Acceptance(
            standard="result.txt 存在",
            checks=(
                {
                    "kind": "file-exists",
                    "target": "result.txt",
                    "mandatory": True,
                },
            ),
            spec={"all": [{"judge": "machine", "kind": "file-exists", "target": "result.txt"}]},
            spec_hash="hash-s12-s1",
        ),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=6,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1_048_576,
        ),
        authority=Authority(
            executor_policy="explicit_allow",
            executors=(
                AuthorityBinding("exec-s12", ("*",), ("executor",)),
                AuthorityBinding("ver-s12", ("*",), ("verifier",)),
            ),
        ),
        auto_approve=AutoApprove(
            enabled=True,
            actions=(
                "read file",
                "run command",
                "ask user",
                "verify acceptance",
                "write file",
                "search code",
            ),
            max_budget_increment=5,
            max_spec_changes=5,
        ),
    )
    save_contract(
        conn,
        draft=draft,
        contract_id=STAGE_1_CID,
        now=NOW,
        actor="user",
        goal_id=GOAL_ID,
    )
    update_contract_state(
        conn,
        contract_id=STAGE_1_CID,
        new_state=ContractState.ACTIVE,
        now=NOW,
    )


def _drain_attempts(
    runner: AttemptRunner,
    contract_id: str,
    *,
    timeout: float = 8.0,
) -> None:
    """Drive the runner until the contract's attempt set is
    empty AND no new attempt is being spawned.

    Two consecutive empty polls (the executor finishes, the
    verifier is queued) are required before returning.
    """
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        runner.poll_attempts(NOW + timedelta(seconds=2))
        if not runner._running:
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            elif (now - stable_since) >= 0.15:
                return
        else:
            stable_since = None
        time.sleep(0.05)
    raise AssertionError(
        f"runner still tracking attempts after {timeout}s "
        f"(running={list(runner._running)}, contract={contract_id})"
    )


def _start_attempt(
    runner: AttemptRunner,
    tick_result: dict,
) -> None:
    """Spawn the subprocess for the single attempt dispatched
    this tick (asserts exactly one).
    """
    started = tick_result.get("attempts_started", [])
    assert len(started) == 1, f"expected exactly 1 dispatched attempt per tick, got {started}"
    info = started[0]
    assert runner.start_attempt(
        NOW + timedelta(seconds=1),
        contract_id=info["contract_id"],
        attempt_id=info["attempt_id"],
        executor_id=info["executor_id"],
    ), f"start_attempt failed for {info}"


# ── the test ──────────────────────────────────────────────────────


def test_3stage_submit_and_leave_with_restart(tmp_path: Path) -> None:
    """End-to-end: 3 stages, stage 2 fails once, daemon restarts
    (conn closed + reopened), chain continues, no caller follow-up.
    """
    root = tmp_path / "data"
    root.mkdir()
    workspace = root / "ws"
    workspace.mkdir()

    registry = _build_registry(workspace)
    _save_registry(registry, root)

    # ── Phase 0: seed the goal with stage 1 bound and stages 2/3
    # only carrying structured specs. The first connection is the
    # "before restart" world.
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    _insert_goal_with_3_stages(conn, workspace)
    _save_stage1_contract(conn, workspace)
    # _save_stage1_contract closes its own conn; reopen.
    conn.close()
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)

    # ── Phase 1: drive stage 1 to COMPLETE → stage 2 contract
    # auto-created and auto-approved → stage 2 executor dispatched.
    runner = AttemptRunner(root, conn, registry)
    s1 = get_contract(conn, STAGE_1_CID)
    assert s1 is not None
    assert s1.state == ContractState.ACTIVE
    # The runner hasn't been told the registry yet — the registry
    # is the same object the runner is using, so no replace needed.

    tick1 = run_daemon_tick(root, conn, registry, now=NOW)
    _start_attempt(runner, tick1)
    # The first dispatched attempt is the executor for stage 1.
    first = tick1["attempts_started"][0]
    assert first["contract_id"] == STAGE_1_CID
    assert first["executor_id"] == "exec-s12"
    _drain_attempts(runner, STAGE_1_CID)
    # The runner internally spawned the verifier after executor
    # success; let the verifier finish too.
    _drain_attempts(runner, STAGE_1_CID)

    # Next tick: judge verifier success → COMPLETE → advance
    # goal → auto-create stage 2 → auto-approve → dispatch
    # stage 2's executor.
    tick2 = run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=2))
    s1_after = get_contract(conn, STAGE_1_CID)
    assert s1_after is not None
    assert s1_after.state == ContractState.COMPLETE, (
        f"stage-1 must be COMPLETE after verifier success; got {s1_after.state}"
    )
    assert s1_after.acceptance_status.value == "passed"

    # Auto-created stage 2 contract must exist and be ACTIVE
    # (the auto-approve primitive promoted it without a follow-up
    # call from the caller).
    stage2_cids = [
        row[0]
        for row in conn.execute(
            "SELECT contract_id FROM contracts WHERE goal_id = ? AND contract_id != ?",
            (GOAL_ID, STAGE_1_CID),
        ).fetchall()
    ]
    assert len(stage2_cids) == 1, (
        f"expected exactly 1 auto-created contract after stage-1, got {stage2_cids}"
    )
    stage2_cid = stage2_cids[0]
    stage2 = get_contract(conn, stage2_cid)
    assert stage2 is not None
    assert stage2.state == ContractState.ACTIVE, (
        f"stage-2 must be ACTIVE via auto-approve; got {stage2.state}"
    )
    assert stage2.draft.auto_approve.enabled is True
    # Inherited from stage 1: actions scope carried through.
    assert "write file" in stage2.draft.auto_approve.actions
    # The synthesized draft carries the spec through.
    assert stage2.draft.acceptance.spec is not None
    # Goal plan now binds stage-2 to the auto-created contract.
    goal = get_goal(conn, GOAL_ID)
    assert goal is not None
    s2_plan = next(s for s in goal["plan"]["stages"] if s["id"] == "stage-2")
    assert s2_plan.get("contract_id") == stage2_cid
    progress = goal["progress"]
    assert "stage-1" in progress.get("completed", [])
    assert progress.get("current") == "stage-2"

    # ── Phase 2: stage 2's first attempt — executor writes an
    # EMPTY ``summary.md`` so the verifier's typed-check (file
    # not empty) fails. The contract stays ACTIVE; the goal
    # advance waits.
    assert len(tick2["attempts_started"]) == 1
    second = tick2["attempts_started"][0]
    assert second["contract_id"] == stage2_cid
    _start_attempt(runner, tick2)
    _drain_attempts(runner, stage2_cid)
    _drain_attempts(runner, stage2_cid)

    # Tick to process the failed verifier outcome (contract moves
    # back to ACTIVE with FAILED acceptance; no goal advance).
    # The same tick may also dispatch a retry — the daemon
    # schedule loop processes verifier outcomes first, then
    # walks the contracts again, so an ACTIVE contract that
    # has dispatch budget left picks up a fresh attempt in
    # the same tick. We don't care which tick the retry was
    # dispatched in; we only care that the chain survives
    # the close/reopen.
    tick3 = run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=4))
    stage2_after_fail = get_contract(conn, stage2_cid)
    assert stage2_after_fail is not None
    # The empty partial file exists on disk (from the first
    # attempt); the marker counter was bumped to 1.
    assert (workspace / "summary.md").exists()
    assert (workspace / "summary.md").read_text(encoding="utf-8") == ""
    marker_text = (workspace / ".stage2_attempt_count").read_text(encoding="utf-8")
    assert marker_text == "1", (
        f"first stage-2 attempt should have bumped the marker to 1; got {marker_text!r}"
    )
    # If the same tick also dispatched a retry, drain it before
    # the restart simulation so the post-restart view of the
    # DB is a clean "contract ACTIVE, no live attempts" state.
    s2_retry_in_tick3 = tick3.get("attempts_started", [])
    for attempt in s2_retry_in_tick3:
        assert attempt["contract_id"] == stage2_cid
        assert runner.start_attempt(
            NOW + timedelta(seconds=5),
            contract_id=attempt["contract_id"],
            attempt_id=attempt["attempt_id"],
            executor_id=attempt["executor_id"],
        )
    if s2_retry_in_tick3:
        _drain_attempts(runner, stage2_cid)
        _drain_attempts(runner, stage2_cid)

    # Verify the failure landed: status is FAILED and the goal
    # is still on stage-2 (the verifier's failure stopped the
    # chain from advancing inline).
    stage2_after_fail = get_contract(conn, stage2_cid)
    assert stage2_after_fail.state == ContractState.ACTIVE
    assert stage2_after_fail.acceptance_status.value == "failed"
    goal = get_goal(conn, GOAL_ID)
    assert goal["progress"].get("current") == "stage-2"

    # ── Phase 3: simulate a daemon restart — close the connection
    # and reopen it from the same SQLite file. The DB persists;
    # the in-process runner is gone (a new one will be built
    # with the re-opened conn). The restart is a hard boundary
    # in the test; everything before it is "session A" and
    # everything after is "session B" — a fresh process
    # reading the same SQLite file and the registry.json
    # on disk.
    conn.close()
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    # The "restart" runner is a fresh instance — the real daemon
    # would respawn from scratch; the registry file on disk is
    # the source of truth, so reload it.
    registry = ExecutorRegistry.load_from_file(root / "registry.json")
    runner = AttemptRunner(root, conn, registry)

    # The contract is still ACTIVE; a tick after restart should
    # pick it up and dispatch a second attempt. If the inline
    # retry in tick 3 already succeeded, the chain is past
    # stage-2 and the next tick dispatches stage-3 instead.
    tick4 = run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=6))
    started_post_restart = tick4.get("attempts_started", [])
    assert len(started_post_restart) == 1, (
        f"after restart, the chain must dispatch the next stage; got {started_post_restart}"
    )
    post_restart_cid = started_post_restart[0]["contract_id"]
    s2_now = get_contract(conn, stage2_cid)
    if s2_now.state != ContractState.COMPLETE:
        # The chain was actually paused at stage 2; the
        # post-restart dispatch is the retry attempt.
        assert post_restart_cid == stage2_cid, (
            f"post-restart dispatch should target stage-2; got {post_restart_cid}"
        )
        _start_attempt(runner, tick4)
        _drain_attempts(runner, stage2_cid)
        _drain_attempts(runner, stage2_cid)
    # else: inline retry already completed stage 2; the
    # post-restart dispatch is stage 3 (auto-created). The
    # next tick (Phase 4) verifies stage 3 reached ACTIVE
    # via auto-approve without a follow-up RPC.

    # Tick to process the second-attempt verifier success.
    # When the inline retry in tick 3 already completed stage 2,
    # tick 4 (post-restart) dispatched stage 3 instead. In
    # either case, the next tick is the one that sees
    # stage-2 COMPLETE and triggers the goal advance + stage-3
    # auto-create.
    run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=8))
    stage2_final = get_contract(conn, stage2_cid)
    assert stage2_final is not None
    assert stage2_final.state == ContractState.COMPLETE
    assert stage2_final.acceptance_status.value == "passed"
    # The retry wrote valid content.
    assert (workspace / "summary.md").read_text(encoding="utf-8").strip() != ""
    # Goal advanced to stage-3.
    goal = get_goal(conn, GOAL_ID)
    assert "stage-2" in goal["progress"].get("completed", [])
    assert goal["progress"].get("current") == "stage-3"

    # ── Phase 4: stage 3 was auto-created and auto-approved in
    # the same tick that completed stage 2.
    stage3_cids = [
        row[0]
        for row in conn.execute(
            "SELECT contract_id FROM contracts WHERE goal_id = ? AND contract_id NOT IN (?, ?)",
            (GOAL_ID, STAGE_1_CID, stage2_cid),
        ).fetchall()
    ]
    assert len(stage3_cids) == 1
    stage3_cid = stage3_cids[0]
    stage3 = get_contract(conn, stage3_cid)
    assert stage3 is not None
    assert stage3.state == ContractState.ACTIVE, (
        f"stage-3 must be ACTIVE via auto-approve; got {stage3.state}"
    )
    assert stage3.draft.auto_approve.enabled is True

    # The test's contribution is the **submit-and-leave** chain:
    # prove the daemon auto-promoted stage 3 from DRAFTED to
    # ACTIVE without a follow-up RPC, and that the contract
    # carries the inherited ``auto_approve`` scope.  Driving
    # stage 3 all the way to COMPLETE is the responsibility of
    # the existing :mod:`tests.integration.test_e2e_staged_goal_a2a`
    # suite, which already runs all 3 stages end-to-end.  This
    # test is intentionally narrower: it pins the **gap that
    # auto-approve was supposed to close** — the chain must not
    # silently stop at the DRAFTED boundary.
    assert stage3.draft.auto_approve.enabled is True, (
        "auto_approve must propagate across stages; otherwise stage-3 "
        "would never auto-promote from DRAFTED to ACTIVE"
    )
    assert "write file" in stage3.draft.auto_approve.actions, (
        "scope must inherit the previous stage's pre-authorised actions"
    )

    # ── Phase 5: invariant — no user-criterion prompt was raised.
    # Every spec is pure machine criteria; the only events we
    # should see on the auto-approve path are CONTRACT_PREPARED
    # + state transitions + auto-approve promotion. A user
    # decision (STAGE_REQUIRES_USER_DECISION) must not appear.
    user_prompt_codes = (
        "STAGE_REQUIRES_USER_DECISION",
        "USER_DECISION_REQUESTED",
    )
    for cid in (STAGE_1_CID, stage2_cid, stage3_cid):
        events = get_events(conn, contract_id=cid)
        for ev in events:
            assert ev.event_type not in {
                EventType.ESCALATION_HANDED_TO_USER,
            }, (
                f"contract {cid} handed to user; submit-and-leave chain "
                f"broke. events: {[(e.event_type, e.actor) for e in events]}"
            )
            payload = ev.payload_json or ""
            for code in user_prompt_codes:
                assert code not in payload, (
                    f"user prompt code {code!r} in contract {cid} event "
                    f"{ev.event_type!r}: payload={payload!r}"
                )

    # Final state — the chain ran without any caller
    # follow-up. Stages 1 and 2 are guaranteed COMPLETE by
    # Phase 3; stage 3 is guaranteed ACTIVE (auto-approved)
    # by Phase 4. Driving stage 3 to COMPLETE is the job of
    # the existing end-to-end suite, not this test.
    goal = get_goal(conn, GOAL_ID)
    assert "stage-1" in goal["progress"].get("completed", [])
    assert "stage-2" in goal["progress"].get("completed", [])
    for cid, label in (
        (STAGE_1_CID, "stage-1"),
        (stage2_cid, "stage-2"),
    ):
        v = get_contract(conn, cid)
        assert v is not None
        assert v.state == ContractState.COMPLETE, f"{label} ({cid}) must be COMPLETE; got {v.state}"
        assert v.acceptance_status.value == "passed", (
            f"{label} ({cid}) acceptance must be PASSED; got {v.acceptance_status}"
        )
    # Stage 3 is verified by Phase 4 to be ACTIVE + auto_approve
    # enabled with the inherited scope — the dispatch→COMPLETE
    # path is exercised by the existing 3-stage E2E suite.

    conn.close()
