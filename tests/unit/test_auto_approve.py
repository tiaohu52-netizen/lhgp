"""Daemon-driven DRAFTED -> ACTIVE auto-approval.

4th-round follow-up (2026-09-08): the ``contract/auto-approve``
endpoint was added by the submit-and-leave chain.  Like the
prior ``contract/user-confirm`` and ``lhgp/plan/signoff`` cases,
the Principal gate lives in the handler, not the MCP wrapper,
and the 3rd/4th-round reviews showed that mock-envelope unit
tests routinely miss the real-entry wiring.  This file
exercises the handler with real ``RequestEnvelope`` objects so
the daemon-only allowlist, the ``auto_approve.enabled`` server-
side re-check, the state-skip path, and the request_id
idempotency are all pinned.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope
from longtask.contracts.schema import ContractState
from longtask.rpc.handlers.contract import handle_contract_auto_approve

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

pytestmark = pytest.mark.real_entry


def _seed_drafted_contract(
    tmp_path: Path, *, auto_approve: AutoApprove
) -> tuple[sqlite3.Connection, str]:
    """Contract in DRAFTED, with the given auto_approve settings."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = "lt-aa-rpc-1"
    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=auto_approve,
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    return conn, cid


def _envelope(client_id: str, cid: str) -> RequestEnvelope:
    return RequestEnvelope(
        method=Method.CONTRACT_AUTO_APPROVE,
        request_id=f"req-aa-{cid}-{client_id}",
        client_id=client_id,
        protocol_version=2,
        params={"contract_id": cid},
    )


# happy path


def test_daemon_promotes_drafted_to_active(tmp_path: Path) -> None:
    """Daemon auto-approve transitions DRAFTED -> ACTIVE only;
    acceptance_status is set to PASSED by the executor's verifier
    path, not by this handler (verifier success writes
    ACCEPTANCE_STATUS_CHANGED + CONTRACT_COMPLETED, then the
    completion path sets PASSED)."""
    conn, cid = _seed_drafted_contract(
        tmp_path, auto_approve=AutoApprove(enabled=True, actions=("write file",))
    )
    env = _envelope("daemon", cid)
    result = handle_contract_auto_approve(env, conn=conn, now=NOW)
    assert result["ok"] is True
    view = get_contract(conn, cid)
    assert view.state == ContractState.ACTIVE
    assert view.acceptance_status.value == "pending"
    approved = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.CONTRACT_APPROVED.value
    ]
    assert len(approved) == 1
    assert approved[0].actor == "daemon"
    conn.close()


# daemon-only allowlist


@pytest.mark.parametrize("client_id", ["mcp", "executor", "verifier", "cli", "longtask-cli"])
def test_non_daemon_clients_rejected(tmp_path: Path, client_id: str) -> None:
    conn, cid = _seed_drafted_contract(
        tmp_path, auto_approve=AutoApprove(enabled=True, actions=("write file",))
    )
    env = _envelope(client_id, cid)
    with pytest.raises(RpcError) as exc_info:
        handle_contract_auto_approve(env, conn=conn, now=NOW)
    assert exc_info.value.code == ErrorCode.AUTH_FAILED
    view = get_contract(conn, cid)
    assert view.state == ContractState.DRAFTED
    conn.close()


# auto_approve.enabled server-side re-check


def test_disabled_auto_approve_rejected(tmp_path: Path) -> None:
    conn, cid = _seed_drafted_contract(tmp_path, auto_approve=AutoApprove(enabled=False))
    env = _envelope("daemon", cid)
    with pytest.raises(RpcError) as exc_info:
        handle_contract_auto_approve(env, conn=conn, now=NOW)
    assert exc_info.value.code == ErrorCode.VALIDATION_FAILED
    view = get_contract(conn, cid)
    assert view.state == ContractState.DRAFTED
    conn.close()


# state-skip (already non-DRAFTED)


def test_already_active_skipped(tmp_path: Path) -> None:
    conn, cid = _seed_drafted_contract(tmp_path, auto_approve=AutoApprove(enabled=True))
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    env = _envelope("daemon", cid)
    result = handle_contract_auto_approve(env, conn=conn, now=NOW)
    inner = result.get("result") if isinstance(result, dict) else None
    assert isinstance(inner, dict)
    assert inner.get("skipped") is True
    conn.close()


# idempotency (replay same request_id)


def test_idempotent_replay(tmp_path: Path) -> None:
    conn, cid = _seed_drafted_contract(tmp_path, auto_approve=AutoApprove(enabled=True))
    env = _envelope("daemon", cid)
    first = handle_contract_auto_approve(env, conn=conn, now=NOW)
    second = handle_contract_auto_approve(env, conn=conn, now=NOW)
    # Second call replays the original outcome (revision did not
    # bump again; the contract stays ACTIVE; only one
    # CONTRACT_APPROVED event in the log).
    assert second.get("result") == first.get("result")
    approved = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.CONTRACT_APPROVED.value
    ]
    assert len(approved) == 1
    conn.close()


# unknown contract


def test_unknown_contract_rejected(tmp_path: Path) -> None:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    env = _envelope("daemon", "lt-aa-does-not-exist")
    with pytest.raises(RpcError) as exc_info:
        handle_contract_auto_approve(env, conn=conn, now=NOW)
    assert exc_info.value.code == ErrorCode.UNKNOWN_CONTRACT
    conn.close()
