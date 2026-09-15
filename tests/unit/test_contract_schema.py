"""合同数据模型校验（DESIGN §4、§11.6）。

冻结区切分、deadline 显式时区、预算正数、验收条款最小集。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from longtask.contracts.authority import from_dict as authority_from_dict
from longtask.contracts.continuity import from_dict as continuity_from_dict
from longtask.contracts.contract_draft import from_dict as draft_from_dict
from longtask.contracts.schema import (
    FROZEN_FIELDS,
    Acceptance,
    Budget,
    ContractDraft,
)

pytestmark = pytest.mark.unit

DEADLINE = datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)


def make_draft(**overrides: object) -> ContractDraft:
    kwargs: dict[str, object] = {
        "title": "示例合同",
        "objective": "完成某件可验收的事",
        "deadline_at": DEADLINE,
        "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        "acceptance": Acceptance(standard="标准", checks=("check-1",)),
        "workload_initial_hours": 6.0,
        "budget": Budget(
            max_dispatches=8,
            max_escalations=3,
            max_concurrent_attempts=1,
            max_attempt_minutes=90,
            max_output_bytes=1_048_576,
        ),
    }
    kwargs.update(overrides)
    return ContractDraft(**kwargs)  # type: ignore[arg-type]


def test_json_schema_accepts_typed_check_object() -> None:
    schema_path = Path(__file__).parents[2] / "schemas" / "contract.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = make_draft(
        acceptance=Acceptance(
            standard="artifact exists",
            checks=(
                {
                    "kind": "file-exists",
                    "target": "dist/app.js",
                    "mandatory": True,
                },
            ),
        )
    ).to_dict()
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []


class TestDraftValidation:
    def test_valid_draft_passes(self) -> None:
        assert make_draft().validate() == []

    def test_naive_deadline_rejected(self) -> None:
        # DESIGN §4：deadline_at 必须显式时区
        draft = make_draft(deadline_at=datetime(2026, 9, 5, 23, 59, 59))
        assert any("timezone" in e for e in draft.validate())

    def test_empty_title_rejected(self) -> None:
        assert make_draft(title="").validate() != []

    def test_nonpositive_budget_rejected(self) -> None:
        draft = make_draft(
            budget=Budget(
                max_dispatches=0,
                max_escalations=3,
                max_concurrent_attempts=1,
                max_attempt_minutes=90,
                max_output_bytes=1_048_576,
            )
        )
        assert any("max_dispatches" in e for e in draft.validate())

    def test_empty_checks_rejected(self) -> None:
        draft = make_draft(acceptance=Acceptance(standard="标准", checks=()))
        assert any("checks" in e for e in draft.validate())

    def test_unknown_verifier_rejected(self) -> None:
        draft = make_draft(acceptance=Acceptance(standard="s", checks=("c",), verifier="self"))
        assert any("verifier" in e for e in draft.validate())


class TestFrozenFields:
    def test_frozen_set_matches_design(self) -> None:
        # SPEC §4 / §6.4 冻结区：objective / deadline_at / hard_constraints / authority
        # authority 进入冻结区是 P2（§6.4 明确授权修订必须 Principal 批准）。
        # budget 进入冻结区：patch 应用面本就不含预算，显式拒绝优于静默忽略
        # （防 agent 误传/试探预算改写——branch hardening/deadline-contract-guards）。
        assert (
            frozenset({"objective", "deadline_at", "hard_constraints", "authority", "budget"})
            == FROZEN_FIELDS
        )


class TestContractSerialization:
    def test_budget_boolean_is_rejected_during_deserialization(self) -> None:
        payload = make_draft().to_dict()
        payload["budget"]["max_dispatches"] = True
        with pytest.raises(TypeError, match=r"budget\.max_dispatches must be an integer"):
            draft_from_dict(payload)

    @pytest.mark.parametrize("field", ["max_dispatches", "max_attempt_minutes"])
    def test_budget_float_is_rejected_during_deserialization(self, field: str) -> None:
        payload = make_draft().to_dict()
        payload["budget"][field] = 1.5
        with pytest.raises(TypeError, match=rf"budget\.{field} must be an integer"):
            draft_from_dict(payload)

    def test_draft_and_view_to_dict(self) -> None:
        draft = make_draft()
        d_dict = draft.to_dict()
        assert d_dict["title"] == "示例合同"
        assert d_dict["acceptance"]["standard"] == "标准"
        assert d_dict["workload_estimate"]["initial_hours"] == 6.0

        from longtask.contracts.schema import (
            AcceptanceStatus,
            ContractState,
            ContractView,
            DeadlineStatus,
        )

        now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
        view = ContractView(
            draft=draft,
            contract_id="lt-20260831-001",
            goal_id="lt-20260831-001",
            revision=1,
            state=ContractState.DRAFTED,
            deadline_status=DeadlineStatus.NOT_DUE,
            acceptance_status=AcceptanceStatus.PENDING,
            created_at=now,
            updated_at=now,
            next_wakeup_at=None,
            next_decision_at=None,
        )
        v_dict = view.to_dict()
        assert v_dict["contract_id"] == "lt-20260831-001"
        assert v_dict["state"] == "drafted"
        assert v_dict["revision"] == 1
        assert v_dict["title"] == "示例合同"
        assert v_dict["schema_version"] == 2


def test_authority_allow_parallel_rejects_non_boolean() -> None:
    with pytest.raises(TypeError, match="allow_parallel must be a boolean"):
        authority_from_dict({"allow_parallel": "false"})


def test_continuity_checkpoint_flag_rejects_non_boolean() -> None:
    with pytest.raises(TypeError, match="checkpoint_on_material_change must be a boolean"):
        continuity_from_dict({"checkpoint_on_material_change": "false"})


def test_continuity_numeric_flags_reject_boolean_values() -> None:
    with pytest.raises(TypeError, match=r"checkpoint_max_age_minutes must be an integer"):
        continuity_from_dict({"checkpoint_max_age_minutes": True})

    def test_continuity_numeric_fields_reject_float_values() -> None:
        with pytest.raises(TypeError, match=r"checkpoint_max_age_minutes must be an integer"):
            continuity_from_dict({"checkpoint_max_age_minutes": 1.5})


def test_workload_estimate_rejects_boolean_and_non_finite_values() -> None:
    payload = make_draft().to_dict()
    payload["workload_estimate"]["initial_hours"] = True
    with pytest.raises(
        TypeError, match=r"workload_estimate\.initial_hours must be a finite number"
    ):
        draft_from_dict(payload)

    payload["workload_estimate"]["initial_hours"] = float("nan")
    with pytest.raises(
        TypeError, match=r"workload_estimate\.initial_hours must be a finite number"
    ):
        draft_from_dict(payload)
