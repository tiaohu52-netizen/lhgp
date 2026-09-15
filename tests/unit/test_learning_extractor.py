"""P6: extractor + scoring tests (lhgp.learning.extractor).

The extractor is the entry point of the auto-evolve pipeline: wrong
math here silently degrades every template the evolver writes. Zero
tests existed before. This pins:

- _acceptance_pass_rate heuristic (counting passed/satisfied vs total)
- score_contract_quality: returns None on no evaluation; rating
  normalisation 1..5 → 0..1; weighted blend
- extract_template_signals: min_rating filter, REJECT verdict filter,
  threshold gate, vocabulary + keywords round-trip
- _extract_keywords: stopword filter, dedup, max_tokens cap, non-ascii
  dropped
- is_terminal_for_learning / is_accepted_terminal: state helpers
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft, ContractState
from lhgp.feedback.store import record_evaluation
from lhgp.feedback.types import (
    EvaluationRating,
    EvaluationVerdict,
    UserEvaluation,
)
from lhgp.learning.extractor import (
    _HIGH_QUALITY_THRESHOLD,
    _acceptance_pass_rate,
    _extract_keywords,
    extract_template_signals,
    is_accepted_terminal,
    is_terminal_for_learning,
    score_contract_quality,
)
from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event
from lhgp.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
CID = "lt-p6-ext"


def _draft(deadline: datetime | None = None) -> ContractDraft:
    return ContractDraft(
        title="P6 extractor",
        objective="extract template signals",
        deadline_at=deadline or NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="测试", checks=("通过",)),
        workload_initial_hours=2.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=1048576,
        ),
    )


def _setup(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db))
    ensure_schema(conn)
    save_contract(conn, _draft(), contract_id=CID, now=NOW)
    update_contract_state(conn, contract_id=CID, new_state=ContractState.ACTIVE, now=NOW)
    return conn


def _row_event_type(et: str) -> dict:
    """Build a sqlite3.Row-like object accepted by _acceptance_pass_rate."""
    # _acceptance_pass_rate supports both sqlite3.Row and tuple; build a
    # minimal sqlite3.Row shape by inserting one row.
    return {"event_type": et}


class TestAcceptancePassRate:
    def test_empty_returns_zero(self) -> None:
        assert _acceptance_pass_rate([]) == 0.0

    def test_pure_pass(self) -> None:
        events = [
            _row_event_type("acceptance/passed"),
            _row_event_type("acceptance/satisfied"),
        ]
        assert _acceptance_pass_rate(events) == 1.0

    def test_pure_fail(self) -> None:
        events = [
            _row_event_type("acceptance/failed"),
            _row_event_type("acceptance/rejected"),
        ]
        assert _acceptance_pass_rate(events) == 0.0

    def test_mixed(self) -> None:
        events = [
            _row_event_type("acceptance/passed"),
            _row_event_type("acceptance/failed"),
            _row_event_type("acceptance/calibrated"),
            _row_event_type("acceptance/satisfied"),
        ]
        # 2 passed/satisfied out of 4 = 0.5
        assert _acceptance_pass_rate(events) == pytest.approx(0.5, abs=1e-9)

    def test_caps_at_one_when_only_passes(self) -> None:
        events = [_row_event_type("acceptance/passed")] * 1000
        assert _acceptance_pass_rate(events) == 1.0

    def test_ignores_unrelated_events(self) -> None:
        # 'event/foo' doesn't contain 'acceptance' → not counted.
        events = [
            _row_event_type("event/foo"),
            _row_event_type("contract/approved"),
        ]
        assert _acceptance_pass_rate(events) == 0.0


class TestExtractKeywords:
    def test_empty_text(self) -> None:
        assert _extract_keywords("") == ()
        assert _extract_keywords("   ") == ()

    def test_strips_punctuation(self) -> None:
        result = _extract_keywords("Hello, World!")
        assert "hello" in result
        assert "world" in result

    def test_drops_stopwords(self) -> None:
        result = _extract_keywords("the quick brown fox is on the table")
        assert "the" not in result
        assert "is" not in result
        assert "on" not in result
        assert "fox" in result

    def test_drops_short_tokens(self) -> None:
        result = _extract_keywords("a I go home")
        # 'a', 'I' (1 char) and 'go' (2 char) are too short
        assert "a" not in result
        assert "i" not in result
        assert "go" not in result
        assert "home" in result

    def test_drops_non_ascii(self) -> None:
        result = _extract_keywords("hello 中文 world")
        assert "hello" in result
        assert "world" in result
        assert not any("\u4e00" <= c <= "\u9fff" for c in "".join(result))

    def test_dedup_preserves_first_occurrence_order(self) -> None:
        result = _extract_keywords("alpha beta alpha gamma beta")
        assert result == ("alpha", "beta", "gamma")

    def test_caps_at_max_tokens(self) -> None:
        text = " ".join(f"token{i}" for i in range(20))
        result = _extract_keywords(text, max_tokens=5)
        assert len(result) == 5


class TestStateHelpers:
    def test_is_terminal_for_learning(self) -> None:
        assert is_terminal_for_learning("satisfied") is True
        assert is_terminal_for_learning("cancelled") is True
        assert is_terminal_for_learning("active") is False
        assert is_terminal_for_learning("expired") is False

    def test_is_accepted_terminal(self) -> None:
        assert is_accepted_terminal("satisfied", "passed") is True
        assert is_accepted_terminal("satisfied", "candidate") is True
        assert is_accepted_terminal("satisfied", "failed") is False
        # Cancelled never counts as accepted.
        assert is_accepted_terminal("cancelled", "passed") is False


class TestScoreContractQuality:
    def test_returns_none_without_evaluation(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            assert score_contract_quality(conn, CID, 1) is None
        finally:
            conn.close()

    def test_returns_quality_score_with_evaluation(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            ev = UserEvaluation(
                contract_id=CID,
                contract_revision=1,
                evaluator="u",
                rating=EvaluationRating.GOOD,
                verdict=EvaluationVerdict.ACCEPT,
                comments="good",
            )
            record_evaluation(conn, ev)
            score = score_contract_quality(conn, CID, 1)
        finally:
            conn.close()
        assert score is not None
        # Rating 4 (GOOD) → normalised (4-1)/4 = 0.75
        assert score.user_rating == pytest.approx(0.75, abs=1e-9)
        # No events, no diff → pass_rate=0, diff_eff=1.0
        # overall = 0.75*0.6 + 0*0.25 + 1*0.15 = 0.45 + 0 + 0.15 = 0.6
        assert score.overall == pytest.approx(0.6, abs=1e-9)

    def test_perfect_score(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            ev = UserEvaluation(
                contract_id=CID,
                contract_revision=1,
                evaluator="u",
                rating=EvaluationRating.EXCELLENT,
                verdict=EvaluationVerdict.ACCEPT,
                comments="",
            )
            record_evaluation(conn, ev)
            # Inject contract/satisfied events so pass_rate = 1.0
            # (CONTRACT_SATISFIED is the live positive signal the
            # heuristic actually recognises).
            for _ in range(2):
                append_event(
                    conn,
                    contract_id=CID,
                    event_type=EventType.CONTRACT_SATISFIED,
                    payload={},
                    now=NOW,
                    actor="system",
                    contract_revision=1,
                )
            score = score_contract_quality(conn, CID, 1)
        finally:
            conn.close()
        assert score is not None
        # 1.0 * 0.6 + 1.0 * 0.25 + 1.0 * 0.15 = 1.0
        assert score.overall == pytest.approx(1.0, abs=1e-9)


class TestExtractTemplateSignals:
    def test_no_evaluations_returns_empty(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            assert extract_template_signals(conn) == []
        finally:
            conn.close()

    def test_low_rating_filtered(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            ev = UserEvaluation(
                contract_id=CID,
                contract_revision=1,
                evaluator="u",
                rating=EvaluationRating.NEUTRAL,  # 3 < default 4
                verdict=EvaluationVerdict.ACCEPT,
                comments="",
            )
            record_evaluation(conn, ev)
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            assert extract_template_signals(conn) == []
        finally:
            conn.close()

    def test_reject_verdict_filtered(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            ev = UserEvaluation(
                contract_id=CID,
                contract_revision=1,
                evaluator="u",
                rating=EvaluationRating.EXCELLENT,
                verdict=EvaluationVerdict.REJECT,
                comments="",
            )
            record_evaluation(conn, ev)
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            assert extract_template_signals(conn) == []
        finally:
            conn.close()

    def test_high_quality_signal_emitted(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            ev = UserEvaluation(
                contract_id=CID,
                contract_revision=1,
                evaluator="u",
                rating=EvaluationRating.EXCELLENT,
                verdict=EvaluationVerdict.ACCEPT,
                comments="all-around solid contract",
            )
            record_evaluation(conn, ev)
            # Force overall above threshold via accepted events AND emit
            # one acceptance/status-changed event so the vocabulary
            # extractor has at least one payload to read check_kinds from.
            for _ in range(2):
                append_event(
                    conn,
                    contract_id=CID,
                    event_type=EventType.CONTRACT_SATISFIED,
                    payload={},
                    now=NOW,
                    actor="system",
                    contract_revision=1,
                )
            append_event(
                conn,
                contract_id=CID,
                event_type=EventType.ACCEPTANCE_STATUS_CHANGED,
                payload={"status": "passed", "check_kinds": ["structure-valid"]},
                now=NOW,
                actor="system",
                contract_revision=1,
            )
        finally:
            conn.close()
        conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
        try:
            signals = extract_template_signals(conn)
        finally:
            conn.close()
        assert len(signals) == 1
        s = signals[0]
        assert s.contract_id == CID
        assert s.contract_revision == 1
        assert s.quality.overall >= _HIGH_QUALITY_THRESHOLD
        # vocabulary collected from events
        assert "structure-valid" in s.acceptance_vocabulary
        # title_hint truncated to 80 chars of comments
        assert s.title_hint == "all-around solid contract"
        # keywords picked out of comments
        assert "all-around" in s.objective_keywords or "solid" in s.objective_keywords
        assert "contract" in s.objective_keywords


class TestThresholdConstant:
    def test_threshold_value(self) -> None:
        # 0.7 was the user-chosen bar; lock it down.
        assert _HIGH_QUALITY_THRESHOLD == 0.7
