"""9th-round review (2026-09-10): the acceptance fingerprint must be fixed at
attempt admission, not read from the live contract row at collection time.

The reviewer's repro, in words:

  1. the contract is already waiting for a user confirmation, and another
     verification is requested;
  2. the verifier starts and receives the *old* requirement (check done.txt);
  3. before it finishes, the acceptance is patched through the normal revision
     interface to require new.txt;
  4. the verifier checks the old file and passes, but the collection code
     stamps the *new* acceptance's fingerprint onto its result;
  5. user_confirm matches that fingerprint against the current acceptance and
     the contract reaches COMPLETE / passed -- while new.txt does not exist.

The recorded events gave it away: launch and result both belong to revision 4,
yet the result carried the content identity of revision 5.

This file drives the real collection path (``AttemptRunner._finish_attempt``
with a stubbed adapter, so no subprocess startup is involved) against a real
store, a real revision trail and a real ``user_confirm``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp.contracts.acceptance import Acceptance, evidence_binding
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import AcceptanceStatus
from lhgp.persistence.store import (
    StoreConfig,
    attempt_evidence_binding,
    connect,
    ensure_schema,
    get_contract,
    patch_contract,
    save_contract,
    update_contract_state,
)
from lhgp.rpc.errors import RpcError
from lhgp.rpc.server import PROTOCOL_VERSION, parse_envelope
from longtask.cli.runner import AttemptRunner
from longtask.contracts.schema import ContractState
from longtask.persistence.events_query import get_events
from longtask.promoter.records import _record_attempt
from longtask.rpc.handlers.contract import handle_contract_user_confirm

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 10, 18, 0, 0, tzinfo=UTC)

OLD_REQUIREMENT = "done.txt says good"
NEW_REQUIREMENT = "new.txt must exist"


class _StubAdapter:
    """The minimum an attempt-collect path needs from an adapter."""

    def collect(self, attempt_id: str) -> dict[str, Any]:
        return {"returncode": 0, "stdout": "", "stderr": "", "exit_code_known": True}


def _acceptance(standard: str) -> Acceptance:
    from lhgp.acceptance.checks import CheckKind, CheckSpec

    return Acceptance(
        standard=standard,
        checks=(CheckSpec(kind=CheckKind.FILE_EXISTS, target="done.txt"),),
        verifier="cross_check",
    )


def _stage(tmp_path: Path, conn: sqlite3.Connection, cid: str) -> Acceptance:
    """Create the contract, activate it, and put a passing artefact in place.

    The typed check targets ``done.txt`` -- the file the verifier legitimately
    produced.  ``new.txt``, the one the edited requirement demands, never
    exists anywhere in this test.
    """
    workspace = tmp_path / "data" / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "done.txt").write_text("good", encoding="utf-8")
    original = _acceptance(OLD_REQUIREMENT)
    save_contract(
        conn,
        ContractDraft(
            title="fix the fingerprint timing",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {
                    "mode": "workspace-write",
                    "workspace_root": str(workspace),
                }
            },
            acceptance=original,
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    update_contract_state(
        conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW + timedelta(seconds=1)
    )
    return original


def _verifier_event(conn: sqlite3.Connection, cid: str) -> dict[str, Any]:
    all_events = get_events(conn, contract_id=cid)
    events = [
        e for e in all_events if e.role == "verifier" and str(e.event_type) == "attempt/succeeded"
    ]
    assert events, (
        f"no verifier success event; got {sorted({str(e.event_type) for e in all_events})}"
    )
    return json.loads(events[-1].payload_json or "{}")


def test_collected_evidence_carries_the_admitted_revision(tmp_path: Path) -> None:
    """An acceptance patched while the verifier runs must not move its stamp."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    cid = "lt-midflight-collect"
    original = _stage(tmp_path, conn, cid)

    admitted = get_contract(conn, cid)
    assert admitted is not None
    attempt_id = "att-verifier-midflight"
    _record_attempt(
        conn,
        goal_id=admitted.goal_id,
        contract_id=cid,
        attempt_id=attempt_id,
        contract_revision=admitted.revision,
        role="verifier",
        executor_id="exec-1",
        state="running",
        admitted_at=NOW,
        started_at=NOW,
        updated_at=NOW,
    )
    runner = AttemptRunner(data_dir, conn, registry=None)  # type: ignore[arg-type]
    runner._running[attempt_id] = {
        "contract_id": cid,
        "executor_id": "exec-1",
        "model": "*",
        "role": "verifier",
        "contract_revision": admitted.revision,
        "session_ref": "sess-midflight",
    }

    # The user edits the requirement while this verifier is in flight.
    patch_contract(
        conn,
        contract_id=cid,
        expected_revision=admitted.revision,
        now=NOW + timedelta(seconds=2),
        acceptance=_acceptance(NEW_REQUIREMENT),
        actor="user",
    )
    edited = get_contract(conn, cid)
    assert edited is not None and edited.revision > admitted.revision

    runner._finish_attempt(
        NOW + timedelta(seconds=3),
        attempt_id,
        runner._running[attempt_id],
        _StubAdapter(),
        "succeeded",
    )

    payload = _verifier_event(conn, cid)
    assert payload["acceptance_fingerprint"] == original.content_fingerprint, (
        "the collected event describes work done against the acceptance the "
        "attempt was admitted at, so that is the identity it must carry"
    )
    assert payload["acceptance_fingerprint"] != edited.draft.acceptance.content_fingerprint


