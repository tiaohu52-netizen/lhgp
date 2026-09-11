"""budget.max_cost 强制（§6.3：唯一按钱画线的预算）。

台账（SPEC §12.3.1）回答「烧了多少」，max_cost 回答「最多烧多少」。
强制点在 decide()：成本耗尽 → HAND_TO_USER，且必须先于「无租约 STEER 转
RESPAWN」——转向重派同样要拉新会话花钱。未声明 max_cost 的合同零差异。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import from_dict as draft_from_dict
from lhgp.contracts.validation import validate_raw
from lhgp.promoter.escalation import decide
from lhgp.promoter.urgency import UrgencyTier

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


class TestBudgetMaxCostParsing:
    def test_absent_defaults_to_none(self) -> None:
        draft = draft_from_dict(
            {
                "title": "t",
                "objective": "o",
                "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
                "acceptance": {"standard": "s", "checks": ["c1"], "verifier": "cross_check"},
                "workload_estimate": {"initial_hours": 1.0},
                "budget": {
                    "max_dispatches": 3,
                    "max_escalations": 1,
                    "max_concurrent_attempts": 1,
                    "max_attempt_minutes": 30,
                    "max_output_bytes": 1024,
                },
            }
        )
        assert draft.budget.max_cost is None

    def test_explicit_null_is_refused_not_treated_as_absent(self) -> None:
        """显式 null 与缺席是两种声明：null 拒收（对齐 JSON schema）。"""
        with pytest.raises(TypeError, match="must not be null"):
            draft_from_dict(_draft_dict(max_cost=None))

    def test_present_positive_parses(self) -> None:
        draft = draft_from_dict(_draft_dict(max_cost=12.5))
        assert draft.budget.max_cost == 12.5

    @pytest.mark.parametrize("bad", [-1, 0, True, "12.5"])
    def test_invalid_values_are_refused(self, bad: Any) -> None:
        with pytest.raises(TypeError, match="max_cost"):
            draft_from_dict(_draft_dict(max_cost=bad))


class TestBudgetValidate:
    def test_validate_flags_non_positive_max_cost(self) -> None:
        assert Budget(
            max_dispatches=1,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=1,
            max_output_bytes=1,
            max_cost=0,
        ).validate() == ["budget.max_cost must be a positive number, got 0"]

    def test_validate_accepts_positive_max_cost(self) -> None:
        assert (
            Budget(
                max_dispatches=1,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=1,
                max_output_bytes=1,
                max_cost=3.14,
            ).validate()
            == []
        )


class TestRawValidation:
    def test_negative_max_cost_reported(self) -> None:
        errors = validate_raw(_draft_dict(max_cost=-2.0))
        assert any("budget.max_cost" in e for e in errors)

    def test_valid_max_cost_no_errors(self) -> None:
        assert validate_raw(_draft_dict(max_cost=9.0)) == []


class TestDecideCostBudget:
    def _decide(self, **kwargs: Any) -> Any:
        return decide(
            UrgencyTier.STEER,
            lease_alive=False,
            budget_dispatches_left=3,
            budget_escalations_left=1,
            estimate_stalled=False,
            **kwargs,
        )

    def test_cost_exhausted_hands_to_user_before_steer_conversion(self) -> None:
        """成本耗尽必须先于 STEER→RESPAWN 换挡：转向重派同样要花钱。"""
        decision = self._decide(budget_cost_left=0.0)
        assert decision.tier == UrgencyTier.HAND_TO_USER
        assert "cost budget exhausted" in decision.reason

    def test_cost_partially_left_allows_respawn(self) -> None:
        decision = self._decide(budget_cost_left=0.01)
        assert decision.tier == UrgencyTier.RESPAWN

    def test_cost_left_none_is_unchanged(self) -> None:
        """未声明 max_cost：行为与既有完全一致（向后兼容）。"""
        assert self._decide().tier == UrgencyTier.RESPAWN

    def test_cost_exhaustion_applies_to_parallel_tier_too(self) -> None:
        decision = decide(
            UrgencyTier.STEER,
            lease_alive=False,
            budget_dispatches_left=3,
            budget_escalations_left=3,
            estimate_stalled=True,
            partitions_allowed=True,
            budget_cost_left=0.0,
        )
        assert decision.tier == UrgencyTier.HAND_TO_USER

    def test_alive_lease_still_caps_before_cost_check(self) -> None:
        """租约活着时维持 §7 封顶语义（成本线在派工侧，不在提醒侧）。"""
        decision = decide(
            UrgencyTier.STEER,
            lease_alive=True,
            budget_dispatches_left=3,
            budget_escalations_left=1,
            estimate_stalled=False,
            budget_cost_left=0.0,
        )
        assert decision.tier == UrgencyTier.REMIND


class TestJsonSchemaAcceptsMaxCost:
    def test_contract_with_max_cost_validates(self, tmp_path: Path) -> None:
        schema = json.loads(Path("schemas/contract.schema.json").read_text(encoding="utf-8"))
        document = _json_document(max_cost=12.5)
        import jsonschema

        jsonschema.validate(document, schema)

    def test_negative_max_cost_fails_schema(self, tmp_path: Path) -> None:
        schema = json.loads(Path("schemas/contract.schema.json").read_text(encoding="utf-8"))
        document = _json_document(max_cost=-1.0)
        import jsonschema

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)


def _draft_dict(max_cost: Any) -> dict[str, Any]:
    budget: dict[str, Any] = {
        "max_dispatches": 3,
        "max_escalations": 1,
        "max_concurrent_attempts": 1,
        "max_attempt_minutes": 30,
        "max_output_bytes": 1024,
    }
    if max_cost is not None or "sentinel" == "sentinel":
        # None 也要能过「显式传了 None」的用例：区分缺席与显式 null
        budget["max_cost"] = max_cost
    return {
        "title": "t",
        "objective": "o",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        "acceptance": {"standard": "s", "checks": ["c1"], "verifier": "cross_check"},
        "workload_estimate": {"initial_hours": 1.0},
        "budget": budget,
    }


def _json_document(max_cost: Any) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "title": "t",
        "objective": "o",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        "acceptance": {"standard": "s", "checks": [{"kind": "file-exists", "target": "x"}]},
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 3,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1024,
            "max_cost": max_cost,
        },
        "authority": {"executor_policy": "closed"},
        "attention": {"notify_on": []},
        "continuity": {},
        "auto_approve": {},
    }
