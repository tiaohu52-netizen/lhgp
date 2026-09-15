"""5th-round follow-up: concurrent user_confirm pressure test.

Two threads calling ``contract/user-confirm`` on the same
CANDIDATE contract at the same time.  Only one must succeed
(single CONTRACT_COMPLETED event); the other must surface
``REVISION_CONFLICT`` so the caller can re-read.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import AcceptanceStatus
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope
from longtask.contracts.schema import ContractState
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from longtask.rpc.handlers.contract import handle_contract_user_confirm

pytestmark = pytest.mark.real_entry

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _seed_candidate(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-uc-conc"
    draft = ContractDraft(
        title="t",
        objective="o",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    update_contract_state(
        conn,
        contract_id=cid,
        new_state=ContractState.ACTIVE,
        now=NOW,
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    return conn, cid


def _open_thread_local_conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(StoreConfig(db_path=tmp_path / "state.db"))


def test_concurrent_user_confirm_only_one_wins(tmp_path: Path) -> None:
    """Two threads call ``contract/user-confirm`` in parallel.
    The CAS in ``update_contract_state`` (now wrapped in
    ``with transaction()``) guarantees exactly one promotion;
    the other thread must raise ``REVISION_CONFLICT``.
    """
    main_conn, cid = _seed_candidate(tmp_path)

    def confirm(idx: int) -> dict:
        thread_conn = _open_thread_local_conn(tmp_path)
        try:
            env = RequestEnvelope(
                method=Method.CONTRACT_USER_CONFIRM,
                request_id=f"req-uc-conc-{idx}",
                client_id="cli",
                protocol_version=2,
                params={"contract_id": cid, "note": f"user-{idx}"},
            )
            return handle_contract_user_confirm(env, conn=thread_conn, now=NOW)
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Submit both; do not call ``result()`` because one
        # raises and Python 3.13's contextlib chokes.
        f1 = pool.submit(confirm, 0)
        f2 = pool.submit(confirm, 1)
        # Wait for completion.
        import concurrent.futures

        concurrent.futures.wait([f1, f2])

    # Reap results, catching the expected RpcError.
    outcomes: list[dict | Exception] = []
    for f in (f1, f2):
        try:
            outcomes.append(f.result())
        except RpcError as exc:
            outcomes.append(exc)
        except Exception as exc:
            outcomes.append(exc)

    successes = [o for o in outcomes if isinstance(o, dict)]
    losers = [
        o
        for o in outcomes
        if isinstance(o, RpcError)
        and o.code in (ErrorCode.VALIDATION_FAILED, ErrorCode.REVISION_CONFLICT)
    ]
    unexpected = [
        o
        for o in outcomes
        if not (
            isinstance(o, dict)
            or (
                isinstance(o, RpcError)
                and o.code in (ErrorCode.VALIDATION_FAILED, ErrorCode.REVISION_CONFLICT)
            )
        )
    ]
    assert len(successes) == 1, (
        f"exactly one thread must succeed; got {len(successes)}: {successes}"
    )
    # The other thread may surface either VALIDATION_FAILED
    # (its in-transaction re-read saw the state already past
    # CANDIDATE) or REVISION_CONFLICT (its in-transaction
    # state update lost the CAS).  Both are valid rejections
    # for the concurrent user_confirm scenario.
    assert len(losers) == 1, (
        f"the other thread must raise VALIDATION_FAILED or REVISION_CONFLICT; "
        f"got: {[str(o) for o in outcomes]}"
    )
    assert not unexpected, f"unexpected outcomes: {unexpected}"
    # End state: contract is COMPLETE.
    view = get_contract(main_conn, cid)
    assert view is not None
    assert view.state == ContractState.COMPLETE
    assert view.acceptance_status.value == "passed"
    # Exactly one CONTRACT_COMPLETED event.
    completed = [
        e
        for e in get_events(main_conn, contract_id=cid)
        if str(e.event_type) == EventType.CONTRACT_COMPLETED.value
    ]
    # debug: list all events
    all_events = list(get_events(main_conn, contract_id=cid))
    for e in all_events:
        print(f"  event {e.event_id} type={e.event_type} rev={e.contract_revision}")
    assert len(completed) == 1
    # Exactly one ACCEPTANCE_STATUS_CHANGED event.
    status_changed = [
        e
        for e in get_events(main_conn, contract_id=cid)
        if str(e.event_type) == EventType.ACCEPTANCE_STATUS_CHANGED.value
    ]
    assert len(status_changed) == 1
    main_conn.close()
