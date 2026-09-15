"""E3 提案校验规则（纯函数，可测试）。

模型的计划提案在落 goal/proposed 事件之前必须通过结构校验；
用户 apply 时也用同一套校验，确保「提案的内容」和「应用的计划」
结构一致。校验规则来自 ADR-004 规则 6 + Goal 计划 schema。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 一个计划的最大 stage 数：超出说明模型在灌垃圾而非提计划
MAX_STAGES = 20
# 单个 stage 的最大字段数（id + contract_id + checks + 少量 metadata）
MAX_STAGE_FIELDS = 10


@dataclass(frozen=True, slots=True)
class ProposalValidation:
    """提案校验结果：errors 为空则通过。"""

    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_proposed_plan(plan: Any) -> ProposalValidation:
    """校验模型提案的 Goal 计划结构。

    规则（按 ADR-004 规则 6 + Goal 计划 schema）：
    1. plan 必须是 dict；
    2. plan.stages 必须是 list，每个元素是 dict；
    3. 每个 stage 必须有非空 id（字符串）；
    4. stage.contract_id 如有，必须是合法安全 slug（不含路径字符）；
    5. stages 数量不超过 MAX_STAGES；
    6. 不允许包含执行权限字段（executor/model/budget）——
       这些是合同冻结区内容，计划提案不得染指。
    """
    errors: list[str] = []

    if not isinstance(plan, dict):
        return ProposalValidation(["plan must be an object"])

    stages = plan.get("stages")
    if not isinstance(stages, list):
        return ProposalValidation(["plan.stages must be an array"])

    if len(stages) > MAX_STAGES:
        errors.append(f"plan.stages has {len(stages)} stages; max is {MAX_STAGES}")

    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            errors.append(f"stage[{i}] must be an object")
            continue

        if len(stage) > MAX_STAGE_FIELDS:
            errors.append(f"stage[{i}] has {len(stage)} fields; max is {MAX_STAGE_FIELDS}")

        stage_id = stage.get("id")
        if not isinstance(stage_id, str) or not stage_id.strip():
            errors.append(f"stage[{i}].id must be a non-empty string")

        contract_id = stage.get("contract_id")
        if contract_id is not None:
            if not isinstance(contract_id, str) or not contract_id.strip():
                errors.append(f"stage[{i}].contract_id must be a non-empty string")
            elif any(c in contract_id for c in ("/", "\\", "..")):
                errors.append(f"stage[{i}].contract_id contains path characters: {contract_id!r}")

        # 计划提案不得携带执行权限字段（ADR-004 规则 6 的核心约束）
        for forbidden in ("executor", "model", "budget", "authority"):
            if forbidden in stage:
                errors.append(
                    f"stage[{i}].{forbidden} is forbidden in plan proposals; "
                    "these are contract frozen-zone fields"
                )

    return ProposalValidation(errors)


__all__ = ["MAX_STAGES", "MAX_STAGE_FIELDS", "ProposalValidation", "validate_proposed_plan"]
