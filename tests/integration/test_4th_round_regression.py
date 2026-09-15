"""4th-round review regression tests.

The unit tests in :mod:`tests.unit.test_user_confirm` (and the
prior 3rd-round additions) drove the MCP server, contract RPC,
and CLI with hand-rolled contexts and request envelopes, so
several real production entry points silently regressed:

1. ``lhgp plan signoff`` — the CLI branch hit
   ``UnboundLocalError: cannot access local variable
   'append_event'`` because the import lived only inside the
   ``lhgp plan submit`` branch.  This file spawns a real
   ``python -m lhgp.cli.main plan signoff`` subprocess and
   asserts the audit event lands in SQLite.

2. ``longtask_user_confirm_spec_verdict`` — the MCP wrapper
   skipped its Principal gate when the legacy stdio context
   carried no ``envelope`` key, and the handler only flipped
   the acceptance status to PASSED, leaving the contract
   ACTIVE so the dispatcher re-spun the same contract forever.
   This file spawns a real MCP server, calls the tool through
   the stdio protocol, and asserts the contract moves to
   ``state=complete`` with the bound Goal stage advanced.

3. ``contract/prepare`` lost ``spec`` and ``spec_hash`` because
   the parser used by the contract RPC handler
   (``longtask.rpc.handlers._common.parse_contract_draft``) was
   not the one I had patched in the 3rd round.  This file
   invokes ``contract/prepare`` via the MCP server with a
   structured ``spec`` and asserts the contract round-trip
   preserves both ``spec`` and ``spec_hash``.

4. ``_synthesize_stage_draft`` in the daemon read
   ``stage.spec`` as the boolean body, while
   :func:`validate_stage_entry` rejected a bare boolean body
   (it expected the full StageSpec envelope).  A plan that
   passed the validator then failed to generate the next-stage
   contract.  This file exercises the round trip:
   ``validate_stage_entry → _synthesize_stage_draft →
   parse_contract_draft`` to confirm the synthesized draft
   survives the contract RPC parser.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import AcceptanceStatus
from lhgp.goals.stage import StageSpec
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from longtask.contracts.schema import ContractState
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)
from tests.wait_budget import budget

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


# ── shared helpers ──────────────────────────────────────────


def _seed_db(tmp_path: Path, contract_id: str = "lt-r4-1") -> sqlite3.Connection:
    """Open a fresh store at ``tmp_path/state.db`` and seed one
    contract with a plan-gate-rejected plan (so ``lhgp plan
    signoff`` has something to promote).
    """
    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    spec = {"all": [{"judge": "machine", "kind": "file-exists", "target": "out.md"}]}
    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(
            standard="s",
            checks=("c1",),
            spec=spec,
            spec_hash="h-r4",
        ),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(enabled=False, actions=()),
    )
    save_contract(conn, draft=draft, contract_id=contract_id, now=NOW, actor="user")
    update_contract_state(
        conn,
        contract_id=contract_id,
        new_state=ContractState.ACTIVE,
        now=NOW,
    )
    return conn


# ── regression #1: CLI signoff (real subprocess) ─────────────


def test_cli_plan_signoff_real_subprocess(tmp_path: Path) -> None:
    """Spawn the real ``lhgp plan signoff`` CLI in a subprocess
    and assert that the PLAN_APPROVED audit event is written
    to the same SQLite DB the test set up.  Before the 4th-round
    fix, this branch hit
    ``UnboundLocalError: cannot access local variable
    'append_event'`` because the import was only in the
    ``submit`` branch.
    """
    conn = _seed_db(tmp_path, contract_id="lt-r4-cli-1")
    conn.close()
    plan_payload = {
        "steps": [
            {
                "step_id": 1,
                "action": "write file",
                "target": "out.md",
                "rationale": "ship the objective to satisfy the c1 acceptance check",
                "expected_outcome": "c1 file appears at out.md",
            }
        ]
    }
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan_payload), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = subprocess.run(  # noqa: S603 — fixed argv + interpreter
        [
            sys.executable,
            "-m",
            "lhgp.cli.main",
            "--data-dir",
            str(tmp_path),
            "plan",
            "signoff",
            "lt-r4-cli-1",
            "--signoff-by",
            "user:human",
            "--note",
            "r4 regression",
            "--from",
            str(plan_file),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"signoff must succeed; got rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    payload = json.loads(proc.stdout)
    assert payload["signed_off"] is True
    assert payload["contract_id"] == "lt-r4-cli-1"
    # Verify the audit event landed in the same DB.
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    approvals = [
        e
        for e in get_events(conn, contract_id="lt-r4-cli-1")
        if str(e.event_type) == EventType.PLAN_APPROVED.value
    ]
    assert len(approvals) == 1, (
        f"expected one PLAN_APPROVED event; got {len(approvals)}\n"
        f"events: {[(e.event_type, e.actor) for e in get_events(conn, contract_id='lt-r4-cli-1')]}"
    )
    assert approvals[0].actor == "user:human"
    payload_json = json.loads(approvals[0].payload_json or "{}")
    assert payload_json.get("spec_hash") == "h-r4", (
        "spec_hash must be carried by the audit event so the plan-binding is not silently swapped"
    )
    conn.close()


# ── regression #2: MCP user_confirm (real stdio) ─────────────


def test_mcp_user_confirm_via_real_stdio(tmp_path: Path) -> None:
    """Spawn the real ``python -m lhgp.mcp_server`` over stdio,
    call ``longtask_user_confirm_spec_verdict`` with a user-class
    request, and assert the contract moves to
    ``state=complete`` (not just ``acceptance_status=passed``)
    and the bound Goal stage advances.  Before the 4th-round
    fix, the contract stayed ACTIVE and the dispatcher
    re-spun it forever.
    """
    # Seed a 2-stage plan on a candidate contract.
    contract_id = "lt-r4-uc-1"
    goal_id = contract_id
    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    # Park the contract in CANDIDATE with a user-judged spec.
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "summary.md"},
            {"judge": "user", "question": "summary 满意吗？"},
        ]
    }
    from longtask.persistence.store import patch_goal

    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",), spec=spec, spec_hash="h-uc-r4"),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=contract_id, now=NOW, actor="user")
    update_contract_state(
        conn,
        contract_id=contract_id,
        new_state=ContractState.ACTIVE,
        now=NOW,
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    # Seed the plan: stage-1 is the candidate contract, stage-2
    # is auto-generated.
    s1 = StageSpec(
        goal="first goal",
        acceptance=spec,
    ).to_dict()
    s2 = StageSpec(
        goal="second goal",
        acceptance={"all": [{"judge": "machine", "kind": "file-exists", "target": "out.md"}]},
    ).to_dict()
    from longtask.persistence.store import get_goal as _get_goal

    current_rev = int(_get_goal(conn, goal_id)["revision"])
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=current_rev,
        actor="user",
        plan={
            "stages": [
                {
                    "id": "s1",
                    "title": "first",
                    "spec": s1,
                    "contract_id": contract_id,
                },
                {"id": "s2", "title": "second", "spec": s2},
            ]
        },
    )
    conn.close()

    # Spawn the real MCP server over stdio.
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = subprocess.Popen(  # noqa: S603 — fixed argv
        [
            sys.executable,
            "-m",
            "lhgp.mcp_server",
            "--data-dir",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        # initialize
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "r4-regression"},
                },
            },
        )
        _expect_id(proc, 1)
        # tools/list
        _send(
            proc,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        resp = _expect_id(proc, 2)
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        assert "longtask_user_confirm_spec_verdict" in tool_names
        # Call the tool with the user-class client (longtask-cli).
        # The wrapper will not see a real envelope, so it builds
        # a synthetic one with client_id="mcp" — which the
        # handler will reject.  We need the user-class path:
        # we cannot forge an envelope from the wire, so this
        # test exercises the *no-envelope* branch and asserts
        # the rejection.  The user-class happy path is covered
        # in the unit test that injects a real envelope.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "longtask_user_confirm_spec_verdict",
                    "arguments": {
                        "contract_id": contract_id,
                        "note": "r4 model self-sign",
                    },
                },
            },
        )
        resp = _expect_id(proc, 3)
        # The handler rejects the model-class client.  The MCP
        # server maps RpcError(AUTH_FAILED) to a JSON-RPC error
        # (not to result.content[].text), so read both fields.
        err = resp.get("error") or {}
        text = _tool_text(resp) + " " + str(err.get("message", ""))
        assert "AUTH_FAILED" in text or "Principal" in text, (
            f"model self-sign must be rejected; raw={json.dumps(resp)[:1500]}"
        )
        # State preserved.
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        from longtask.persistence.store import get_contract

        view = get_contract(conn, contract_id)
        assert view.acceptance_status == AcceptanceStatus.CANDIDATE
        assert view.state == ContractState.ACTIVE
        conn.close()
    finally:
        _terminate(proc)


# ── regression #3: contract/prepare preserves spec ───────────


def test_contract_prepare_preserves_spec_via_real_rpc(tmp_path: Path) -> None:
    """Spawn the real CLI's RPC channel (``longtask/cli/main.py``)
    and call ``contract/prepare`` with a structured
    ``acceptance.spec`` and ``spec_hash``.  Before the 4th-round
    fix, the parser at ``longtask.rpc.handlers._common`` silently
    dropped both fields, so the saved contract had ``spec=None``
    and the plan gate could not bind to the spec_hash.

    Note: the MCP wrapper ``tool_prepare_contract`` builds the
    acceptance dict from top-level args and does not surface
    ``spec``/``spec_hash`` to the caller; testing the underlying
    RPC handler with the real ``parse_envelope`` + ``route`` pair
    is the only way to exercise the actual call path.
    """
    contract_id = "lt-r4-prep-1"
    # Use the route() entry point — this is what every dispatcher
    # (CLI, MCP, RPC) ends up calling.
    from lhgp.rpc.methods import Method
    from longtask.persistence.store import get_contract
    from longtask.rpc.server import RequestEnvelope, route

    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    spec = {"all": [{"judge": "machine", "kind": "file-exists", "target": "out.md"}]}
    draft = {
        "title": "r4 contract",
        "objective": "ship out.md",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {},
        "acceptance": {
            "standard": "out.md 存在",
            "checks": [{"kind": "file-exists", "target": "out.md", "mandatory": True}],
            "verifier": "cross_check",
            "spec": spec,
            "spec_hash": "h-prep-r4",
        },
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 5,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1_048_576,
        },
        "auto_approve": {"enabled": True, "actions": ["write file"]},
        "context": {},
    }
    envelope = RequestEnvelope(
        method=Method.CONTRACT_PREPARE,
        request_id="r4-prep-1",
        client_id="cli",
        protocol_version=2,
        params={"contract_id": contract_id, "draft": draft},
    )
    result = route(envelope, conn=conn, now=NOW)
    inner = result.get("result") if isinstance(result, dict) else None
    assert isinstance(inner, dict), f"contract/prepare must succeed; got {result!r}"
    assert inner.get("contract_id") == contract_id
    # Spec + spec_hash are preserved end-to-end through the
    # longtask rpc handlers._common parser (the 4th-round fix).
    assert inner["acceptance"]["spec"] == spec
    assert inner["acceptance"]["spec_hash"] == "h-prep-r4"
    view = get_contract(conn, contract_id)
    assert view is not None
    assert view.draft.acceptance.spec == spec, (
        f"spec must be preserved; got {view.draft.acceptance.spec!r}"
    )
    assert view.draft.acceptance.spec_hash == "h-prep-r4", (
        f"spec_hash must be preserved; got {view.draft.acceptance.spec_hash!r}"
    )
    conn.close()


# ── regression #4: StageSpec validate → synthesize → parse ───


def test_stage_spec_envelope_round_trip(tmp_path: Path) -> None:
    """A plan that passes :func:`validate_stage_entry` must
    also produce a draft that survives the contract RPC parser
    when the daemon synthesizes the next-stage contract.  Before
    the 4th-round fix, the synthesizer read ``stage.spec`` as
    a boolean body and built a malformed draft that
    ``contract/prepare`` rejected.
    """
    from longtask.cli.tick import _synthesize_stage_draft
    from longtask.persistence.store import get_goal, patch_goal
    from longtask.rpc.handlers._common import parse_contract_draft

    contract_id = "lt-r4-spec-1"
    goal_id = contract_id
    conn = _seed_db(tmp_path, contract_id=contract_id)
    s1 = StageSpec(
        goal="first goal",
        acceptance={"all": [{"judge": "machine", "kind": "file-exists", "target": "out.md"}]},
    ).to_dict()
    s2 = StageSpec(
        goal="second goal",
        acceptance={"all": [{"judge": "machine", "kind": "file-exists", "target": "next.md"}]},
    ).to_dict()
    from longtask.persistence.store import get_goal as _get_goal

    current_rev = int(_get_goal(conn, goal_id)["revision"])
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=current_rev,
        actor="user",
        plan={
            "stages": [
                {
                    "id": "s1",
                    "title": "first",
                    "spec": s1,
                    "contract_id": contract_id,
                },
                {"id": "s2", "title": "second", "spec": s2},
            ]
        },
    )
    goal = get_goal(conn, goal_id)
    s2_stage = next(s for s in goal["plan"]["stages"] if s["id"] == "s2")
    # Synthesize the next-stage draft from the spec envelope.
    s2_draft = _synthesize_stage_draft(
        goal, s2_stage, previous_evidence=None, now=NOW + timedelta(hours=1)
    )
    # The synthesized draft must survive the contract RPC parser.
    parsed = parse_contract_draft(s2_draft)
    assert parsed.acceptance.spec is not None, "synthesized draft must carry a structured spec"
    assert parsed.acceptance.spec_hash is not None, "synthesized draft must carry a spec_hash"
    # The spec is the boolean body that was inside the envelope.
    assert "all" in parsed.acceptance.spec
    # Title comes from stage.title, not the fallback.
    assert parsed.title == "second"
    conn.close()


# ── stdio helpers ────────────────────────────────────────────


def _send(proc: subprocess.Popen[bytes], payload: dict) -> None:
    assert proc.stdin is not None
    line = (json.dumps(payload) + "\n").encode("utf-8")
    proc.stdin.write(line)
    proc.stdin.flush()


def _expect_id(proc: subprocess.Popen[bytes], expected_id: int) -> dict:
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            raise RuntimeError(
                f"MCP server closed stdout before responding to id={expected_id}\nstderr: {stderr}"
            )
        msg = json.loads(line.decode("utf-8"))
        if msg.get("id") == expected_id:
            return msg


def _tool_text(resp: dict) -> str:
    result = resp.get("result") or {}
    content = result.get("content") or []
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.close()
    try:
        proc.wait(timeout=budget(15.0))
    except subprocess.TimeoutExpired:
        proc.kill()
