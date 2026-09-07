"""P6+1: failure-driven memory mining (GOTCHA lesson auto-mine).

Counterpart of the auto-mine tests in
``tests/integration/test_memory_auto_mine.py`` (which cover the
*positive* evaluation path). This file covers the *failure* path
exposed by :func:`lhgp.feedback.lessons.mine_lesson_if_due` and the
hook in :func:`lhgp.feedback.store.record_evaluation`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.feedback.lessons import mine_lesson_if_due
from lhgp.feedback.store import record_evaluation
from lhgp.feedback.types import (
    EvaluationRating,
    EvaluationVerdict,
    UserEvaluation,
)
from lhgp.memory.store import list_memories
from lhgp.memory.types import MemoryKind, MemoryScope
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
)

pytestmark = pytest.mark.unit

CID_A = "lt-lessons-a"
CID_B = "lt-lessons-b"
NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh v4 schema connection (events + memories + user_evaluations)."""
    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    return conn


def _fail_event(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    when: datetime | None = None,
) -> int:
    when = when or NOW
    obj = append_event(
        conn,
        contract_id=contract_id,
        event_type=EventType.ATTEMPT_FAILED,
        payload={"i": 0},
        now=when,
    )
    conn.commit()
    return int(obj.event_id)


def _reject_evaluation(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    when: datetime | None = None,
    comments: str = "nope",
) -> int:
    return record_evaluation(
        conn,
        UserEvaluation(
            contract_id=contract_id,
            contract_revision=1,
            evaluator="u",
            rating=EvaluationRating.POOR,
            verdict=EvaluationVerdict.REJECT,
            comments=comments,
            created_at=when or NOW,
        ),
    )


def _lessons(conn: sqlite3.Connection) -> list:
    return [m for m in list_memories(conn, include_expired=True) if "source/lesson" in m.tags]


def _lesson_events(conn: sqlite3.Connection) -> list[tuple[str, dict]]:
    rows = conn.execute(
        "SELECT event_type, payload_json FROM events WHERE event_type = ? ORDER BY event_id",
        (EventType.MEMORY_LESSON_MINED.value,),
    ).fetchall()
    return [(r[0], json.loads(r[1])) for r in rows]


# ---------------------------------------------------------------------------
# Threshold semantics
# ---------------------------------------------------------------------------


