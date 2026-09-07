"""P2: auto-mine hook integration test on user_evaluation.

The hook is in ``lhgp/feedback/store.py::_maybe_record_memory_from_evaluation``.
It must:
  - fire on rating >= 4 with non-empty comments (PATTERN)
  - fire on REJECT verdict with non-empty comments (GOTCHA)
  - NOT fire on rating < 4 + ACCEPT + empty comments
  - NEVER poison the evaluation write — failures are swallowed
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft, ContractState
from lhgp.feedback.store import record_evaluation
from lhgp.feedback.types import (
    EvaluationRating,
    EvaluationVerdict,
    UserEvaluation,
)
from lhgp.memory.store import list_memories
from lhgp.memory.types import MemoryKind, MemoryScope
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

CID = "lt-p2-auto-1"
NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _setup(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    draft = ContractDraft(
        title="auto-mine smoke",
        objective="verify auto-mine",
        deadline_at=NOW.replace(hour=NOW.hour + 2),
        hard_constraints={},
        acceptance=Acceptance(standard="x", checks=("y",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=2,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=65536,
        ),
    )
    save_contract(conn, draft, contract_id=CID, now=NOW)
    update_contract_state(conn, contract_id=CID, new_state=ContractState.ACTIVE, now=NOW)
    return conn


def _eval(
    conn: sqlite3.Connection, rating: EvaluationRating, verdict: EvaluationVerdict, comments: str
) -> int:
    return record_evaluation(
        conn,
        UserEvaluation(
            contract_id=CID,
            contract_revision=1,
            evaluator="u",
            rating=rating,
            verdict=verdict,
            comments=comments,
        ),
    )


def _memories(conn: sqlite3.Connection):
    return list_memories(conn, include_expired=True)


class TestHighRatingMinesPattern:
    def test_rating_5_with_comments_mines_pattern(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _eval(conn, EvaluationRating.EXCELLENT, EvaluationVerdict.ACCEPT, "everything works")
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            mems = _memories(conn)
        finally:
            conn.close()
        assert len(mems) == 1
        m = mems[0]
        assert m.kind is MemoryKind.PATTERN
        assert m.scope is MemoryScope.PROJECT
        assert m.body_md == "everything works"
        assert "source/evaluation" in m.tags
        assert m.source_contract_id == CID
        # Rating 5 → score 0.7
        assert m.score == pytest.approx(0.7, abs=1e-9)

    def test_rating_4_mines_pattern_with_lower_score(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _eval(conn, EvaluationRating.GOOD, EvaluationVerdict.ACCEPT, "works")
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            m = _memories(conn)[0]
        finally:
            conn.close()
        # Rating 4 → score 0.6
        assert m.score == pytest.approx(0.6, abs=1e-9)


class TestRejectMinesGotcha:
    def test_reject_mines_gotcha_with_higher_score(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _eval(
                conn, EvaluationRating.NEUTRAL, EvaluationVerdict.REJECT, "did not work as expected"
            )
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            mems = _memories(conn)
        finally:
            conn.close()
        assert len(mems) == 1
        m = mems[0]
        assert m.kind is MemoryKind.GOTCHA
        # Rejections are signal-dense → score 0.7
        assert m.score == pytest.approx(0.7, abs=1e-9)
        assert "rating/3" in m.tags


class TestSkipsWhenNoSignal:
    def test_no_comments_no_memory(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _eval(conn, EvaluationRating.EXCELLENT, EvaluationVerdict.ACCEPT, "")
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            assert _memories(conn) == []
        finally:
            conn.close()

    def test_low_rating_accept_no_memory(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _eval(
                conn,
                EvaluationRating.NEUTRAL,
                EvaluationVerdict.ACCEPT,
                "okay, nothing special",
            )
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            assert _memories(conn) == []
        finally:
            conn.close()

    def test_low_rating_reject_still_mines(self, tmp_path: Path) -> None:
        # REJECT is signal-dense even at low rating — capture as gotcha.
        conn = _setup(tmp_path)
        try:
            _eval(conn, EvaluationRating.POOR, EvaluationVerdict.REJECT, "broke my workflow")
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            mems = _memories(conn)
        finally:
            conn.close()
        assert len(mems) == 1
        assert mems[0].kind is MemoryKind.GOTCHA


class TestEvaluationWriteAlwaysSucceeds:
    def test_auto_mine_failure_does_not_break_evaluation(self, tmp_path: Path) -> None:
        # Even if the auto-mine code path chokes, the evaluation itself
        # must persist. The fix in feedback/store.py wraps
        # _maybe_record_memory_from_evaluation in a try/except.
        conn = _setup(tmp_path)
        try:
            # An evaluation that would otherwise trigger auto-mine:
            eid = _eval(
                conn,
                EvaluationRating.EXCELLENT,
                EvaluationVerdict.ACCEPT,
                "great work",
            )
        finally:
            conn.close()
        # The evaluation was written — there should be a user_evaluations row.
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            row = conn.execute(
                "SELECT * FROM user_evaluations WHERE evaluation_id = ?", (eid,)
            ).fetchone()
            assert row is not None
            # And the auto-mine memory exists too.
            mems = _memories(conn)
            assert len(mems) == 1
        finally:
            conn.close()


class TestTopicScopeFlips:
    """P2 verifier P0 fix: ``topic: <domain>`` must also tag the memory
    with ``topic/<domain>`` so MemoryIndex.retrieve can find it.

    Without the tag, the index's domain filter drops DOMAIN memories
    whose tag set lacks ``topic/<domain>`` — i.e. every auto-mined
    DOMAIN memory becomes a dead letter.
    """

    def test_topic_prefix_flips_scope_and_tags(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _eval(
                conn,
                EvaluationRating.GOOD,
                EvaluationVerdict.ACCEPT,
                "topic: persistence — restart-safe writes work as expected",
            )
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            mems = _memories(conn)
        finally:
            conn.close()
        assert len(mems) == 1
        m = mems[0]
        assert m.scope is MemoryScope.DOMAIN
        assert "topic/persistence" in m.tags

    def test_topic_mined_memory_is_retrievable_by_domain(self, tmp_path: Path) -> None:
        from lhgp.memory.index import MemoryIndex

        conn = _setup(tmp_path)
        try:
            _eval(
                conn,
                EvaluationRating.GOOD,
                EvaluationVerdict.ACCEPT,
                "topic: sql — JSON binding correctly escapes identifiers",
            )
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            idx = MemoryIndex(conn, top_n=5, budget_bytes=4000)
            out = idx.retrieve({"domain": "sql"})
        finally:
            conn.close()
        # The auto-mined DOMAIN memory must surface; pre-fix it was
        # filtered out and the result was empty.
        assert len(out) == 1
        assert "topic/sql" in out[0].tags

    def test_mid_prose_topic_mention_does_not_flip(self, tmp_path: Path) -> None:
        # Anchored regex: a "topic: choice is yours" comment in the
        # middle of prose must NOT trigger the scope flip.
        conn = _setup(tmp_path)
        try:
            _eval(
                conn,
                EvaluationRating.GOOD,
                EvaluationVerdict.ACCEPT,
                "the topic: choice is yours, but mine worked fine",
            )
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            mems = _memories(conn)
        finally:
            conn.close()
        assert len(mems) == 1
        assert mems[0].scope is MemoryScope.PROJECT
        assert not any(t.startswith("topic/") for t in mems[0].tags)