def test_midflight_edit_survives_to_a_refused_confirmation(tmp_path: Path) -> None:
    """The full chain: honest collect, then confirm must be refused.

    This is the reviewer's five-step repro reduced to what the product can
    observe -- and it must not end in COMPLETE.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    cid = "lt-midflight-confirm"
    _stage(tmp_path, conn, cid)
    admitted = get_contract(conn, cid)
    assert admitted is not None
    attempt_id = "att-verifier-confirm"
    _record_attempt(
        conn,
        goal_id=admitted.goal_id,
        contract_id=cid,
        attempt_id=attempt_id,
        contract_revision=admitted.revision,
        role="verifier",
        executor_id="exec-1",
        state="running",
        admitted_at=NOW,
        started_at=NOW,
        updated_at=NOW,
    )
    runner = AttemptRunner(data_dir, conn, registry=None)  # type: ignore[arg-type]
    runner._running[attempt_id] = {
        "contract_id": cid,
        "executor_id": "exec-1",
        "model": "*",
        "role": "verifier",
        "contract_revision": admitted.revision,
        "session_ref": "sess-confirm",
    }
    patch_contract(
        conn,
        contract_id=cid,
        expected_revision=admitted.revision,
        now=NOW + timedelta(seconds=2),
        acceptance=_acceptance(NEW_REQUIREMENT),
        actor="user",
    )
    runner._finish_attempt(
        NOW + timedelta(seconds=3),
        attempt_id,
        runner._running[attempt_id],
        _StubAdapter(),
        "succeeded",
    )
    # The verifier pass leaves the contract waiting on the user.
    waiting = get_contract(conn, cid)
    assert waiting is not None
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW + timedelta(seconds=4),
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )

    envelope = parse_envelope(
        {
            "method": "contract/user-confirm",
            "request_id": "confirm-midflight",
            "client_id": "cli",
            "protocol_version": PROTOCOL_VERSION,
            "params": {"contract_id": cid, "note": "confirm"},
        }
    )
    with pytest.raises(RpcError) as exc_info:
        handle_contract_user_confirm(envelope, conn=conn, now=NOW + timedelta(seconds=5))
    assert "acceptance was edited after the last verifier pass" in str(exc_info.value)

    after = get_contract(conn, cid)
    assert after is not None
    assert after.state != ContractState.COMPLETE
    assert not [
        e for e in get_events(conn, contract_id=cid) if str(e.event_type) == "contract/completed"
    ], "a mid-flight edit must never reach COMPLETE on evidence from before it"


def test_an_honest_run_still_completes(tmp_path: Path) -> None:
    """No edit, no obstacle: the collect stamp must match the current row.

    Pinned in the same file because the timing fix is one line of source and
    could just as easily have made every confirmation fail.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)
    cid = "lt-midflight-honest"
    original = _stage(tmp_path, conn, cid)
    admitted = get_contract(conn, cid)
    assert admitted is not None
    attempt_id = "att-verifier-honest"
    _record_attempt(
        conn,
        goal_id=admitted.goal_id,
        contract_id=cid,
        attempt_id=attempt_id,
        contract_revision=admitted.revision,
        role="verifier",
        executor_id="exec-1",
        state="running",
        admitted_at=NOW,
        started_at=NOW,
        updated_at=NOW,
    )
    runner = AttemptRunner(data_dir, conn, registry=None)  # type: ignore[arg-type]
    runner._running[attempt_id] = {
        "contract_id": cid,
        "executor_id": "exec-1",
        "model": "*",
        "role": "verifier",
        "contract_revision": admitted.revision,
        "session_ref": "sess-honest",
    }
    runner._finish_attempt(
        NOW + timedelta(seconds=3),
        attempt_id,
        runner._running[attempt_id],
        _StubAdapter(),
        "succeeded",
    )
    payload = _verifier_event(conn, cid)
    current = get_contract(conn, cid)
    assert current is not None
    assert payload["acceptance_fingerprint"] == current.draft.acceptance.content_fingerprint
    assert payload["acceptance_fingerprint"] == original.content_fingerprint
    # A lifecycle-only revision bump after the fact must not invalidate it.
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW + timedelta(seconds=4),
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    rebound = attempt_evidence_binding(conn, cid, admitted.revision)
    assert rebound == evidence_binding(current.draft.acceptance), (
        "same content, different revision label: the identity must be stable, "
        "otherwise every lifecycle bump would force a re-verification"
    )
