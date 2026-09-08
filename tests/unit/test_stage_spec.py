"""Structured stage spec: 5 required contents + version binding + dispatch wiring.

外部 review 3rd-round 建议：每个阶段明确
- Goal（交付什么结果）
- Spec（功能/接口/约束/不做范围）
- 验收条件（用什么测试/证据判断完成）
- 依赖与产物（开工前需要什么，完成后交给谁）
- 时间/预算/权限（截止、重试、可改范围）

本测试覆盖：
1. StageSpec dataclass 的 5 字段 + 校验
2. spec_hash 稳定性
3. evaluate_stage_acceptance 在 dispatch 路径上的 pass/fail/pending 行为
4. Acceptance dataclass 新加 spec/spec_hash 字段的 round-trip
5. ContractDraft to_dict/from_dict 携带 spec + spec_hash
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lhgp.acceptance.spec import (
    SpecVerdict,
    evaluate_stage_acceptance,
    validate_spec,
    verdict_to_event_payload,
)
from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_draft import from_dict as draft_from_dict
from lhgp.goals.stage import StageSpec

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _spec(
    goal: str = "交付 CLI",
    *,
    with_user: bool = False,
) -> dict:
    """Helper: build a minimal but valid spec."""
    spec = {
        "all": [
            {
                "judge": "machine",
                "kind": "file-exists",
                "target": "dist/app.js",
            }
        ]
    }
    if with_user:
        spec["all"].append(
            {
                "judge": "user",
                "question": "demo 效果 OK 吗？",
            }
        )
    return spec


class TestStageSpecDataclass:
    def test_minimal_round_trip(self) -> None:
        raw = {"goal": "实现登录", "acceptance": _spec()}
        spec = StageSpec.from_dict(raw)
        assert spec.goal == "实现登录"
        assert spec.acceptance == _spec()
        # Round-trip should preserve all fields
        rebuilt = StageSpec.from_dict(spec.to_dict())
        assert rebuilt == spec

    def test_required_fields_validated(self) -> None:
        # Missing goal
        spec = StageSpec(goal="", acceptance=_spec())
        errors = spec.validate()
        assert any("goal" in e for e in errors)
        # Missing acceptance
        spec2 = StageSpec(goal="x", acceptance={})
        errors2 = spec2.validate()
        assert any("acceptance" in e for e in errors2)

    def test_invalid_acceptance_propagated(self) -> None:
        # Acceptance with unknown judge
        spec = StageSpec(goal="x", acceptance={"all": [{"judge": "magic"}]})
        errors = spec.validate()
        assert any("judge" in e for e in errors)

    def test_budget_must_be_positive(self) -> None:
        spec = StageSpec(goal="x", acceptance=_spec(), max_dispatches=0)
        errors = spec.validate()
        assert any("max_dispatches" in e for e in errors)

    def test_spec_hash_deterministic_and_changes_on_edit(self) -> None:
        a = StageSpec(goal="x", acceptance=_spec())
        b = StageSpec(goal="x", acceptance=_spec())
        assert a.spec_hash() == b.spec_hash()
        c = StageSpec(goal="x", acceptance=_spec(with_user=True))
        assert a.spec_hash() != c.spec_hash()
        d = StageSpec(goal="y", acceptance=_spec())
        assert a.spec_hash() != d.spec_hash()


class TestAcceptanceRoundTrip:
    def test_acceptance_carries_spec(self) -> None:
        spec = _spec()
        acc = Acceptance(standard="s", checks=("c1",), spec=spec, spec_hash="abc")
        # Acceptance is a frozen dataclass; serialization happens via
        # ContractDraft.to_dict (covered below).
        assert acc.spec == spec
        assert acc.spec_hash == "abc"
        assert acc.validate() == []

    def test_invalid_spec_in_acceptance_surfaces_error(self) -> None:
        acc = Acceptance(standard="s", checks=("c1",), spec={"all": [{"judge": "magic"}]})
        errors = acc.validate()
        assert any("judge" in e for e in errors)


class TestContractDraftSpecRoundTrip:
    def test_draft_serialization_preserves_spec(self) -> None:
        spec = _spec()
        acceptance = Acceptance(standard="s", checks=("c1",), spec=spec, spec_hash="deadbeef")
        draft = ContractDraft(
            title="t",
            objective="o",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=acceptance,
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1_048_576,
                verification_attempts_reserved=2,
            ),
            auto_approve=AutoApprove(),
        )
        raw = draft.to_dict()
        assert raw["acceptance"]["spec"] == spec
        assert raw["acceptance"]["spec_hash"] == "deadbeef"
        rebuilt = draft_from_dict(raw)
        assert rebuilt.acceptance.spec == spec
        assert rebuilt.acceptance.spec_hash == "deadbeef"


class TestEvaluateStageAcceptance:
    def test_pass_when_all_machine_criteria_pass(self) -> None:
        spec = _spec()
        verdict = evaluate_stage_acceptance(spec, check_results={"file-exists:dist/app.js": "pass"})
        assert isinstance(verdict, SpecVerdict)
        assert verdict.outcome == "pass"
        assert verdict.machine_pass == 1
        assert verdict.machine_fail == 0
        assert verdict.user_pending == 0

    def test_fail_when_any_machine_criterion_fails(self) -> None:
        spec = _spec()
        verdict = evaluate_stage_acceptance(
            spec,
            check_results={"file-exists:dist/app.js": "fail"},
        )
        assert verdict.outcome == "fail"
        assert verdict.machine_fail == 1

    def test_pending_when_user_criterion_present(self) -> None:
        spec = _spec(with_user=True)
        verdict = evaluate_stage_acceptance(spec, check_results={"file-exists:dist/app.js": "pass"})
        assert verdict.outcome == "pending"
        assert verdict.user_pending == 1

    def test_dict_must_be_dict(self) -> None:
        with pytest.raises(ValueError):
            evaluate_stage_acceptance("not a dict", check_results={})

    def test_validate_spec_catches_top_level_only(self) -> None:
        # Leaf with no judge
        errors = validate_spec({"all": [{"kind": "file-exists", "target": "x"}]})
        assert errors

    def test_verdict_to_event_payload(self) -> None:
        spec = _spec()
        verdict = evaluate_stage_acceptance(spec, check_results={"file-exists:dist/app.js": "pass"})
        payload = verdict_to_event_payload(verdict)
        assert payload["outcome"] == "pass"
        assert "results" in payload
        assert isinstance(payload["results"], list)
