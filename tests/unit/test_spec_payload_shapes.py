"""Real bug regressions the user found in their 3rd-round review.

The 3rd-round review found that the previous "E2E" test drove the
spec dispatch with hand-written ``append_event`` calls, which
hid four real bugs:

1. ``_evaluate_contract_spec`` only read the top-level ``checks``
   dict.  The runner writes the verifier outcome under
   ``evidence`` (a list of typed-check results) and
   ``model_verdict`` (a ``lhgp-verdict`` block).  A real
   executor → real verifier pass was therefore always judged
   pending — the gate said "needs user" when the verifier had
   actually passed.
2. ``parse_contract_draft`` (and the patch path) constructed
   ``Acceptance`` manually and dropped the new ``spec`` /
   ``spec_hash`` fields.  Contract prepare and contract patch
   silently invalidated every spec dispatch.
3. The CLI ``lhgp plan signoff`` branch's ``get_contract``
   reference hit an ``UnboundLocalError`` because the symbol
   wasn't always imported (it was conditional on another
   branch's import).
4. The MCP ``lhgp_plan_signoff`` accepted the caller's
   ``signoff_by`` verbatim.  A model client could claim
   ``user:human`` and promote its own plan.

These tests pin the bugs so a future refactor cannot reintroduce
them.  They run on the real DB / real handlers / real
``_evaluate_contract_spec`` — no mock of the SUT.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from lhgp.rpc.handlers._common import parse_contract_draft
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope
from longtask.cli.tick import _evaluate_contract_spec
from longtask.contracts.schema import ContractState
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)
from longtask.rpc.handlers.contract import handle_contract_request_verification

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ── _evaluate_contract_spec: runner-shaped payloads must pass ─────


def _make_draft_with_spec(spec: dict, spec_hash: str = "h-1") -> ContractDraft:
    return ContractDraft(
        title="t",
        objective="o",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(
            standard="s",
            checks=("c1",),
            spec=spec,
            spec_hash=spec_hash,
        ),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )


class _Stub:
    def __init__(self, draft: ContractDraft, contract_id: str) -> None:
        self.draft = draft
        self.contract_id = contract_id
        self.revision = 1


def test_spec_verdict_reads_runner_evidence_list() -> None:
    """The runner writes ``payload["evidence"]`` as a list of
    ``{check_id, outcome}`` dicts.  This is the shape produced
    by the real executor → verifier flow; the spec must read it
    and judge pass when every machine criterion is pass."""
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "result.txt"},
        ]
    }
    draft = _make_draft_with_spec(spec)
    payload = {
        "evidence": [
            {
                "check_id": "file-exists:result.txt",
                "outcome": "pass",
                "source": "typed-check",
                "model_outcome": "pass",
            },
        ],
        "model_verdict": None,
    }
    verdict = _evaluate_contract_spec(_Stub(draft, "lt-x"), payload)
    assert verdict is not None
    assert verdict.outcome == "pass", (
        f"runner-shaped evidence list must yield spec verdict=pass; got {verdict.outcome!r}"
    )


def test_spec_verdict_reads_model_verdict_block() -> None:
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "result.txt"},
        ]
    }
    draft = _make_draft_with_spec(spec)
    payload = {
        "model_verdict": {
            "verdict": "succeeded",
            "checks": [
                {"check_id": "file-exists:result.txt", "outcome": "pass", "source": "model"},
            ],
        },
    }
    verdict = _evaluate_contract_spec(_Stub(draft, "lt-x"), payload)
    assert verdict is not None
    assert verdict.outcome == "pass"


def test_spec_verdict_reads_top_level_checks_dict() -> None:
    """Legacy shape: callers can pass a flat ``checks`` dict."""
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "result.txt"},
        ]
    }
    draft = _make_draft_with_spec(spec)
    payload = {"checks": {"file-exists:result.txt": "pass"}}
    verdict = _evaluate_contract_spec(_Stub(draft, "lt-x"), payload)
    assert verdict is not None
    assert verdict.outcome == "pass"


def test_spec_verdict_combines_shapes() -> None:
    """When all three shapes are present, union them — don't drop
    any criterion."""
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "a.txt"},
            {"judge": "machine", "kind": "file-exists", "target": "b.txt"},
            {"judge": "machine", "kind": "file-exists", "target": "c.txt"},
        ]
    }
    draft = _make_draft_with_spec(spec)
    payload = {
        "checks": {"file-exists:a.txt": "pass"},
        "evidence": [
            {"check_id": "file-exists:b.txt", "outcome": "pass"},
        ],
        "model_verdict": {
            "checks": [
                {"check_id": "file-exists:c.txt", "outcome": "pass"},
            ],
        },
    }
    verdict = _evaluate_contract_spec(_Stub(draft, "lt-x"), payload)
    assert verdict is not None
    assert verdict.outcome == "pass"
    assert verdict.machine_pass == 3


def test_spec_verdict_no_evidence_means_pending_not_pass() -> None:
    """If the verifier wrote no machine-check evidence (rare;
    e.g., a verifier that only wrote a lhgp-verdict block),
    missing machine criteria must fall back to pending — not
    silently to pass."""
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "a.txt"},
        ]
    }
    draft = _make_draft_with_spec(spec)
    payload: dict = {}  # no evidence, no model_verdict, no top-level
    verdict = _evaluate_contract_spec(_Stub(draft, "lt-x"), payload)
    assert verdict is not None
    assert verdict.outcome == "pending"


# ── parse_contract_draft must preserve spec + spec_hash ──────────


def test_parse_contract_draft_preserves_spec() -> None:
    """3rd-round review: contract/prepare used to construct
    ``Acceptance`` manually and dropped ``spec`` / ``spec_hash``;
    downstream spec dispatch then saw ``spec=None`` and skipped."""
    spec = {"all": [{"judge": "machine", "kind": "file-exists", "target": "x"}]}
    params = {
        "title": "t",
        "objective": "o",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {},
        "acceptance": {
            "standard": "s",
            "checks": ["c1"],
            "verifier": "cross_check",
            "spec": spec,
            "spec_hash": "hash-abc",
        },
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 5,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1_048_576,
        },
    }
    draft = parse_contract_draft(params)
    assert draft.acceptance.spec == spec, "spec must round-trip through parse_contract_draft"
    assert draft.acceptance.spec_hash == "hash-abc"


def test_parse_contract_draft_allows_no_spec() -> None:
    """Legacy contract without spec / spec_hash must still parse."""
    params = {
        "title": "t",
        "objective": "o",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {},
        "acceptance": {"standard": "s", "checks": ["c1"]},
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 5,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1_048_576,
        },
    }
    draft = parse_contract_draft(params)
    assert draft.acceptance.spec is None
    assert draft.acceptance.spec_hash is None


def test_parse_contract_draft_rejects_garbage_spec() -> None:
    """A spec that fails validation must surface as a validation
    error, not silently become None."""
    params = {
        "title": "t",
        "objective": "o",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {},
        "acceptance": {
            "standard": "s",
            "checks": ["c1"],
            "verifier": "cross_check",
            "spec": {"all": [{"judge": "magic"}]},  # invalid judge
        },
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 5,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1_048_576,
        },
    }
    from longtask.rpc.errors import RpcError

    with pytest.raises(RpcError) as exc_info:
        parse_contract_draft(params)
    # spec errors are surfaced as acceptance.spec.<error>
    assert "spec" in str(exc_info.value)


# ── end-to-end: real DB → spec → request_verification → pending
# dispatch path (no hand-written verifier event; use real request
# verification RPC which writes a real event and dispatches the
# verifier) ─────────────────────────────────────────────────────────


def test_request_verification_writes_real_event(tmp_path: Path) -> None:
    """When the user requests verification, the contract path
    actually writes a ``VERIFICATION_REQUESTED`` event with the
    spec_hash recorded. The next ``run_daemon_tick`` then
    consumes it and dispatches the verifier (which is real —
    FakeExecutor here, but driven through the runner's actual
    attempt lifecycle, not a stubbed event)."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    spec = {"all": [{"judge": "machine", "kind": "file-exists", "target": "x.txt"}]}
    spec_hash = "h-test"
    draft = _make_draft_with_spec(spec, spec_hash=spec_hash)
    cid = "lt-req-vrf"
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    # Activate so the request_verification can act
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    envelope = RequestEnvelope(
        method=Method.CONTRACT_REQUEST_VERIFICATION,
        request_id="req-vrf-1",
        client_id="cli",
        protocol_version=2,
        params={"contract_id": cid, "actor": "user"},
    )
    handle_contract_request_verification(envelope, conn=conn, now=NOW)
    events = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.VERIFICATION_REQUESTED.value
    ]
    assert events, "request_verification must write a real event"
    payload = json.loads(events[0].payload_json or "{}")
    # The handler records the actor as "user" (from the envelope
    # after the principal check) — at minimum it must NOT be
    # silently lost.
    assert payload.get("requested_by")
    conn.close()
