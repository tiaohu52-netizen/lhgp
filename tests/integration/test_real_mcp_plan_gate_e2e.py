"""End-to-end through the real MCP path with the plan gate ON.

6th-round audit follow-up: the previous MCP submit-and-leave
E2E (test_mcp_submit_and_leave_e2e_with_goal_pre_authorized_and_execution_config)
exercised the no-gate path — the contract's ``context.gate``
was not set, so the dispatcher's plan-gate check was off and a
PLAN_APPROVED was never required.  The reviewer's real-MCP
repro targeted ``context.gate="plan"`` and uncovered three
real bugs:

  1. plan approval went stale across the auto-activate
     revision bump (the gate read payload.contract_revision,
     the migration re-stamped events.contract_revision);
  2. bounded ``pre_authorized`` Goal + MCP-stripped
     ``auto_approve`` claim → contract stuck in DRAFTED;
  3. verifier success event left at the old revision when
     the CANDIDATE transition bumped the contract → user
     confirm's evidence lookup synthesised a stub and lost
     the real verifier record.

This test exercises all three at once through the real MCP
tool path + a real ``SubprocessAdapter`` executor + a real
verifier.  Acceptance:

  - the plan gate accepts the contract on dispatch (case 1);
  - the bounded pre_authorized Goal auto-activates the
    MCP-issued contract without ``wildcard=True`` (case 2);
  - the verifier event is carried across the CANDIDATE
    revision bump and is the source of the
    user_confirmed evidence (case 3 — no synthesised stub).

Like the kill-mid-exec E2E, the executor + verifier are
real ``Popen`` children writing real artifacts; the daemon
runs through a real ``run_daemon_tick`` + ``AttemptRunner``;
the user_confirm is issued via the same handler the CLI
uses (``handle_contract_user_confirm`` with a Principal
envelope).
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.contracts.authority import Authority, AuthorityBinding
from longtask.contracts.schema import ContractDraft, ContractState, Enforcement
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 9, 13, 0, 0, tzinfo=UTC)


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
    return (
        "from pathlib import Path; "
        # Write the artifact the verifier will check.
        "Path('result.txt').write_text('good')"
    )


def _verifier_code() -> str:
    return (
        "import json; from pathlib import Path; "
        "ok = Path('result.txt').read_text() == 'good'; "
        "print('```lhgp-verdict\\n' + "
        "json.dumps({'verdict': 'succeeded' if ok else 'failed', "
        "'checks': [{'check_id': 'c1', 'outcome': 'pass' if ok else 'fail'}]}) "
        "+ '\\n```')"
    )


def _registry(workspace: Path) -> ExecutorRegistry:
    registry = ExecutorRegistry()
    for entry_id, code, cost in (
        ("exec-plan", _executor_code(), CostHint.LOW),
        ("ver-plan", _verifier_code(), CostHint.MEDIUM),
    ):
        registry.register(
            RegistryEntry(
                id=entry_id,
                kind="subprocess",
                launch=LaunchSpec(
                    argv=(sys.executable, "-c", code),
                    cwd=str(workspace),
                    env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
                ),
                capabilities=_caps(),
                limits={"max_concurrent_attempts": 1},
                cost_hint=cost,
                enabled=True,
            )
        )
    return registry


def _seed_goal_and_authority(conn, goal_id: str, workspace: Path) -> None:
    """Drop a goal-bootstrap contract + a real contract whose
    authority binds the two registry entries.  The real
    contract carries ``context.gate="plan"`` so the dispatcher
    requires a recent PLAN_APPROVED before any executor runs.
    """
    save_contract(
        conn,
        ContractDraft(
            title="goal bootstrap",
            objective="x",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=__import__("lhgp.contracts.acceptance", fromlist=["Acceptance"]).Acceptance(
                standard="s", checks=("c1",)
            ),
            workload_initial_hours=4.0,
            budget=__import__("lhgp.contracts.budget", fromlist=["Budget"]).Budget(
                5, 1, 1, 30, 1_048_576, 2
            ),
        ),
        contract_id=f"{goal_id}-bootstrap",
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    from lhgp.contracts.acceptance import Acceptance
    from lhgp.contracts.budget import Budget

    save_contract(
        conn,
        ContractDraft(
            title="write a file under plan gate",
            objective="write result.txt for the verifier",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(workspace),
                }
            },
            acceptance=Acceptance(
                standard="user confirm",
                checks=("c1",),
                spec_hash="hash-plan",
                # Single user criterion — machine work is
                # not in the spec, so the judge's success
                # path lands at CANDIDATE (waiting for
                # user-confirm).  This is the lifecycle bump
                # the user_confirm migration is supposed to
                # cover.
                spec={"all": [{"judge": "user"}]},
            ),
            workload_initial_hours=4.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
            authority=Authority(
                executor_policy="explicit_allow",
                executors=(
                    AuthorityBinding("exec-plan", ("*",), ("executor",)),
                    AuthorityBinding("ver-plan", ("*",), ("verifier",)),
                ),
            ),
            context={"gate": "plan"},
        ),
        contract_id="lt-plan-gate-cid",
        now=NOW,
        actor="model",
        goal_id=goal_id,
    )


def test_real_mcp_plan_gate_dispatches_through_to_user_confirm(tmp_path: Path) -> None:
    """Full chain through the real MCP path:

    1. ``tool_prepare_contract`` strips the model's
       ``auto_approve`` claim.
    2. ``tool_submit_plan`` writes a PLAN_APPROVED whose
       action (``write file``) is in the Goal's bounded
       pre_authorized grant.
    3. ``auto_approve_drafted_contract`` promotes the
       DRAFTED contract to ACTIVE on the new bounded-grant
       override (no wildcard needed).
    4. ``run_daemon_tick`` + ``AttemptRunner`` + real
       ``SubprocessAdapter`` execute the contract, then
       the runner auto-dispatches the independent verifier.
    5. ``_judge_verifier_outcomes`` transitions ACTIVE →
       CANDIDATE (the lifecycle bump that left the verifier
       event stranded before the fix).
    6. The user_confirm handler finds the real verifier
       evidence on the new revision (verifier event
       migration) and drives the contract to COMPLETE.
    """
    from lhgp.persistence.events import EventType
    from lhgp.rpc.server import PROTOCOL_VERSION, parse_envelope
    from longtask.cli.daemon import run_daemon_tick
    from longtask.cli.runner import AttemptRunner
    from longtask.cli.tick import _judge_verifier_outcomes
    from longtask.mcp_server import tool_prepare_contract, tool_submit_plan
    from longtask.persistence.events_query import get_events
    from longtask.persistence.store import (
        auto_approve_drafted_contract,
        get_contract,
        patch_goal,
    )
    from longtask.rpc.handlers.contract import handle_contract_user_confirm

    root = tmp_path / "data"
    root.mkdir()
    workspace = root / "ws"
    workspace.mkdir()

    registry = _registry(workspace)
    registry.save_to_file(root / "registry.json")

    db_path = root / "state.db"
    conn = connect(StoreConfig(db_path=db_path))
    ensure_schema(conn)
    try:
        goal_id = "lt-plan-gate-goal"
        _seed_goal_and_authority(conn, goal_id, workspace)
        # Bounded pre_authorized: write file only.
        patch_goal(
            conn,
            goal_id=goal_id,
            now=NOW,
            expected_revision=1,
            actor="user",
            plan={
                "pre_authorized": {
                    "enabled": True,
                    "actions": ["write file"],
                }
            },
        )
        # ── 1) Real MCP prepare.  The parse-time strip
        # removes the model's auto_approve claim; the
        # contract is bound to the Goal.
        prepared = tool_prepare_contract(
            {
                "title": "plan-gate contract",
                "objective": "write a file",
                "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                "acceptance_standard": "user confirm",
                "acceptance_checks": ["c1"],
                "goal_id": goal_id,
                "hard_constraints": {
                    "file_effects": {
                        "mode": "workspace-write",
                        "workspace_root": str(workspace),
                    }
                },
                "context": {"gate": "plan"},
                "spec": {"all": [{"judge": "user"}]},
                "spec_hash": "hash-plan",
                "authority": {
                    "executor_policy": "explicit_allow",
                    "executors": [
                        {
                            "executor_id": "exec-plan",
                            "models": ["*"],
                            "roles": ["executor"],
                        },
                        {
                            "executor_id": "ver-plan",
                            "models": ["*"],
                            "roles": ["verifier"],
                        },
                    ],
                },
            },
            {"conn": conn, "registry": registry, "root": tmp_path},
        )
        assert prepared.get("ok") is True
        cid = prepared["result"]["contract_id"]

        view = get_contract(conn, cid)
        assert view is not None
        # Parse-time strip: no claim, even if the model
        # tried to set one.
        assert view.draft.auto_approve.enabled is False
        assert view.draft.context.get("gate") == "plan"
        # ── 2) Real MCP submit_plan.  The plan claims
        # ``write file`` which is in the Goal's grant;
        # the submit-side subset check approves without
        # signoff.
        plan_result = tool_submit_plan(
            {
                "contract_id": cid,
                "steps": [
                    {
                        "step_id": 1,
                        "action": "write file",
                        "target": "c1",
                        "rationale": "write a file for the verifier",
                        "expected_outcome": "c1 satisfied",
                    }
                ],
            },
            {"conn": conn, "registry": registry, "root": tmp_path},
        )
        assert plan_result["approved"] is True, (
            f"MCP submit_plan must auto-approve when the plan's action is "
            f"in the Goal's bounded pre_authorized grant; got {plan_result!r}"
        )
        assert plan_result["requires_signoff"] is False
        # ── 3) Real auto-activate via the new bounded
        # grant override (no wildcard, recent
        # PLAN_APPROVED).
        view = get_contract(conn, cid)
        assert view is not None
        assert view.state.value == "drafted"
        promoted = auto_approve_drafted_contract(conn, view, NOW + timedelta(seconds=1))
        assert promoted is True, (
            "bounded pre_authorized Goal + recent PLAN_APPROVED "
            "must auto-activate the no-claim MCP contract"
        )
        post_view = get_contract(conn, cid)
        assert post_view is not None
        assert post_view.state.value == "active"
        # ── 4) Real daemon tick + real subprocess
        # executor + real subprocess verifier.
        runner = AttemptRunner(root, conn, registry)
        tick = run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=2))
        assert tick["attempts_started"], (
            f"plan gate must accept the contract post-migration; got {tick!r}"
        )
        started = tick["attempts_started"][0]
        assert runner.start_attempt(
            NOW + timedelta(seconds=3),
            contract_id=started["contract_id"],
            attempt_id=started["attempt_id"],
            executor_id=started["executor_id"],
        )
        # Drain the runner for the executor + verifier
        # chain.  Two attempts run sequentially in this
        # test; allow up to 30s for both real subprocess
        # lifecycles.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and runner._running:
            time.sleep(0.05)
            runner.poll_attempts(NOW + timedelta(seconds=4))
        assert not runner._running, (
            f"real subprocess executor+verifier did not reach terminal; "
            f"running={list(runner._running.keys())}"
        )
        # ── 5) Judge tick: verifier success → CANDIDATE
        # transition (lifecycle bump that left the
        # verifier event stranded pre-fix).
        _judge_verifier_outcomes(root, conn, NOW + timedelta(seconds=5))
        post = get_contract(conn, cid)
        assert post is not None
        # Sanity: the spec round-trips through the JSON
        # blob so the judge actually sees a structured
        # spec.  Without this, the CANDIDATE transition
        # never fires (verifier success alone drives the
        # legacy "verifier success → COMPLETE" path).
        assert post.draft.acceptance.spec is not None, (
            f"acceptance.spec must round-trip through persistence; got {post.draft.acceptance!r}"
        )
        assert post.acceptance_status.value == "candidate", (
            f"verifier success + spec with user criterion must transition to "
            f"CANDIDATE; got {post.acceptance_status.value!r}"
        )
        # The verifier event MUST be on the post-CANDIDATE
        # revision (the migration re-stamped it).  This
        # is the reviewer-found regression #3.
        verifier_events = [
            e
            for e in get_events(conn, contract_id=cid)
            if e.event_type == EventType.ATTEMPT_SUCCEEDED and e.role == "verifier"
        ]
        assert verifier_events, "verifier success must be recorded"
        # 7th-round fix: verifier evidence is content-bound
        # (the event carries the spec_hash it ran against),
        # not lifecycle-bound.  The event does NOT migrate
        # to the post-CANDIDATE revision; the user_confirm
        # path matches the event's spec_hash against the
        # contract's current spec_hash instead.
        verifier_payload = json.loads(verifier_events[0].payload_json or "{}")
        assert verifier_payload.get("spec_hash") == "hash-plan", (
            f"verifier event must carry the spec_hash it ran against; "
            f"got spec_hash={verifier_payload.get('spec_hash')!r}"
        )
        # ── 6) user_confirm with a Principal envelope.
        # The handler must find the real verifier
        # evidence (not the synthesised stub) and drive
        # the contract to COMPLETE.
        envelope = parse_envelope(
            {
                "method": "contract/user-confirm",
                "request_id": "e2e-plan-gate-user-confirm-1",
                "client_id": "cli",
                "protocol_version": PROTOCOL_VERSION,
                "params": {
                    "contract_id": cid,
                    "note": "E2E user-confirm after plan-gate auto-activate",
                },
            }
        )
        handle_contract_user_confirm(
            envelope,
            conn=conn,
            now=NOW + timedelta(seconds=6),
        )
        # The handler must record the verifier attempt
        # in the CONTRACT_COMPLETED event (not a
        # "user-confirm:" stub).
        completed_events = [
            e
            for e in get_events(conn, contract_id=cid)
            if e.event_type == EventType.CONTRACT_COMPLETED
        ]
        assert completed_events, "CONTRACT_COMPLETED must be written"
        completed_payload = json.loads(completed_events[0].payload_json or "{}")
        verifier_ref = completed_payload.get("verifier")
        assert verifier_ref is not None
        assert not str(verifier_ref).startswith("user-confirm:"), (
            f"user_confirm must carry the real verifier attempt id (not the "
            f"synthesised stub); got verifier={verifier_ref!r}"
        )
        # Final state: COMPLETE + acceptance=PASSED.
        final_view = get_contract(conn, cid)
        assert final_view is not None
        assert final_view.state == ContractState.COMPLETE, (
            f"MCP plan-gate submit-and-leave must drive the contract to "
            f"COMPLETE; got state={final_view.state!r}"
        )
        assert final_view.acceptance_status.value == "passed"
        # The real artifact landed on disk.
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "good"
    finally:
        with contextlib.suppress(Exception):
            conn.close()
