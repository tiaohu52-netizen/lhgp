"""P6: portfolio + trace tests (lhgp.portfolio.{summary,tracking}).

Pins the dashboard aggregator and the per-contract trace log so a
future schema change or query rewrite doesn't silently mis-classify
counters or drop events. Zero tests existed before.

Coverage:
  portfolio_summary:
    - empty store → empty snapshot, zeroed counters, generated_at set
    - single contract → 1 row, counters at 1
    - multiple contracts → by_state / by_deadline / by_acceptance
      counts match
    - include_terminal=False filters cancelled/expired/archived out
    - last_user_rating / last_user_verdict joined from latest
      user_evaluation row
    - contracts without an evaluation → rating/verdict are None
    - limit=2 → 2 rows returned

  trace_contract:
    - empty contract → 0 entries
    - mixed event types returned in chronological order
    - latest_user_evaluation is the most recent row (to_db_row shape)
    - latest_acceptance_diff is None when no diff
    - contract_revision filter narrows the event stream
    - corrupt payload_json → _raw key, no exception
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft, ContractState
from lhgp.feedback.store import record_diff, record_evaluation
from lhgp.feedback.types import (
    AcceptanceDiff,
    EvaluationRating,
    EvaluationVerdict,
    UserEvaluation,
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
from lhgp.portfolio.summary import portfolio_summary
from lhgp.portfolio.tracking import trace_contract

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _draft() -> ContractDraft:
    return ContractDraft(
        title="P6 portfolio",
        objective="snapshot",
        deadline_at=NOW + timedelta(hours=2),
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
    return conn


def _save(
    conn: sqlite3.Connection, cid: str, *, state: ContractState = ContractState.ACTIVE
) -> None:
    save_contract(conn, _draft(), contract_id=cid, now=NOW)
    if state is not ContractState.ACTIVE:
        # 状态机（审计 B1）：DRAFTED 的出边只有 active/cancelled，经 active 落到
        # 目标态（本 fixture 用到 PAUSED），而不是直写生产不可达的转移。
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=state, now=NOW)


def _eval(
    conn: sqlite3.Connection, cid: str, *, rating: EvaluationRating, verdict: EvaluationVerdict
) -> None:
    record_evaluation(
        conn,
        UserEvaluation(
            contract_id=cid,
            contract_revision=1,
            evaluator="u",
            rating=rating,
            verdict=verdict,
            comments="ok",
        ),
    )


class TestPortfolioEmpty:
    def test_empty_store(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            snap = portfolio_summary(conn)
        finally:
            conn.close()
        assert snap.contracts == ()
        assert snap.by_state == {}
        assert snap.by_deadline == {}
        assert snap.by_acceptance == {}
        assert snap.generated_at  # set to current time


class TestPortfolioSingle:
    def test_one_contract(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-pf-1")
            _eval(conn, "lt-pf-1", rating=EvaluationRating.GOOD, verdict=EvaluationVerdict.ACCEPT)
            snap = portfolio_summary(conn)
        finally:
            conn.close()
        assert len(snap.contracts) == 1
        c = snap.contracts[0]
        assert c.contract_id == "lt-pf-1"
        assert c.state == "active"
        assert c.last_user_rating == 4  # GOOD
        assert c.last_user_verdict == "accept"
        # counters: one of each
        assert snap.by_state == {"active": 1}
        assert snap.by_deadline == {"not_due": 1}
        assert snap.by_acceptance == {"pending": 1}

    def test_no_evaluation_rating_verdict_none(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-pf-no-eval")
            snap = portfolio_summary(conn)
        finally:
            conn.close()
        assert len(snap.contracts) == 1
        c = snap.contracts[0]
        assert c.last_user_rating is None
        assert c.last_user_verdict is None


class TestPortfolioMulti:
    def test_counters_partition(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-pf-a")
            _save(conn, "lt-pf-b")
            _save(conn, "lt-pf-c", state=ContractState.PAUSED)
            snap = portfolio_summary(conn)
        finally:
            conn.close()
        assert {c.contract_id for c in snap.contracts} == {
            "lt-pf-a",
            "lt-pf-b",
            "lt-pf-c",
        }
        assert snap.by_state == {"active": 2, "paused": 1}

    def test_include_terminal_false_filters_cancelled(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-pf-on")
            _save(conn, "lt-pf-off")
            update_contract_state(
                conn,
                contract_id="lt-pf-off",
                new_state=ContractState.CANCELLED,
                now=NOW,
            )
            full = portfolio_summary(conn, include_terminal=True)
            filtered = portfolio_summary(conn, include_terminal=False)
        finally:
            conn.close()
        assert {c.contract_id for c in full.contracts} == {"lt-pf-on", "lt-pf-off"}
        assert {c.contract_id for c in filtered.contracts} == {"lt-pf-on"}
        assert filtered.by_state == {"active": 1}

    def test_limit_caps_results(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            for i in range(5):
                _save(conn, f"lt-pf-cap-{i}")
            snap = portfolio_summary(conn, limit=2)
        finally:
            conn.close()
        assert len(snap.contracts) == 2
        # counters reflect only the first 2 returned (limit applied pre-counter).
        assert sum(snap.by_state.values()) == 2

    def test_latest_evaluation_wins(self, tmp_path: Path) -> None:
        # Two evaluations: older with rating=2, newer with rating=5.
        # Portfolio must show the newer (MAX evaluation_id) value.
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-pf-multi-eval")
            _eval(
                conn,
                "lt-pf-multi-eval",
                rating=EvaluationRating.POOR,
                verdict=EvaluationVerdict.REJECT,
            )
            _eval(
                conn,
                "lt-pf-multi-eval",
                rating=EvaluationRating.EXCELLENT,
                verdict=EvaluationVerdict.ACCEPT,
            )
            snap = portfolio_summary(conn)
        finally:
            conn.close()
        c = snap.contracts[0]
        assert c.last_user_rating == 5
        assert c.last_user_verdict == "accept"


class TestTraceEmpty:
    def test_unknown_contract_returns_empty(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            trace = trace_contract(conn, "lt-no-such")
        finally:
            conn.close()
        assert trace["events_total"] == 0
        assert trace["entries"] == []
        assert trace["latest_user_evaluation"] is None
        assert trace["latest_acceptance_diff"] is None


class TestTracePopulated:
    def test_chronological_order(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-tr-1")
            # _save emits contract/prepared (rev=1) + contract/approved (rev=2,
            # auto-bumped by update_contract_state). Two more events on top.
            for et, rev in [
                (EventType.ATTEMPT_ADMITTED, 2),
                (EventType.ATTEMPT_SUCCEEDED, 2),
            ]:
                append_event(
                    conn,
                    contract_id="lt-tr-1",
                    event_type=et,
                    payload={},
                    now=NOW,
                    actor="test",
                    contract_revision=rev,
                )
            trace = trace_contract(conn, "lt-tr-1")
        finally:
            conn.close()
        assert trace["events_total"] == 4  # prepared + approved + admitted + succeeded
        types = [e["event_type"] for e in trace["entries"]]
        assert types == [
            "contract/prepared",
            "contract/approved",
            "attempt/admitted",
            "attempt/succeeded",
        ]

    def test_revision_filter(self, tmp_path: Path) -> None:
        # save_contract writes prepared at rev=1; update_contract_state(ACTIVE)
        # writes contract/approved at rev=2 (auto-bump). Patches at rev=3.
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-tr-rev")
            append_event(
                conn,
                contract_id="lt-tr-rev",
                event_type=EventType.CONTRACT_PATCHED,
                payload={},
                now=NOW + timedelta(hours=1),
                actor="test",
                contract_revision=3,
            )
            r1 = trace_contract(conn, "lt-tr-rev", contract_revision=1)
            r2 = trace_contract(conn, "lt-tr-rev", contract_revision=2)
            r3 = trace_contract(conn, "lt-tr-rev", contract_revision=3)
        finally:
            conn.close()
        assert [e["event_type"] for e in r1["entries"]] == ["contract/prepared"]
        assert [e["event_type"] for e in r2["entries"]] == ["contract/approved"]
        assert [e["event_type"] for e in r3["entries"]] == ["contract/patched"]

    def test_payload_decoded_from_json(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-tr-payload")
            append_event(
                conn,
                contract_id="lt-tr-payload",
                event_type=EventType.ATTEMPT_SUCCEEDED,
                payload={"result": "ok", "size": 42},
                now=NOW,
                actor="test",
                contract_revision=1,
            )
            trace = trace_contract(conn, "lt-tr-payload")
        finally:
            conn.close()
        payload = trace["entries"][-1]["payload"]
        assert payload == {"result": "ok", "size": 42}

    def test_corrupt_payload_falls_back_to_raw(self, tmp_path: Path) -> None:
        # Manually insert a row whose payload_json is not valid JSON.
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-tr-corrupt")
            conn.execute(
                "INSERT INTO events (contract_id, goal_id, event_type, "
                "payload_json, payload_schema_version, created_at, actor, "
                "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "lt-tr-corrupt",
                    "lt-tr-corrupt",
                    EventType.ACCEPTANCE_STATUS_CHANGED.value,
                    "{not valid json",
                    3,
                    NOW.isoformat(),
                    "broken",
                    3,
                ),
            )
            trace = trace_contract(conn, "lt-tr-corrupt")
        finally:
            conn.close()
        payload = trace["entries"][-1]["payload"]
        assert "_raw" in payload
        assert payload["_raw"] == "{not valid json"

    def test_latest_user_evaluation_shape(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-tr-eval")
            _eval(
                conn,
                "lt-tr-eval",
                rating=EvaluationRating.GOOD,
                verdict=EvaluationVerdict.ACCEPT,
            )
            trace = trace_contract(conn, "lt-tr-eval")
        finally:
            conn.close()
        latest = trace["latest_user_evaluation"]
        assert latest is not None
        # to_db_row shape
        assert latest["contract_id"] == "lt-tr-eval"
        assert latest["rating"] == "4"  # StrEnum
        assert latest["verdict"] == "accept"

    def test_latest_acceptance_diff_none_when_absent(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-tr-no-diff")
            trace = trace_contract(conn, "lt-tr-no-diff")
        finally:
            conn.close()
        assert trace["latest_acceptance_diff"] is None

    def test_latest_acceptance_diff_shape(self, tmp_path: Path) -> None:
        conn = _setup(tmp_path)
        try:
            _save(conn, "lt-tr-diff")
            diff = AcceptanceDiff(
                contract_id="lt-tr-diff",
                contract_revision=1,
                attempt_id=None,
                snapshot_before={"files": []},
                snapshot_after={"files": []},
                files_changed=(),
                summary="0 created, 0 modified, 0 deleted",
                computed_at=NOW,
            )
            record_diff(conn, diff)
            trace = trace_contract(conn, "lt-tr-diff")
        finally:
            conn.close()
        latest_diff = trace["latest_acceptance_diff"]
        assert latest_diff is not None
        assert latest_diff["contract_id"] == "lt-tr-diff"
        assert "summary" in latest_diff
