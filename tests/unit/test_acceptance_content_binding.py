"""8th-round review (2026-09-10): verifier evidence must be bound to the
acceptance content by something the runtime controls.

The external reviewer reproduced this through real MCP:

  submit a spec with machine + user criteria, leaving the optional
  ``spec_hash`` out -> verifier passes -> edit the machine criterion to
  "new.txt must exist" -> confirm the user criterion -> the contract lands
  in COMPLETE/passed although nothing checks for new.txt and new.txt does
  not exist.

The mechanism was in ``_latest_verifier_evidence``: it rejected evidence
only when *both* spec hashes were present and differed.  A missing
identity read as an identity match, and ``spec_hash`` is optional and
caller-supplied (``Acceptance.spec_hash`` defaults to ``None``; the MCP
tool only forwards it when the caller happens to pass one).

Two states are worse than the reported one, and both are pinned here:

* patching the acceptance without re-supplying the hash turns the current
  value into ``None``, which also read as "equal" -- so an honest caller
  that had used hashes all along was *less* protected after editing;
* a verifier attempt settled by the crash reconciler carried no identity
  whatsoever, and passed the same comparison.

The fix binds evidence to ``Acceptance.content_fingerprint`` -- a hash the
runtime computes from standard + checks + verifier + spec -- and treats a
missing identity as *no match*, never as a match.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.acceptance.checks import CheckKind, CheckSpec
from lhgp.contracts.acceptance import (
    EVIDENCE_FINGERPRINT_KEY,
    Acceptance,
    evidence_binding,
)
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import AcceptanceStatus
from lhgp.persistence.events import EventType
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from lhgp.rpc.errors import RpcError
from lhgp.rpc.server import PROTOCOL_VERSION, parse_envelope
from longtask.contracts.schema import ContractState
from longtask.persistence.events_query import get_events
from longtask.persistence.store import append_event, patch_contract
from longtask.rpc.handlers.contract import (
    _latest_verifier_evidence,
    handle_contract_user_confirm,
)

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

ORIGINAL = "done.txt says good"
EDITED = "new.txt must exist"


def _acceptance(standard: str = ORIGINAL, spec_hash: str | None = None) -> Acceptance:
    return Acceptance(
        standard=standard,
        checks=(CheckSpec(kind=CheckKind.FILE_EXISTS, target="done.txt"),),
        verifier="cross_check",
        spec={
            "version": 1,
            "rule": "all",
            "criteria": [
                {
                    "id": "m1",
                    "judge": "machine",
                    "check": {"kind": "file-exists", "target": "done.txt"},
                },
                {"id": "u1", "judge": "user", "statement": "looks right to me"},
            ],
        },
        spec_hash=spec_hash,
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(connection)
    yield connection
    connection.close()


def _stage_contract(conn: sqlite3.Connection, acceptance: Acceptance, cid: str = "lt-bind") -> str:
    """Create the contract and drive it to CANDIDATE, the only state from
    which ``user_confirm`` is reachable."""
    save_contract(
        conn,
        ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=acceptance,
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW + timedelta(seconds=1),
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    return cid


def _record_verifier_pass(
    conn: sqlite3.Connection,
    cid: str,
    checked: Acceptance,
    *,
    bound: bool = True,
    revision: int = 2,
) -> None:
    """Write the event a successful verifier attempt leaves behind.

    ``bound=False`` reproduces evidence from before the fingerprint existed
    (and, in the field, a verifier attempt that the reconciler settled
    without stamping).
    """
    payload: dict[str, object] = {
        "verdict": "succeeded",
        "checks": [{"check_id": "m1", "outcome": "pass"}],
    }
    if bound:
        payload.update(evidence_binding(checked))
    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.ATTEMPT_SUCCEEDED,
        payload=payload,
        now=NOW + timedelta(seconds=2),
        actor="verifier",
        role="verifier",
        contract_revision=revision,
    )


def _confirm(conn: sqlite3.Connection, cid: str, request_id: str) -> None:
    envelope = parse_envelope(
        {
            "method": "contract/user-confirm",
            "request_id": request_id,
            "client_id": "cli",
            "protocol_version": PROTOCOL_VERSION,
            "params": {"contract_id": cid, "note": "confirming"},
        }
    )
    handle_contract_user_confirm(envelope, conn=conn, now=NOW + timedelta(seconds=5))


def _completed_events(conn: sqlite3.Connection, cid: str) -> list[str]:
    return [
        str(e.event_type)
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == "contract/completed"
    ]


# ── the fingerprint itself ────────────────────────────────────────────


def test_fingerprint_survives_a_store_round_trip(conn: sqlite3.Connection) -> None:
    """Without this, every restart would invalidate honest evidence and the
    fix would strand running contracts instead of protecting them."""
    acceptance = _acceptance()
    cid = _stage_contract(conn, acceptance)
    stored = get_contract(conn, cid)
    assert stored is not None
    assert stored.draft.acceptance.content_fingerprint == acceptance.content_fingerprint


def test_fingerprint_ignores_how_the_check_was_written(conn: sqlite3.Connection) -> None:
    """A mapping and the ``CheckSpec`` parsed from it must agree.

    The store returns ``CheckSpec``; a caller may build either form.  A
    fingerprint that depended on which one was used would report an edit that
    never happened.
    """
    typed = Acceptance(
        standard=ORIGINAL,
        checks=(CheckSpec(kind=CheckKind.COMMAND_EXIT_ZERO, target="t", args={"argv": ["x"]}),),
    )
    raw = Acceptance(
        standard=ORIGINAL,
        checks=({"kind": "command-exit-zero", "target": "t", "args": {"argv": ["x"]}},),
    )
    assert typed.content_fingerprint == raw.content_fingerprint


@pytest.mark.parametrize(
    "field",
    ["standard", "check-target", "verifier", "spec-criterion", "check-order"],
)
def test_fingerprint_follows_every_substantive_field(field: str) -> None:
    """Each of these is a different promise; the identity must change with it."""
    base = _acceptance()
    if field == "standard":
        other = Acceptance(
            standard=EDITED, checks=base.checks, verifier=base.verifier, spec=base.spec
        )
    elif field == "check-target":
        other = Acceptance(
            standard=base.standard,
            checks=(CheckSpec(kind=CheckKind.FILE_EXISTS, target="new.txt"),),
            verifier=base.verifier,
            spec=base.spec,
        )
    elif field == "verifier":
        other = Acceptance(
            standard=base.standard, checks=base.checks, verifier="none", spec=base.spec
        )
    elif field == "spec-criterion":
        spec = json.loads(json.dumps(base.spec))
        spec["criteria"][0]["check"]["target"] = "new.txt"
        other = Acceptance(
            standard=base.standard, checks=base.checks, verifier=base.verifier, spec=spec
        )
    else:  # check-order
        first = CheckSpec(kind=CheckKind.FILE_EXISTS, target="a.txt")
        second = CheckSpec(kind=CheckKind.FILE_EXISTS, target="b.txt")
        other = Acceptance(
            standard=base.standard,
            checks=(first, second),
            verifier=base.verifier,
            spec=base.spec,
        )
        base = Acceptance(
            standard=base.standard,
            checks=(second, first),
            verifier=base.verifier,
            spec=base.spec,
        )
    assert base.content_fingerprint != other.content_fingerprint


def test_fingerprint_is_immune_to_the_callers_own_label() -> None:
    """``spec_hash`` is metadata; editing it must not move the fingerprint.

    This is what makes the binding unforgable: the caller cannot keep an old
    identity by rewriting the label, nor claim a new requirement by
    relabelling an old one.
    """
    assert (
        _acceptance(spec_hash=None).content_fingerprint
        == _acceptance(spec_hash="whatever-the-agent-said").content_fingerprint
    )


# ── the reviewer's matrix, end to end ────────────────────────────────


@pytest.mark.parametrize(
    ("initial_hash", "edited_hash"),
    [
        pytest.param(None, None, id="both-omitted-the-reviewers-repro"),
        pytest.param("hash-done", None, id="edit-drops-the-hash"),
        pytest.param(None, "hash-new", id="hash-appears-on-edit"),
        pytest.param("hash-done", "hash-new", id="both-labelled"),
    ],
)
def test_editing_the_acceptance_invalidates_the_evidence(
    conn: sqlite3.Connection,
    initial_hash: str | None,
    edited_hash: str | None,
) -> None:
    """Whatever the caller put in ``spec_hash``, an edited acceptance must
    not complete on evidence gathered against the previous one."""
    cid = _stage_contract(conn, _acceptance(spec_hash=initial_hash))
    _record_verifier_pass(conn, cid, _acceptance(spec_hash=initial_hash))
    pre = get_contract(conn, cid)
    assert pre is not None
    patch_contract(
        conn,
        contract_id=cid,
        expected_revision=pre.revision,
        now=NOW + timedelta(seconds=3),
        acceptance=_acceptance(standard=EDITED, spec_hash=edited_hash),
        actor="user",
    )

    with pytest.raises(RpcError) as exc_info:
        _confirm(conn, cid, f"confirm-{initial_hash}-{edited_hash}")
    assert exc_info.value.code.value == "STATE_FORBIDDEN"
    assert "acceptance was edited after the last verifier pass" in str(exc_info.value)
    assert not _completed_events(conn, cid), "the contract must not have completed"
    after = get_contract(conn, cid)
    assert after is not None
    assert after.acceptance_status == AcceptanceStatus.CANDIDATE


def test_evidence_without_any_identity_is_refused_not_trusted(
    conn: sqlite3.Connection,
) -> None:
    """A pre-fingerprint event proves nothing, so it must cost a re-run.

    The refusal has to name that reason: an operator reading "acceptance was
    edited" would go looking for an edit that never happened.
    """
    acceptance = _acceptance()
    cid = _stage_contract(conn, acceptance)
    _record_verifier_pass(conn, cid, acceptance, bound=False)

    with pytest.raises(RpcError) as exc_info:
        _confirm(conn, cid, "confirm-unbound")
    assert "carries no acceptance content fingerprint" in str(exc_info.value)
    assert not _completed_events(conn, cid)


def test_untouched_acceptance_still_confirms(conn: sqlite3.Connection) -> None:
    """The guard must not become a wall: honest evidence, no edit, completes.

    Pinned because fail-closed changes like this one are just as capable of
    locking the happy path as the attack path.
    """
    acceptance = _acceptance()
    cid = _stage_contract(conn, acceptance)
    _record_verifier_pass(conn, cid, acceptance)

    _confirm(conn, cid, "confirm-honest")

    after = get_contract(conn, cid)
    assert after is not None
    assert after.state == ContractState.COMPLETE
    assert after.acceptance_status == AcceptanceStatus.PASSED
    assert _completed_events(conn, cid)


def test_relabelling_spec_hash_over_identical_content_still_refuses(
    conn: sqlite3.Connection,
) -> None:
    """The 6th-round veto is kept on purpose: tightening only.

    Identical content with a new caller label is not something the old code
    accepted, and silently starting to accept it would be the one way for a
    "safety" fix to loosen a boundary.
    """
    cid = _stage_contract(conn, _acceptance(spec_hash="hash-done"))
    _record_verifier_pass(conn, cid, _acceptance(spec_hash="hash-done"))
    pre = get_contract(conn, cid)
    assert pre is not None
    patch_contract(
        conn,
        contract_id=cid,
        expected_revision=pre.revision,
        now=NOW + timedelta(seconds=3),
        acceptance=_acceptance(spec_hash="hash-new"),  # same content, new label
        actor="user",
    )

    with pytest.raises(RpcError) as exc_info:
        _confirm(conn, cid, "confirm-relabel")
    assert "acceptance.spec_hash changed" in str(exc_info.value)
    assert not _completed_events(conn, cid)


def test_matcher_reports_the_reason_the_caller_needs(
    conn: sqlite3.Connection,
) -> None:
    result = _latest_verifier_evidence(conn, _stage_contract(conn, _acceptance()), revision=2)
    assert result == {
        "attempt_id": None,
        "payload": {},
        "matched": False,
        "stale": False,
        "no_event": True,
        "reason": None,
    }


# ── producers cannot drift away from the consumer ────────────────────


def test_every_verifier_success_producer_stamps_the_binding() -> None:
    """Each site that writes a verifier ``ATTEMPT_SUCCEEDED`` must stamp it.

    The matcher is only as good as the last producer that remembered to
    write the identity; this scan is what keeps a fourth producer from being
    added with the same hole.
    """
    src = Path(__file__).resolve().parents[2] / "src"
    # ``event_type=EventType.ATTEMPT_SUCCEEDED`` is how a producer writes the
    # event; files that merely name the enum (its definition, read-side
    # filters, docstrings) are not producers.
    writer = re.compile(r"event_type\s*=\s*EventType\.ATTEMPT_SUCCEEDED")
    producers = [
        path
        for path in sorted(src.rglob("*.py"))
        if writer.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert {p.name for p in producers} == {
        "runner.py",
        "reconcile.py",
        "executor_api.py",
    }, "a new producer appeared; decide whether it must stamp before editing this test"
    unbound = [
        path.relative_to(src.parent).as_posix()
        for path in producers
        if "evidence_binding" not in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert not unbound, f"these write verifier evidence with no content binding: {unbound}"


def test_binding_keys_are_shared_not_duplicated() -> None:
    """Producers and the consumer must agree on the payload key."""
    binding = evidence_binding(_acceptance())
    assert set(binding) == {EVIDENCE_FINGERPRINT_KEY, "spec_hash"}
    assert binding[EVIDENCE_FINGERPRINT_KEY] == _acceptance().content_fingerprint
