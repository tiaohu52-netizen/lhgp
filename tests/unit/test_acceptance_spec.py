"""Acceptance spec + calibration regression (branch feature/acceptance-calibration)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lhgp.acceptance.spec import (
    compose_verdict,
    evaluate_machine_checks,
    spec_from_dict,
    spec_to_dict,
    validate_spec,
)
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import save_contract

NOW = datetime(2026, 9, 6, 14, 0, 0, tzinfo=UTC)


class TestSpecValidation:
    def test_valid_composite_spec(self) -> None:
        spec = {
            "all": [
                {"judge": "machine", "kind": "file-exists", "target": "report.md"},
                {
                    "judge": "machine",
                    "kind": "output-contains",
                    "target": "pytest",
                    "args": {"text": "passed"},
                },
                {"any": [{"judge": "user"}]},
            ]
        }
        errors = validate_spec(spec)
        assert not errors, f"unexpected errors: {errors}"

    def test_agent_judge_rejected(self) -> None:
        """agent judge 已移除——非确定、可欺骗、伪精确。"""
        spec = {"judge": "agent", "prompt": "Is it good?"}
        errors = validate_spec(spec)
        assert any("judge" in e for e in errors)

    def test_invalid_judge_rejected(self) -> None:
        spec = {"judge": "oracle", "kind": "file-exists", "target": "x"}
        errors = validate_spec(spec)
        assert any("judge" in e for e in errors)

    def test_machine_requires_kind_and_target(self) -> None:
        spec = {"judge": "machine"}
        errors = validate_spec(spec)
        assert any("kind" in e for e in errors)
        assert any("target" in e for e in errors)

    def test_empty_combinator_rejected(self) -> None:
        spec = {"all": []}
        errors = validate_spec(spec)
        assert any("empty" in e for e in errors)

    def test_roundtrip_serialization(self) -> None:
        spec = {"all": [{"judge": "user", "question": "OK?"}]}
        raw = spec_to_dict(spec)
        parsed = spec_from_dict(raw)
        assert parsed == spec
        assert spec_from_dict("not json") is None


class TestSpecEvaluation:
    def test_machine_checks_evaluated(self) -> None:
        spec = {
            "all": [
                {"judge": "machine", "kind": "file-exists", "target": "a.txt"},
                {"judge": "machine", "kind": "command-exit-zero", "target": "pytest"},
            ]
        }
        results = evaluate_machine_checks(
            spec,
            check_results={
                "file-exists:a.txt": "pass",
                "command-exit-zero:pytest": "fail",
            },
        )
        assert len(results) == 2
        assert results[0].outcome == "pass"
        assert results[1].outcome == "fail"

    def test_verdict_composition(self) -> None:
        spec = {
            "all": [
                {"judge": "machine", "kind": "file-exists", "target": "a.txt"},
                {"judge": "user"},
            ]
        }
        results = evaluate_machine_checks(spec, check_results={"file-exists:a.txt": "pass"})
        verdict = compose_verdict(spec, machine_results=results)
        # Machine passed but user pending → overall pending
        assert verdict.outcome == "pending"
        assert verdict.machine_pass == 1
        assert verdict.user_pending == 1

    def test_verdict_fail_dominates_pending(self) -> None:
        spec = {
            "all": [
                {"judge": "machine", "kind": "command-exit-zero", "target": "pytest"},
                {"judge": "user"},
            ]
        }
        results = evaluate_machine_checks(spec, check_results={"command-exit-zero:pytest": "fail"})
        verdict = compose_verdict(spec, machine_results=results)
        assert verdict.outcome == "fail"

    def test_any_combinator_pass_dominates(self) -> None:
        spec = {
            "any": [
                {"judge": "machine", "kind": "file-exists", "target": "a.txt"},
                {"judge": "user"},
            ]
        }
        results = evaluate_machine_checks(spec, check_results={"file-exists:a.txt": "pass"})
        verdict = compose_verdict(spec, machine_results=results)
        # any: one pass is enough even if user pending
        assert verdict.outcome == "pass"


class TestCalibration:
    def _conn(self, tmp_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "state.db")
        ensure_schema(conn)
        save_contract(
            conn,
            contract_id="lt-cal01",
            draft=ContractDraft(
                title="cal",
                objective="calibration test",
                deadline_at=NOW + timedelta(hours=4),
                hard_constraints={},
                acceptance=Acceptance(standard="s", checks=("c1",)),
                workload_initial_hours=1.0,
                budget=Budget(
                    max_dispatches=5,
                    max_escalations=1,
                    max_concurrent_attempts=1,
                    max_attempt_minutes=30,
                    max_output_bytes=1048576,
                ),
            ),
            now=NOW,
            actor="user",
        )
        return conn

    def test_record_and_summarize(self, tmp_path: Path) -> None:
        from lhgp.persistence.calibration import (
            calibration_summary,
            record_acceptance_outcome,
        )

        conn = self._conn(tmp_path)
        try:
            # 用户接受，2 个 check 都 pass
            record_acceptance_outcome(
                conn,
                contract_id="lt-cal01",
                goal_id="lt-cal01",
                check_results=[
                    {"check_id": "c1", "kind": "file-exists", "target": "a.txt", "outcome": "pass"},
                    {
                        "check_id": "c2",
                        "kind": "command-exit-zero",
                        "target": "pytest",
                        "outcome": "pass",
                    },
                ],
                user_action="accepted",
                now=NOW,
            )
            summary = calibration_summary(conn)
            assert summary["total_observations"] == 1
            assert "file-exists" in summary["by_kind"]
            assert summary["by_kind"]["file-exists"]["true_positive_rate"] == 1.0
        finally:
            conn.close()

    def test_false_positive_tracking(self, tmp_path: Path) -> None:
        """verifier pass 但用户拒绝 = false positive。"""
        from lhgp.persistence.calibration import (
            calibration_summary,
            record_acceptance_outcome,
        )

        conn = self._conn(tmp_path)
        try:
            record_acceptance_outcome(
                conn,
                contract_id="lt-cal01",
                goal_id="lt-cal01",
                check_results=[
                    {"check_id": "c1", "kind": "file-exists", "target": "a.txt", "outcome": "pass"},
                ],
                user_action="rejected",
                now=NOW,
            )
            summary = calibration_summary(conn)
            assert summary["by_kind"]["file-exists"]["false_positive_rate"] == 1.0
        finally:
            conn.close()
