"""Plan approval survives lifecycle-only revision bumps.

5th-round P1 regression: a plan approved at revision=1
becomes unusable after the contract is auto-promoted
from DRAFTED to ACTIVE (which bumps the revision to 2).
The plan-gate dispatch check requires
``event.contract_revision == contract.revision`` and
refuses to dispatch with ``plan gate: no recent
PLAN_APPROVED event``.  The reviewer reproduced this on
``context.gate="plan"`` flows: ``submit_plan`` →
``approved=true`` (revision=1) → auto-activate (revision=2)
→ 0 dispatches.

The fix: in ``update_contract_state``, re-stamp any
prior PLAN_APPROVED with the new revision.  Lifecycle
bumps (DRAFTED→ACTIVE, BLOCKED→ACTIVE, retry) don't
change the spec, the accepted_check_ids, or the plan's
spec_hash; the gate's payload-level checks (spec_hash,
accepted_check_ids) still catch a later CONTENT change
that should invalidate the plan.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.persistence.events import EventType
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.real_entry


def test_plan_approval_migrates_to_new_revision_on_lifecycle_bump(tmp_path: Path) -> None:
    """Submit a plan at revision=1 (DRAFTED), then auto-activate
    (DRAFTED→ACTIVE bumps to revision=2).  The PLAN_APPROVED
    event must now carry contract_revision=2 so the plan gate
    still binds to the contract.
    """
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-plan-mig"
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard="s",
                checks=("c1",),
                spec_hash="hash-mig",
            ),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    # Pre-condition: contract is DRAFTED at revision=1.
    pre = get_contract(conn, cid)
    assert pre is not None
    assert pre.state.value == "drafted"
    assert pre.revision == 1

    # 1) Submit a plan at revision=1 — the plan-gate flow
    # writes PLAN_APPROVED with contract_revision=1.
    from lhgp.contracts.plan import _extract_check_identifiers
    from lhgp.persistence.events_query import append_event, get_events

    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.PLAN_APPROVED,
        payload={
            "submitted_by": "user",
            "step_count": 1,
            "accepted_check_ids": list(_extract_check_identifiers(pre)),
            "spec_hash": pre.draft.acceptance.spec_hash,
            "auto_approved": True,
        },
        contract_revision=1,
        now=NOW,
        actor="daemon",
    )
    # Sanity: the PLAN_APPROVED is at revision=1.
    plan_event = next(
        e for e in get_events(conn, contract_id=cid) if e.event_type == EventType.PLAN_APPROVED
    )
    assert plan_event.contract_revision == 1

    # 2) Auto-activate: DRAFTED → ACTIVE bumps the
    # contract revision to 2.  The fix migrates the
    # PLAN_APPROVED's contract_revision to 2.
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=pre.state.__class__.ACTIVE,
        now=NOW + timedelta(seconds=1),
        actor="daemon",
    )
    post = get_contract(conn, cid)
    assert post is not None
    assert post.state.value == "active"
    assert post.revision == 2

    # 3) The PLAN_APPROVED event's contract_revision is
    # now 2 — the gate's revision check passes.
    plan_event_post = next(
        e for e in get_events(conn, contract_id=cid) if e.event_type == EventType.PLAN_APPROVED
    )
    assert plan_event_post.contract_revision == 2, (
        "lifecycle-only revision bump must migrate the plan "
        "approval's contract_revision so the gate still binds; "
        f"got contract_revision={plan_event_post.contract_revision}"
    )

    conn.close()


def test_plan_approval_preserves_payload_after_migration(tmp_path: Path) -> None:
    """The migration must keep the plan's accepted_check_ids
    and spec_hash — those are what the gate uses to detect
    a content change.  A separate content-bearing patch
    (which would change the spec_hash) is what should
    invalidate the plan; a lifecycle bump is not.
    """
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-plan-mig-payload"
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard="s",
                checks=("c1", "c2"),
                spec_hash="hash-original",
            ),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    from lhgp.contracts.plan import _extract_check_identifiers
    from lhgp.persistence.events_query import append_event, get_events

    pre = get_contract(conn, cid)
    assert pre is not None
    original_checks = list(_extract_check_identifiers(pre))
    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.PLAN_APPROVED,
        payload={
            "submitted_by": "user",
            "step_count": 1,
            "accepted_check_ids": original_checks,
            "spec_hash": pre.draft.acceptance.spec_hash,
            "auto_approved": True,
        },
        contract_revision=1,
        now=NOW,
        actor="daemon",
    )
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=pre.state.__class__.ACTIVE,
        now=NOW + timedelta(seconds=1),
        actor="daemon",
    )
    plan_event = next(
        e for e in get_events(conn, contract_id=cid) if e.event_type == EventType.PLAN_APPROVED
    )
    import json

    payload = json.loads(plan_event.payload_json or "{}")
    assert payload.get("accepted_check_ids") == original_checks, (
        "migration must not change the plan's accepted_check_ids; "
        f"got {payload.get('accepted_check_ids')!r}"
    )
    assert payload.get("spec_hash") == "hash-original", (
        f"migration must not change the plan's spec_hash; got {payload.get('spec_hash')!r}"
    )
    assert plan_event.contract_revision == 2
    conn.close()


def test_no_plan_approval_no_spurious_migration(tmp_path: Path) -> None:
    """A contract without a plan approval gets the lifecycle
    bump as before; the migration's WHERE clause
    (``event_type = PLAN_APPROVED``) is a no-op for
    contracts with no plan yet.
    """
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-no-plan"
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    pre = get_contract(conn, cid)
    assert pre is not None
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=pre.state.__class__.ACTIVE,
        now=NOW + timedelta(seconds=1),
        actor="daemon",
    )
    post = get_contract(conn, cid)
    assert post is not None
    assert post.state.value == "active"
    assert post.revision == 2
    # No PLAN_APPROVED event was written; the migration's
    # WHERE clause is a no-op.
    from lhgp.persistence.events_query import get_events

    plan_events = [
        e for e in get_events(conn, contract_id=cid) if e.event_type == EventType.PLAN_APPROVED
    ]
    assert plan_events == []


def test_patch_contract_also_migrates_plan_approval(tmp_path: Path) -> None:
    """``patch_contract`` bumps the revision the same way
    ``update_contract_state`` does, so the plan-approval
    migration must mirror the lifecycle-bump migration.  A
    soft_guidance-only patch (no content change) should
    keep the user's prior plan sign-off binding, otherwise
    the gate's revision check would false-negative and
    refuse to dispatch.
    """
    from lhgp.persistence.events_query import get_events
    from longtask.persistence.store import patch_contract, save_contract

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-patch-migrate"
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard="s",
                checks=("c1",),
                spec_hash="hash-original",
            ),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    pre = get_contract(conn, cid)
    assert pre is not None
    pre_revision = pre.revision
    # Manually drop a PLAN_APPROVED at the seed revision.
    from lhgp.persistence.store import append_event

    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.PLAN_APPROVED,
        payload={
            "accepted_check_ids": ("c1",),
            "spec_hash": "hash-original",
            "submitted_by": "user",
        },
        now=NOW + timedelta(seconds=1),
        actor="user",
        contract_revision=pre_revision,
    )
    # Soft_guidance-only patch — no content change.
    patch_contract(
        conn,
        contract_id=cid,
        expected_revision=pre_revision,
        now=NOW + timedelta(seconds=2),
        soft_guidance={"new": "guidance"},
        actor="user",
    )
    post = get_contract(conn, cid)
    assert post is not None
    assert post.revision == pre_revision + 1
    plan_events = [
        e for e in get_events(conn, contract_id=cid) if e.event_type == EventType.PLAN_APPROVED
    ]
    assert plan_events, "PLAN_APPROVED must persist across patch_contract"
    assert plan_events[0].contract_revision == post.revision, (
        f"patch_contract must re-stamp PLAN_APPROVED to the new revision; "
        f"got contract_revision={plan_events[0].contract_revision} "
        f"vs contract.revision={post.revision}"
    )
    # accepted_check_ids / spec_hash are unchanged so the
    # gate's payload check would still pass.
    import json

    payload = json.loads(plan_events[0].payload_json or "{}")
    assert payload.get("spec_hash") == "hash-original"
    assert payload.get("accepted_check_ids") == ["c1"]
    conn.close()


def test_gate_reads_revision_from_events_column_not_payload_json(tmp_path: Path) -> None:
    """6th-round P1 fix: the plan gate binds the lifecycle
    revision via ``events.contract_revision`` (the column
    re-stamped on every lifecycle bump), NOT via the
    ``payload_json.contract_revision`` field (a snapshot at
    approval time that drifts after auto-activate).

    A contract approved at revision=1 by the MCP submit
    path lands a PLAN_APPROVED whose payload carries
    ``contract_revision=1`` AND whose row-level
    ``contract_revision`` is 1.  After the auto-activate
    bump to revision=2, the column is re-stamped to 2
    while the payload field is unchanged.  The gate must
    read the column and accept the approval; reading the
    payload field would refuse dispatch with
    ``plan gate: no recent PLAN_APPROVED event``.
    """
    from lhgp.persistence.events_query import get_events
    from longtask.cli.dispatch import _has_recent_plan_approval
    from longtask.persistence.store import (
        auto_approve_drafted_contract,
        patch_goal,
        save_contract,
    )

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    goal_id = "lt-gate-col-test"
    cid = "lt-gate-col-cid"
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard="s",
                checks=("c1",),
                spec_hash="hash-1",
            ),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=f"{goal_id}-bootstrap",
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    # Bounded pre_authorized: write file only.
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={"pre_authorized": {"enabled": True, "actions": ["write file"]}},
    )
    save_contract(
        conn,
        ContractDraft(
            title="real contract",
            objective="write a file",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard="s",
                checks=("c1",),
                spec_hash="hash-1",
            ),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="model",
        goal_id=goal_id,
    )
    # Hand-craft a PLAN_APPROVED with the legacy payload
    # shape (contract_revision embedded in JSON) — the
    # column is the source of truth post-fix.
    from lhgp.persistence.store import append_event

    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.PLAN_APPROVED,
        payload={
            "contract_revision": 1,  # legacy payload field
            "accepted_check_ids": ["c1"],
            "spec_hash": "hash-1",
            "auto_approved": True,
        },
        now=NOW + timedelta(seconds=1),
        actor="model",
        contract_revision=1,  # column
    )
    # Auto-activate (DRAFTED→ACTIVE bumps revision to 2;
    # the migration re-stamps the column to 2; the payload
    # field still says 1).
    view = get_contract(conn, cid)
    assert view is not None
    assert view.state.value == "drafted"
    promoted = auto_approve_drafted_contract(conn, view, NOW + timedelta(seconds=2))
    assert promoted is True
    post = get_contract(conn, cid)
    assert post is not None
    assert post.revision == 2
    # The column is now 2; the payload field is still 1.
    plan_event = next(
        e for e in get_events(conn, contract_id=cid) if e.event_type == EventType.PLAN_APPROVED
    )
    assert plan_event.contract_revision == 2
    import json

    payload = json.loads(plan_event.payload_json or "{}")
    assert payload.get("contract_revision") == 1, (
        "this test pins the legacy-shape payload: the column "
        "is the source of truth, not the payload field"
    )
    # The gate reads the column and accepts the plan.
    accepted = _has_recent_plan_approval(
        conn,
        cid,
        contract_revision=post.revision,
        accepted_check_ids=["c1"],
        spec_hash="hash-1",
        now=NOW + timedelta(seconds=3),
    )
    assert accepted is True, (
        "gate must accept the plan because events.contract_revision "
        "matches the current contract revision; the legacy payload "
        "field contract_revision=1 is ignored"
    )
    conn.close()


def test_verifier_event_does_not_migrate_but_binds_via_spec_hash(tmp_path: Path) -> None:
    """7th-round fix (replaces 6th-round
    ``test_verifier_event_also_migrates_on_lifecycle_bump``):
    verifier evidence is content-bound, not lifecycle-bound.
    The lifecycle bump that the CANDIDATE transition issues
    must NOT re-stamp the verifier event — doing so would
    bring stale evidence back into scope after a user edit
    to ``acceptance`` (the reviewer's P1 regression: a
    contract can land in COMPLETE without a fresh check
    when the verifier event is migrated to the post-edit
    revision).  The user_confirm path now matches the
    event's ``spec_hash`` against the contract's current
    ``acceptance.spec_hash`` and rejects mismatches.
    """
    import json as _json

    from lhgp.contracts.contract_view import AcceptanceStatus
    from lhgp.persistence.events_query import get_events
    from longtask.contracts.schema import ContractState
    from longtask.persistence.store import (
        append_event,
        save_contract,
        update_contract_state,
    )

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-verifier-content-bound"
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(
                standard="s",
                checks=("c1",),
                spec_hash="hash-1",
            ),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    # Verifier success at revision 1, carrying the
    # spec_hash it ran against (the content binding).
    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.ATTEMPT_SUCCEEDED,
        payload={
            "verdict": "succeeded",
            "spec_hash": "hash-1",
            "checks": [{"check_id": "c1", "outcome": "pass"}],
        },
        now=NOW + timedelta(seconds=1),
        actor="verifier",
        role="verifier",
        contract_revision=1,
    )
    # CANDIDATE transition (lifecycle bump only — no
    # content change to ``acceptance``).
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW + timedelta(seconds=2),
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    post = get_contract(conn, cid)
    assert post is not None
    assert post.revision == 2
    verifier_events = [
        e
        for e in get_events(conn, contract_id=cid)
        if e.event_type == EventType.ATTEMPT_SUCCEEDED and e.role == "verifier"
    ]
    assert verifier_events, "verifier success must persist"
    # Lifecycle bump must NOT migrate the verifier
    # event — its contract_revision stays at 1.
    assert verifier_events[0].contract_revision == 1, (
        "verifier event must NOT migrate on lifecycle bump; "
        "it is content-bound (spec_hash), not lifecycle-bound"
    )
    # The spec_hash in the payload is the binding the
    # user_confirm path matches against.
    payload = _json.loads(verifier_events[0].payload_json or "{}")
    assert payload.get("spec_hash") == "hash-1"
    conn.close()


def test_acceptance_change_invalidates_stale_verifier_evidence(tmp_path: Path) -> None:
    """7th-round P1: a user edit to ``acceptance``
    (different spec_hash) must invalidate the prior
    verifier evidence.  ``_latest_verifier_evidence``
    must return an empty dict in that case so the
    user_confirm path falls into the synthesised branch
    instead of re-firing the stale verifier pass — the
    reviewer's real-world repro that produced
    COMPLETE-without-a-fresh-check.
    """
    import json as _json

    from lhgp.contracts.contract_view import AcceptanceStatus
    from lhgp.persistence.events_query import get_events
    from lhgp.rpc.server import PROTOCOL_VERSION, parse_envelope
    from longtask.contracts.schema import ContractState
    from longtask.persistence.store import (
        append_event,
        patch_contract,
        save_contract,
        update_contract_state,
    )
    from longtask.rpc.handlers.contract import handle_contract_user_confirm

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-stale-verifier"
    initial_acceptance = Acceptance(
        standard="done.txt says good",
        checks=("c1",),
        spec_hash="hash-done",
    )
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=initial_acceptance,
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    pre = get_contract(conn, cid)
    assert pre is not None
    # The contract must be in CANDIDATE for user_confirm
    # to consider it at all (this is the gate the
    # handler enforces before evidence lookup).  The
    # reviewer's repro: user pauses, edits acceptance,
    # resumes — the CANDIDATE state was set BEFORE the
    # edit, so the state is still CANDIDATE.  The
    # evidence-lookup guard is what must now catch
    # the stale evidence.
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW + timedelta(seconds=1),
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    # Verifier success at revision 2 against the OLD
    # spec_hash ("hash-done").
    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.ATTEMPT_SUCCEEDED,
        payload={
            "verdict": "succeeded",
            "spec_hash": "hash-done",
            "checks": [{"check_id": "c1", "outcome": "pass"}],
        },
        now=NOW + timedelta(seconds=2),
        actor="verifier",
        role="verifier",
        contract_revision=2,
    )
    pre_patch = get_contract(conn, cid)
    assert pre_patch is not None
    # User edits acceptance to require a different
    # artifact (new spec_hash) — this is the reviewer's
    # repro step.
    new_acceptance = Acceptance(
        standard="new.txt must exist",
        checks=("c1",),
        spec_hash="hash-new",
    )
    patch_contract(
        conn,
        contract_id=cid,
        expected_revision=pre_patch.revision,
        now=NOW + timedelta(seconds=3),
        acceptance=new_acceptance,
        actor="user",
    )
    post_patch = get_contract(conn, cid)
    assert post_patch is not None
    assert post_patch.draft.acceptance.spec_hash == "hash-new"
    # patch_contract bumped the revision; the
    # acceptance_status is still CANDIDATE (patch
    # does not touch the status).  user_confirm's
    # pre-check (acceptance_status == CANDIDATE) will
    # pass; the evidence-lookup guard is the second
    # belt that must now catch the stale evidence via
    # spec_hash mismatch.
    # Now run user_confirm with a Principal envelope.
    # The handler must REFUSE — the verifier evidence is
    # stale (spec_hash mismatch) and synthesising a
    # stub would let a contract land in COMPLETE
    # without a fresh check (the reviewer's P1 repro).
    from lhgp.rpc.errors import RpcError as _RpcError

    envelope = parse_envelope(
        {
            "method": "contract/user-confirm",
            "request_id": "e2e-stale-verifier-1",
            "client_id": "cli",
            "protocol_version": PROTOCOL_VERSION,
            "params": {
                "contract_id": cid,
                "note": "user_confirm after acceptance edit",
            },
        }
    )
    with pytest.raises(_RpcError) as exc_info:
        handle_contract_user_confirm(
            envelope,
            conn=conn,
            now=NOW + timedelta(seconds=5),
        )
    assert "no valid verifier evidence" in str(exc_info.value)
    completed = [
        e for e in get_events(conn, contract_id=cid) if str(e.event_type) == "contract/completed"
    ]
    # user_confirm must NOT have driven the contract
    # to COMPLETE on the stale verifier evidence.
    assert not completed, (
        f"user_confirm must NOT write CONTRACT_COMPLETED on stale verifier "
        f"evidence; got events={[str(e.event_type) for e in get_events(conn, contract_id=cid)]}"
    )
    post = get_contract(conn, cid)
    assert post is not None
    assert post.state == ContractState.ACTIVE, (
        f"contract must remain ACTIVE; user_confirm with stale evidence "
        f"and no CANDIDATE pre-state must NOT silently complete it.  "
        f"got state={post.state!r}"
    )
    # The verifier event still exists with its
    # original contract_revision (no migration).
    verifier_events = [
        e
        for e in get_events(conn, contract_id=cid)
        if e.event_type == EventType.ATTEMPT_SUCCEEDED and e.role == "verifier"
    ]
    assert verifier_events
    assert verifier_events[0].contract_revision == 2
    # spec_hash on the event is the OLD one (the
    # verifier ran against the old acceptance).  The
    # user_confirm path matches-and-skipped it; the
    # refusal is what blocked the call here.
    payload = _json.loads(verifier_events[0].payload_json or "{}")
    assert payload.get("spec_hash") == "hash-done"
    conn.close()


def test_auto_activate_with_bounded_pre_authorized_and_plan_approval(tmp_path: Path) -> None:
    """6th-round P1 fix: an MCP-issued contract whose
    ``auto_approve`` claim is stripped at parse time can
    still auto-activate when the bound Goal has a
    bounded ``pre_authorized`` and a recent
    ``PLAN_APPROVED`` event.  The plan-approval's submit-
    side subset check is the user-pinned sign-off.
    """
    from lhgp.persistence.events import EventType
    from longtask.persistence.store import (
        auto_approve_drafted_contract,
        patch_goal,
        save_contract,
    )

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    goal_id = "lt-bounded-auto"
    cid = "lt-bounded-auto-cid"
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=f"{goal_id}-bootstrap",
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    # Bounded pre_authorized: write file only — no
    # wildcard.
    patch_goal(
        conn,
        goal_id=goal_id,
        now=NOW,
        expected_revision=1,
        actor="user",
        plan={"pre_authorized": {"enabled": True, "actions": ["write file"]}},
    )
    # MCP-style contract: no auto_approve claim (parse-time
    # strip), only a bound Goal.
    save_contract(
        conn,
        ContractDraft(
            title="real contract",
            objective="write a file",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="model",
        goal_id=goal_id,
    )
    view = get_contract(conn, cid)
    assert view is not None
    assert view.draft.auto_approve.enabled is False
    # Without a recent PLAN_APPROVED, the no-claim branch
    # still refuses (no wildcard, no plan).
    promoted = auto_approve_drafted_contract(conn, view, NOW + timedelta(seconds=1))
    assert promoted is False, (
        "bounded pre_authorized Goal without a recent PLAN_APPROVED "
        "must NOT auto-activate a no-claim contract"
    )
    # The MCP submit path would normally write a
    # PLAN_APPROVED at this revision; simulate that.
    from lhgp.persistence.store import append_event

    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.PLAN_APPROVED,
        payload={"contract_revision": 1, "auto_approved": True},
        now=NOW + timedelta(seconds=2),
        actor="model",
        contract_revision=1,
    )
    # Re-fetch the contract (revision unchanged) and retry
    # the auto-approve.
    view2 = get_contract(conn, cid)
    assert view2 is not None
    promoted = auto_approve_drafted_contract(conn, view2, NOW + timedelta(seconds=3))
    assert promoted is True, (
        "bounded pre_authorized Goal with a recent PLAN_APPROVED "
        "must auto-activate a no-claim contract; this is the new "
        "plan-approval override branch"
    )
    post = get_contract(conn, cid)
    assert post is not None
    assert post.state.value == "active"
    conn.close()


# An unambiguously stale-on-the-wall-clock seed.  Every event
# below is written with this timestamp and every check passes
# the same timestamp to ``auto_approve_drafted_contract``, so
# the only clock that can matter is the supplied one.  An
# implementation that reads ``datetime.now()`` inside the
# freshness helper answers False here no matter when the suite
# runs, which is the point.
SEEDED = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)


def _seed_bounded_goal_and_draft(
    conn: sqlite3.Connection, *, goal_id: str, contract_id: str
) -> None:
    """Create a bounded (no wildcard) pre_authorized Goal plus an
    MCP-style DRAFTED contract with no ``auto_approve`` claim.
    """
    from longtask.persistence.store import patch_goal, save_contract

    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=SEEDED + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=f"{goal_id}-bootstrap",
        now=SEEDED,
        actor="user",
        goal_id=goal_id,
    )
    patch_goal(
        conn,
        goal_id=goal_id,
        now=SEEDED,
        expected_revision=1,
        actor="user",
        plan={"pre_authorized": {"enabled": True, "actions": ["write file"]}},
    )
    save_contract(
        conn,
        ContractDraft(
            title="real contract",
            objective="write a file",
            deadline_at=SEEDED + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=contract_id,
        now=SEEDED,
        actor="model",
        goal_id=goal_id,
    )


def _approve_plan_at(conn: sqlite3.Connection, contract_id: str, at: datetime) -> None:
    from lhgp.persistence.store import append_event

    append_event(
        conn,
        contract_id=contract_id,
        event_type=EventType.PLAN_APPROVED,
        payload={"contract_revision": 1, "auto_approved": True},
        now=at,
        actor="model",
        contract_revision=1,
    )


def test_plan_override_freshness_uses_supplied_clock_not_wall_clock(
    tmp_path: Path,
) -> None:
    """7th-round follow-up (P0): the plan-approval freshness bound
    must be measured against the caller-supplied clock.

    ``_has_recent_plan_approval_for_goal`` read
    ``datetime.now(UTC)`` while every other decision in the
    persistence layer is clock-injected, and its caller
    ``auto_approve_drafted_contract`` already receives ``now``
    and threw it away.  Consequences:

    - Production: the DRAFTED→ACTIVE authority silently
      disagreed with the dispatcher's 24 h plan gate, and the
      verdict for one event log changed depending on when the
      process happened to run.
    - Tests: the suite seeds a fixed ``NOW``, so widening the
      window (1800 → 7200 s) only postponed the failure.

    Here the whole scenario lives in 2020; only an injected
    clock can call the approval "recent".
    """
    from longtask.persistence.store import auto_approve_drafted_contract

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    _seed_bounded_goal_and_draft(conn, goal_id="lt-clock-pin", contract_id="lt-clock-cid")
    _approve_plan_at(conn, "lt-clock-cid", SEEDED + timedelta(seconds=1))

    view = get_contract(conn, "lt-clock-cid")
    assert view is not None
    promoted = auto_approve_drafted_contract(conn, view, SEEDED + timedelta(seconds=2))
    assert promoted is True, (
        "a PLAN_APPROVED that is 1 s old relative to the supplied "
        "clock must authorize auto-activation regardless of the "
        "process wall clock"
    )
    conn.close()


@pytest.mark.parametrize(
    ("age_seconds", "expect_promoted"),
    [
        (7199, True),  # inside PLAN_OVERRIDE_LOOKBACK_SECONDS
        (7201, False),  # outside it — approval has aged out
    ],
)
def test_plan_override_boundary_is_window_before_supplied_clock(
    tmp_path: Path, age_seconds: int, expect_promoted: bool
) -> None:
    """The boundary is exactly ``now - PLAN_OVERRIDE_LOOKBACK_SECONDS``,
    not "however much wall clock has passed since the seed"."""
    from longtask.persistence.store import (
        PLAN_OVERRIDE_LOOKBACK_SECONDS,
        auto_approve_drafted_contract,
    )

    assert PLAN_OVERRIDE_LOOKBACK_SECONDS == 7200
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    goal_id = f"lt-bound-{age_seconds}"
    cid = f"{goal_id}-cid"
    _seed_bounded_goal_and_draft(conn, goal_id=goal_id, contract_id=cid)
    tick = SEEDED + timedelta(seconds=2)
    _approve_plan_at(conn, cid, tick - timedelta(seconds=age_seconds))

    view = get_contract(conn, cid)
    assert view is not None
    promoted = auto_approve_drafted_contract(conn, view, tick)
    assert promoted is expect_promoted, (
        f"approval aged {age_seconds}s before the supplied clock: "
        f"expected promoted={expect_promoted}, got {promoted}"
    )
    conn.close()
