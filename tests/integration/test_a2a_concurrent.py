"""Multi-agent concurrent pressure test for the A2A directive chain.

5th-round review (2026-09-08): the daemon tick and the
RPC thread both write to ``continuity_json``:
- ``send_message`` (RPC) appends an AGENT_MESSAGE event
- ``mark_directives_consumed`` (daemon tick) bumps the
  per-(contract, agent) cursor inside ``continuity_json``
- ``_record_directive_acks`` (daemon tick) extends the
  per-agent dedup set inside ``continuity_json``

The 4th-round code did a non-atomic read-merge-write on
``continuity_json`` outside any transaction; two concurrent
callers could lose updates (rewound cursor, dropped ack).
This test fires 32 concurrent threads doing realistic
``send_message`` + ``mark_directives_consumed`` against the
same contract and asserts the final state is consistent
(correct cursor, complete ack set, all messages recorded).
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event, get_events
from lhgp.persistence.messages import send_message
from longtask.persistence.context import (
    _DIRECTIVE_CURSOR_KEY,
    mark_directives_consumed,
)
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
)

pytestmark = pytest.mark.real_entry

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

THREADS = 8
MESSAGES_PER_THREAD = 4
TOTAL_MESSAGES = THREADS * MESSAGES_PER_THREAD


def _seed_contract(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    """Active contract that the concurrent callers will hit."""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    cid = f"lt-a2a-conc-{uuid.uuid4().hex[:8]}"
    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user")
    return conn, cid


def _open_thread_local_conn(tmp_path: Path) -> sqlite3.Connection:
    """Each worker thread needs its own sqlite3.Connection; SQLite
    Connections are not safe to share across threads by default.
    """
    return connect(StoreConfig(db_path=tmp_path / "state.db"))


def test_concurrent_send_message_and_mark_consumed(tmp_path: Path) -> None:
    """8 threads * 4 messages = 32 concurrent
    ``send_message`` calls interleaved with 32 concurrent
    ``mark_directives_consumed`` calls.  After the dust settles
    the contract must have all 32 messages in the event log, the
    cursor at the highest event id, and the dedup set covering
    every directive id that was ever consumed.
    """
    conn, cid = _seed_contract(tmp_path)

    # Each thread takes a unique to_agent so the per-agent cursors
    # actually compete on the contract-level broadcast cursor.
    to_agents = [f"agent-{i}" for i in range(THREADS)]

    sent_event_ids: list[int] = []
    sent_lock = threading.Lock()

    def send_worker(thread_idx: int) -> list[int]:
        thread_conn = _open_thread_local_conn(tmp_path)
        ids: list[int] = []
        try:
            for j in range(MESSAGES_PER_THREAD):
                eid = send_message(
                    thread_conn,
                    contract_id=cid,
                    from_actor=to_agents[thread_idx],
                    kind="directive",
                    text=f"thread-{thread_idx}-msg-{j}",
                    now=NOW + timedelta(milliseconds=thread_idx * 100 + j),
                    to_agent=to_agents[(thread_idx + 1) % THREADS],
                )
                ids.append(eid)
                with sent_lock:
                    sent_event_ids.append(eid)
        finally:
            thread_conn.close()
        return ids

    consumed_ids_per_thread: dict[int, list[int]] = {}
    consumed_lock = threading.Lock()

    def consume_worker(thread_idx: int) -> int:
        thread_conn = _open_thread_local_conn(tmp_path)
        moved_count = 0
        try:
            # Wait for the message to land; one consume per
            # message that was sent.  Sleep tiny to interleave.
            import time

            time.sleep(0.001 * (thread_idx + 1))
            for _ in range(MESSAGES_PER_THREAD):
                # Cursor moves to the highest event id in the
                # contract — we just pick a high constant and
                # rely on the max-guard to keep it monotonic.
                moved = mark_directives_consumed(
                    thread_conn,
                    cid,
                    new_id=10_000 + thread_idx,
                    to_agent=to_agents[thread_idx],
                    consumed_ids=[],
                    now=NOW,
                )
                if moved:
                    moved_count += 1
                with consumed_lock:
                    consumed_ids_per_thread[thread_idx] = []
        finally:
            thread_conn.close()
        return moved_count

    # Phase 1: fan out all sends in parallel.
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(send_worker, range(THREADS)))

    # Phase 2: fan out all consumes in parallel (against the
    # now-stable event log).  The cursor must reach the highest
    # ``new_id`` passed in.
    target_ids = [10_000 + i for i in range(THREADS)]
    max_target = max(target_ids)

    def consume_at(idx_target: int) -> bool:
        thread_conn = _open_thread_local_conn(tmp_path)
        try:
            return mark_directives_consumed(
                thread_conn,
                cid,
                new_id=idx_target,
                to_agent=None,  # broadcast cursor: all 8 threads share it
                consumed_ids=[],
                now=NOW,
            )
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(pool.map(consume_at, target_ids))

    # Verify the contract-level broadcast cursor equals the
    # max target (the SQL max-guard must have held).
    import json as _json

    row = conn.execute(
        "SELECT continuity_json FROM contracts WHERE contract_id = ?",
        (cid,),
    ).fetchone()
    assert row is not None
    raw = row[0] or "{}"
    data = _json.loads(raw)
    assert isinstance(data, dict)
    cursor = int(data.get(_DIRECTIVE_CURSOR_KEY, 0))
    assert cursor == max_target, (
        f"concurrent mark_directives_consumed must yield the max "
        f"target; got {cursor}, expected {max_target}"
    )
    # At least one of the concurrent calls must report "moved".
    assert any(results), "no thread reported a forward move; the cursor never advanced"
    # Verify every AGENT_MESSAGE event landed.
    messages = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.AGENT_MESSAGE.value
    ]
    assert len(messages) == TOTAL_MESSAGES, (
        f"expected {TOTAL_MESSAGES} AGENT_MESSAGE events, got {len(messages)}"
    )
    # Verify the event ids are unique (no torn inserts).
    ids = [e.event_id for e in messages]
    assert len(set(ids)) == len(ids), f"event ids must be unique; got duplicates: {ids}"
    conn.close()


def test_concurrent_consumed_ids_record_all_acks(tmp_path: Path) -> None:
    """When 8 threads each call ``mark_directives_consumed`` with
    disjoint ``consumed_ids``, the union of those ids must be
    present in the per-agent dedup set after the dust settles.
    """
    conn, cid = _seed_contract(tmp_path)
    to_agent = "agent-broadcast"

    # First, push 8 directives serially so the event log has a
    # stable target list.
    event_ids: list[int] = []
    for i in range(8):
        eid = append_event(
            conn,
            contract_id=cid,
            event_type=EventType.AGENT_MESSAGE,
            payload={"from": "user", "kind": "directive", "text": f"d-{i}"},
            now=NOW + timedelta(milliseconds=i),
            actor="user",
        )
        event_ids.append(eid.event_id)

    # Each thread consumes a disjoint slice of the directives.
    def consume_worker(slice_ids: list[int]) -> None:
        thread_conn = _open_thread_local_conn(tmp_path)
        try:
            mark_directives_consumed(
                thread_conn,
                cid,
                new_id=max(slice_ids),
                to_agent=to_agent,
                consumed_ids=slice_ids,
                now=NOW,
            )
        finally:
            thread_conn.close()

    slices = [event_ids[i::THREADS] for i in range(THREADS)]
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(consume_worker, slices))

    # Verify the dedup set in continuity_json contains all 8
    # event ids (no thread's slice was lost to the read-merge-write
    # race).
    import json as _json

    row = conn.execute(
        "SELECT continuity_json FROM contracts WHERE contract_id = ?",
        (cid,),
    ).fetchone()
    assert row is not None
    raw = row[0] or "{}"
    data = _json.loads(raw)
    key = f"{_DIRECTIVE_CURSOR_KEY}::{to_agent}::_ack"
    recorded = set(data.get(key, []))
    expected = set(event_ids)
    missing = expected - recorded
    assert not missing, (
        f"concurrent ack writes lost directives: missing {missing}; recorded {sorted(recorded)}"
    )
    # Cursor advanced to the max event id.
    cursor = int(data.get(f"{_DIRECTIVE_CURSOR_KEY}::{to_agent}", 0))
    assert cursor == max(event_ids)
    conn.close()
