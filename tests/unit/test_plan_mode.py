"""Plan-mode gate tests (SPEC §12.x).

The plan gate forces an agent to declare a structured plan before an attempt
is dispatched. ``Plan.validate(contract)`` is the single source of truth
for whether the plan is good enough; the runner enforces the result by
requiring a matching ``PLAN_APPROVED`` event before ``DISPATCHING ->
RUNNING``. These tests pin the validator's contract so the gate cannot
silently weaken (e.g. by treating empty rationales as acceptable).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from lhgp.acceptance.checks import CheckKind, CheckSpec
from lhgp.contracts import (
    Acceptance,
    Budget,
    ContractDraft,
    Plan,
    PlanStep,
    PlanValidation,
)
from lhgp.contracts.contract_view import (
    AcceptanceStatus,
    ContractState,
    DeadlineStatus,
)
from lhgp.contracts.contract_view_entity import ContractView
from lhgp.contracts.plan import ALLOWED_ACTIONS

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 7, 22, 0, 0, tzinfo=UTC)
DEADLINE = datetime(2026, 9, 8, 22, 0, 0, tzinfo=UTC)


def make_view(
    *,
    objective: str = "migrate the database to Postgres",
    checks: Iterable[str | CheckSpec] = ("file-exists:dist/app.js",),
) -> ContractView:
    """Build a minimal ContractView for plan validation tests.

    The view is detached from any database — ``Plan.validate`` only reads
    ``draft.objective`` and ``draft.acceptance.checks``.
    """
    draft = ContractDraft(
        title="plan-mode test",
        objective=objective,
        deadline_at=DEADLINE,
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=tuple(checks)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=3,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1_048_576,
        ),
    )
    return ContractView(
        draft=draft,
        contract_id="lt-plan-1",
        goal_id="lt-plan-1",
        revision=1,
        state=ContractState.ACTIVE,
        deadline_status=DeadlineStatus.NOT_DUE,
        acceptance_status=AcceptanceStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        next_wakeup_at=None,
        next_decision_at=None,
        blocked_reason=None,
    )


def make_step(
    *,
    step_id: int = 1,
    action: str = "read file",
    target: str = "src/db.py",
    rationale: str = "inspect current database code for the migration plan",
    expected_outcome: str = (
        "understand current schema; will be cross-checked by file-exists:dist/app.js"
    ),
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        action=action,
        target=target,
        rationale=rationale,
        expected_outcome=expected_outcome,
    )


def make_plan(steps: Iterable[PlanStep]) -> Plan:
    return Plan(
        contract_id="lt-plan-1",
        steps=tuple(steps),
        submitted_at=NOW,
        submitted_by="agent:executor-1",
    )


class TestStructuralRejection:
    def test_empty_plan_rejected(self) -> None:
        plan = make_plan([])
        result = plan.validate(make_view())
        assert isinstance(result, PlanValidation)
        assert result.approved is False
        assert any("at least one step" in r for r in result.rejection_reasons)

    def test_disallowed_action_rejected(self) -> None:
        plan = make_plan([make_step(action="rm -rf /", rationale="purge the migration plan files")])
        result = plan.validate(make_view())
        assert result.approved is False
        assert any("not in allowed actions" in r for r in result.rejection_reasons)

    def test_empty_rationale_rejected(self) -> None:
        plan = make_plan([make_step(rationale="   ")])
        result = plan.validate(make_view())
        assert result.approved is False
        assert any("rationale must not be empty" in r for r in result.rejection_reasons)

    def test_empty_target_rejected(self) -> None:
        plan = make_plan([make_step(target="", rationale="inspect the migration plan file")])
        result = plan.validate(make_view())
        assert result.approved is False
        assert any("target must not be empty" in r for r in result.rejection_reasons)

    def test_empty_expected_outcome_rejected(self) -> None:
        plan = make_plan(
            [
                make_step(
                    rationale="inspect the migration plan file",
                    expected_outcome="",
                )
            ]
        )
        result = plan.validate(make_view())
        assert result.approved is False
        assert any("expected_outcome must not be empty" in r for r in result.rejection_reasons)


class TestObjectiveAlignment:
    def test_keyword_must_appear_in_rationale(self) -> None:
        # Rationale exists but never mentions any non-stop word from
        # "migrate the database to Postgres" → rejected.
        plan = make_plan([make_step(rationale="do the thing with stuff", expected_outcome="done")])
        result = plan.validate(make_view())
        assert result.approved is False
        assert any("objective keyword" in r for r in result.rejection_reasons)

    def test_case_insensitive_keyword_match_accepted(self) -> None:
        # The objective's first non-stop word is "migrate". A rationale
        # with "Migrate" (capitalized) must pass — case-insensitive match.
        plan = make_plan(
            [
                make_step(
                    rationale=(
                        "Migrate the schema to the new layout; covers file-exists:dist/app.js"
                    ),
                    expected_outcome=(
                        "schema migrated; will be cross-checked by file-exists:dist/app.js"
                    ),
                )
            ]
        )
        result = plan.validate(make_view())
        assert result.approved is True, result.rejection_reasons

    def test_cjk_objective_rationale_with_one_shared_char_accepted(self) -> None:
        """A CJK objective without spaces must not require the entire
        objective to appear verbatim in every step's rationale.

        Regression for the P2 review finding: with the old
        ``_pick_keyword`` (single-string return, ``objective.split()``),
        the keyword for "修复登录错误并补充测试" was the entire string,
        and no step rationale could match.
        """
        view = make_view(objective="修复登录错误并补充测试")
        plan = make_plan(
            [
                make_step(
                    rationale="定位登录失败的原因并记录堆栈,确认 file-exists:dist/app.js 还在",
                    expected_outcome="ok; cross-checked by file-exists:dist/app.js",
                )
            ]
        )
        result = plan.validate(view)
        assert result.approved is True, result.rejection_reasons

    def test_cjk_objective_rationale_with_no_shared_char_rejected(self) -> None:
        """A CJK rationale that shares no character with the objective
        must still be rejected — the relaxed rule is any-keyword, not
        any-rationale."""
        view = make_view(objective="修复登录错误并补充测试")
        plan = make_plan(
            [
                make_step(
                    rationale="完全无关的工作内容,确认 file-exists:dist/app.js 还在",
                    expected_outcome="ok; cross-checked by file-exists:dist/app.js",
                )
            ]
        )
        result = plan.validate(view)
        assert result.approved is False
        assert any("objective keyword" in r for r in result.rejection_reasons)


class TestAcceptanceCoverage:
    def test_one_step_per_check_approved(self) -> None:
        checks = (
            CheckSpec(kind=CheckKind.FILE_EXISTS, target="dist/app.js", mandatory=True),
            CheckSpec(kind=CheckKind.FILE_EXISTS, target="dist/index.html", mandatory=True),
            CheckSpec(
                kind=CheckKind.COMMAND_EXIT_ZERO,
                target="pytest -q",
                mandatory=True,
            ),
        )
        view = make_view(checks=checks)
        plan = make_plan(
            [
                PlanStep(
                    step_id=1,
                    action="write file",
                    target="src/app.py",
                    rationale="Migrate the application entry so dist/app.js exists",
                    expected_outcome="file-exists:dist/app.js",
                ),
                PlanStep(
                    step_id=2,
                    action="write file",
                    target="src/index.html",
                    rationale="Migrate the index template so dist/index.html exists",
                    expected_outcome="file-exists:dist/index.html",
                ),
                PlanStep(
                    step_id=3,
                    action="run command",
                    target="pytest -q",
                    rationale="Migrate the test runner invocation",
                    expected_outcome="command-exit-zero:pytest -q",
                ),
            ]
        )
        result = plan.validate(view)
        assert result.approved is True, result.rejection_reasons

    def test_uncovered_check_rejected(self) -> None:
        checks = (
            "file-exists:dist/app.js",
            "file-exists:dist/index.html",
        )
        view = make_view(checks=checks)
        plan = make_plan(
            [
                PlanStep(
                    step_id=1,
                    action="write file",
                    target="src/app.py",
                    rationale="Migrate the application entry",
                    expected_outcome="file-exists:dist/app.js",
                ),
                # No step references dist/index.html.
            ]
        )
        result = plan.validate(view)
        assert result.approved is False
        assert any("dist/index.html" in r for r in result.rejection_reasons)
        assert any("file-exists:dist/app.js" not in r for r in result.rejection_reasons)


class TestAllowedActionsContract:
    def test_allowed_actions_is_frozen_and_nonempty(self) -> None:
        # The allowlist is the contract surface — agents learn it from the
        # skill. Any silent edit (rename, removal) here is a breaking change.
        assert isinstance(ALLOWED_ACTIONS, frozenset)
        assert "read file" in ALLOWED_ACTIONS
        assert "run command" in ALLOWED_ACTIONS
        assert "ask user" in ALLOWED_ACTIONS
        assert "verify acceptance" in ALLOWED_ACTIONS

    @pytest.mark.parametrize(
        "bad",
        ["", "execute", "rm -rf", "Read File", "READ FILE", "deploy"],
    )
    def test_case_sensitive_action_match(self, bad: str) -> None:
        plan = make_plan([make_step(action=bad, rationale="Migrate the database for the plan")])
        result = plan.validate(make_view())
        assert result.approved is False
        assert any("not in allowed actions" in r for r in result.rejection_reasons)


class TestPlanStepDataclass:
    def test_step_is_frozen(self) -> None:
        step = make_step()
        with pytest.raises(dataclasses.FrozenInstanceError):
            step.action = "run command"  # type: ignore[misc]

    def test_step_uses_slots(self) -> None:
        # No __dict__ means a typo on assignment cannot silently create an
        # extra attribute — surfaces bugs at write time.
        assert "__dict__" not in dir(make_step())