class TestThreshold:
    def test_below_threshold_no_memory(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            _fail_event(conn, CID_A)
            _fail_event(conn, CID_A, when=NOW + timedelta(seconds=1))
            assert mine_lesson_if_due(conn, CID_A, min_failures=3) is None
            assert _lessons(conn) == []
            assert _lesson_events(conn) == []
        finally:
            conn.close()

    def test_at_threshold_one_memory_one_event(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            mid = mine_lesson_if_due(conn, CID_A, min_failures=3)
            assert mid is not None
            assert _lessons(conn) and _lessons(conn)[0].id == mid
            evts = _lesson_events(conn)
            assert len(evts) == 1
            payload = evts[0][1]
            assert payload["contract_id"] == CID_A
            assert payload["memory_id"] == mid
            assert payload["failure_count"] == 3
            assert payload["last_failure_event_id"] is not None
        finally:
            conn.close()

    def test_mixed_failures_count_together(self, tmp_path: Path) -> None:
        # 2 ATTEMPT_FAILED + 1 REJECT evaluation == 3 signals; one lesson.
        # The 3rd REJECT evaluation is also the trigger: the
        # store-level hook fires, so by the time we return the
        # lesson is already in the table.
        conn = _make_conn(tmp_path)
        try:
            _fail_event(conn, CID_A, when=NOW + timedelta(seconds=0))
            _fail_event(conn, CID_A, when=NOW + timedelta(seconds=1))
            _reject_evaluation(conn, CID_A, when=NOW + timedelta(seconds=2))
            lessons = _lessons(conn)
            assert len(lessons) == 1
            assert lessons[0].source_contract_id == CID_A
        finally:
            conn.close()

    def test_reject_evaluations_alone_can_trip(self, tmp_path: Path) -> None:
        # 3 REJECT evaluations without any ATTEMPT_FAILED events.
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _reject_evaluation(conn, CID_A, when=NOW + timedelta(seconds=i))
            # The 3rd REJECT trips the lesson via the store-level hook.
            lessons = _lessons(conn)
            assert len(lessons) == 1
            assert lessons[0].source_contract_id == CID_A
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Idempotency / cross-contract isolation
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_two_calls_same_cluster_mine_once(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            first = mine_lesson_if_due(conn, CID_A, min_failures=3)
            assert first is not None
            second = mine_lesson_if_due(conn, CID_A, min_failures=3)
            assert second is None
            assert len(_lessons(conn)) == 1
            assert len(_lesson_events(conn)) == 1
        finally:
            conn.close()

    def test_at_threshold_then_success_no_extra_memory(self, tmp_path: Path) -> None:
        # After the lesson is mined, recording a SUCCESS evaluation
        # must not trigger another lesson even if a SUCCESS would
        # otherwise be ignored (it is — the hook is REJECT-only).
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            assert mine_lesson_if_due(conn, CID_A, min_failures=3) is not None
            record_evaluation(
                conn,
                UserEvaluation(
                    contract_id=CID_A,
                    contract_revision=1,
                    evaluator="u",
                    rating=EvaluationRating.GOOD,
                    verdict=EvaluationVerdict.ACCEPT,
                    comments="ok now",
                ),
            )
            # Still exactly one lesson, not re-mined.
            assert len(_lessons(conn)) == 1
        finally:
            conn.close()

    def test_other_contract_not_remined(self, tmp_path: Path) -> None:
        # Contract A reaches the threshold; contract B has its own
        # failure that must trip its own lesson, not re-mine A's.
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            a_mid = mine_lesson_if_due(conn, CID_A, min_failures=3)
            assert a_mid is not None
            # After A's lesson, A is fully covered.
            assert mine_lesson_if_due(conn, CID_A, min_failures=3) is None
            # B has its own 3 fails.
            for i in range(3):
                _fail_event(conn, CID_B, when=NOW + timedelta(seconds=10 + i))
            b_mid = mine_lesson_if_due(conn, CID_B, min_failures=3)
            assert b_mid is not None
            assert b_mid != a_mid
            # A still has only one lesson, B has one too.
            lessons_by_cid = {m.source_contract_id for m in _lessons(conn)}
            assert lessons_by_cid == {CID_A, CID_B}
            assert len(_lesson_events(conn)) == 2
        finally:
            conn.close()

    def test_min_failures_threshold_respected(self, tmp_path: Path) -> None:
        # 4 fails with min_failures=5: no lesson.
        conn = _make_conn(tmp_path)
        try:
            for i in range(4):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            assert mine_lesson_if_due(conn, CID_A, min_failures=5) is None
            # 5th fail trips the lesson.
            _fail_event(conn, CID_A, when=NOW + timedelta(seconds=4))
            assert mine_lesson_if_due(conn, CID_A, min_failures=5) is not None
        finally:
            conn.close()

    def test_reject_only_cluster_uses_reject_id_as_source_event_id(self, tmp_path: Path) -> None:
        """When the cluster is REJECT-only (no ATTEMPT_FAILED events),
        the memory's ``source_event_id`` falls back to the most recent
        REJECT's evaluation_id so the audit trail still has provenance
        back to a real signal (not NULL)."""
        conn = _make_conn(tmp_path)
        try:
            # Bypass record_evaluation's hook so we control exactly
            # when the lesson is mined. Insert REJECTs directly.
            reject_ids: list[int] = []
            for i in range(3):
                cur = conn.execute(
                    "INSERT INTO user_evaluations "
                    "(contract_id, contract_revision, evaluator, rating, "
                    "verdict, comments, created_at, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        CID_A,
                        1,
                        "u",
                        1,
                        "reject",
                        f"r{i}",
                        (NOW + timedelta(seconds=i)).isoformat(),
                        4,
                    ),
                )
                reject_ids.append(int(cur.lastrowid))
            conn.commit()

            memory_id = mine_lesson_if_due(conn, CID_A, min_failures=3, now=NOW)
            assert memory_id is not None
            row = conn.execute(
                "SELECT source_event_id FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            assert row is not None
            assert row[0] is not None, "source_event_id must not be NULL"
            # The fallback value must be one of the REJECT ids in the table.
            assert row[0] in reject_ids
            evts = _lesson_events(conn)
            assert len(evts) == 1
            # In a REJECT-only cluster, last_failure_event_id is
            # omitted from the payload; the REJECT fallback is under
            # last_reject_id.
            assert "last_failure_event_id" not in evts[0][1]
            assert evts[0][1]["last_reject_id"] in reject_ids
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Memory shape + audit event payload
# ---------------------------------------------------------------------------


class TestMemoryShape:
    def test_mined_memory_is_global_gotcha(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            mid = mine_lesson_if_due(conn, CID_A, min_failures=3)
            assert mid is not None
            m = _lessons(conn)[0]
            assert m.scope is MemoryScope.GLOBAL
            assert m.kind is MemoryKind.GOTCHA
            assert CID_A in m.title
            assert m.source_contract_id == CID_A
            assert "source/lesson" in m.tags
        finally:
            conn.close()

    def test_mined_memory_survives_index_retrieve(self, tmp_path: Path) -> None:
        # The new kind is queryable by the existing MemoryIndex.retrieve
        # path (GOTCHA + PATTERN are the same shape there).
        from lhgp.memory.index import MemoryIndex

        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            mid = mine_lesson_if_due(conn, CID_A, min_failures=3)
            assert mid is not None
            idx = MemoryIndex(conn, top_n=5, budget_bytes=4000)
            out = idx.retrieve(None)
            titles = {m.title for m in out}
            assert any(CID_A in t for t in titles)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Hook wiring: store.record_evaluation -> mine_lesson_if_due
# ---------------------------------------------------------------------------


class TestStoreWiring:
    def test_reject_evaluation_with_3_fails_mines_via_hook(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            # The 3rd REJECT evaluation is the trigger: the hook fires
            # from record_evaluation's failure path.
            _reject_evaluation(conn, CID_A, when=NOW + timedelta(seconds=10), comments="bad")
            lessons = _lessons(conn)
            assert len(lessons) == 1
            assert lessons[0].source_contract_id == CID_A
            assert _lesson_events(conn), "MEMORY_LESSON_MINED audit missing"
        finally:
            conn.close()

    def test_accept_evaluation_does_not_trip_lesson(self, tmp_path: Path) -> None:
        # 3 ATTEMPT_FAILED + 1 ACCEPT (rating 5): no lesson, because
        # the hook only fires on REJECT.
        conn = _make_conn(tmp_path)
        try:
            for i in range(3):
                _fail_event(conn, CID_A, when=NOW + timedelta(seconds=i))
            record_evaluation(
                conn,
                UserEvaluation(
                    contract_id=CID_A,
                    contract_revision=1,
                    evaluator="u",
                    rating=EvaluationRating.EXCELLENT,
                    verdict=EvaluationVerdict.ACCEPT,
                    comments="actually fine",
                ),
            )
            assert _lessons(conn) == []
            assert _lesson_events(conn) == []
        finally:
            conn.close()

    def test_lesson_failure_does_not_break_evaluation_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the lesson mine raises, the evaluation row must still
        # land — same best-effort contract as the existing
        # auto-mine hook.
        def boom(*_args: object, **_kwargs: object) -> int | None:
            raise RuntimeError("simulated lesson write failure")

        import lhgp.feedback.lessons as lessons_mod

        monkeypatch.setattr(lessons_mod, "mine_lesson_if_due", boom)
        conn = _make_conn(tmp_path)
        try:
            eid = _reject_evaluation(conn, CID_A, comments="broke")
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            row = conn.execute(
                "SELECT 1 FROM user_evaluations WHERE evaluation_id = ?", (eid,)
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
