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


def test_verifier_event_also_migrates_on_lifecycle_bump(tmp_path: Path) -> None:
    """6th-round P1 fix: the lifecycle migration must
    extend to the verifier ``ATTEMPT_SUCCEEDED`` event.
    Without this, the CANDIDATE transition
    (verifier success → user-criterion-pending) bumps the
    contract revision but leaves the verifier event at
    the old revision; the user_confirm path's
    ``_latest_verifier_evidence`` then filters it out
    and synthesizes a stub.  The real evidence is
    lifecycle-bound, not content-bound; the only
    thing that should invalidate it is a content change
    to ``acceptance``, which the user_confirm path
    checks separately.
    """
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
    cid = "lt-verifier-migrate"
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
    # Verifier success at revision 1.
    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.ATTEMPT_SUCCEEDED,
        payload={"verdict": "succeeded", "checks": [{"check_id": "c1", "outcome": "pass"}]},
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
    assert verifier_events[0].contract_revision == 2, (
        f"verifier event must migrate to the new revision on the CANDIDATE "
        f"lifecycle bump; got contract_revision={verifier_events[0].contract_revision} "
        f"vs contract.revision={post.revision}"
    )
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
