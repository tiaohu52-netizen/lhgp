"""Verifier stdout verdict block parsing and evidence merge (SPEC §12.4).

一次性 CLI verifier 无法调用 ``attempt/write-back`` RPC——它的验收结论
只出现在 stdout。本模块把约定的 ``lhgp-verdict`` 判定块解析为结构化
证据，并与协议确定性评估合成最终裁决。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

VERDICT_MARKER = "lhgp-verdict"
_VALID_OUTCOMES = frozenset({"pass", "fail", "undetermined"})

# ```lhgp-verdict ... ``` 围栏块；容忍围栏前有空白与语言标记变体
_BLOCK_RE = re.compile(
    r"```" + VERDICT_MARKER + r"\s*\n(.*?)```",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ModelVerdict:
    """Parsed verdict block from one verifier run."""

    verdict: str  # succeeded | failed
    checks: dict[str, dict[str, str]]  # check_id -> {outcome, source, details?}

    def outcome_for(self, check_id: str) -> str | None:
        entry = self.checks.get(check_id)
        if entry is None:
            return None
        outcome = entry.get("outcome")
        return outcome if outcome in _VALID_OUTCOMES else None


class VerdictSourceLossError(Exception):
    """verifier 判定块随输出预算截断丢失——与「verifier 没写」可区分（SPEC §12.4）。

    两者下游同为 undetermined / 人工仲裁，但审计含义不同：截断丢失是**部署
    侧可修的**（调大 budget.max_output_bytes 重跑即可拿回证据），「没写」是
    verifier 侧的问题。混在一起会让「明明跑成功了却总进人工仲裁」无从排查。
    """


def verdict_from_output(stdout: str, *, output_truncated: bool) -> ModelVerdict | None:
    """带截断事实的判定块解析（SPEC §12.4 通道 2 的完整语义）。

    判定块约定在 stdout **末尾**，而输出预算保留的是**头部**：截断一旦发生，
    判定块即使写了也必然丢失。此时：

    - 输出被截断**且**没有解析出判定块 → 抛 :class:`VerdictSourceLossError`——
      不能静默返回 None。返回 None 的语义是「verifier 没写判定块」，而这里
      的真实情况是「写了但被我们自己的预算截掉了」，责任方不同、修法不同。
    - 输出被截断但判定块仍解析出来了（围栏在截断点之前）→ 正常返回；
      判定块不依赖被截掉的部分。
    - 未截断 → 与 :func:`parse_verdict_block` 完全一致。
    """
    parsed = parse_verdict_block(stdout)
    if parsed is None and output_truncated:
        raise VerdictSourceLossError(
            "verifier output was truncated by budget.max_output_bytes before the "
            "lhgp-verdict block could be captured; the verdict cannot be trusted "
            "as absent — raise the budget and re-run this verification"
        )
    return parsed


def parse_verdict_block(stdout: str) -> ModelVerdict | None:
    """Extract the last valid ``lhgp-verdict`` block from verifier stdout.

    缺失、非法 JSON、形状不对 → None（无证据，绝不猜测兜底）。
    多个块时取最后一个——verifier 的最终结论在其输出末尾。
    """
    if not stdout:
        return None
    matches = _BLOCK_RE.findall(stdout)
    for raw in reversed(matches):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in ("succeeded", "failed"):
            continue
        raw_checks = data.get("checks")
        if not isinstance(raw_checks, list) or not raw_checks:
            continue
        checks: dict[str, dict[str, str]] = {}
        for item in raw_checks:
            if not isinstance(item, dict):
                continue
            check_id = str(item.get("check_id", "")).strip()
            outcome = str(item.get("outcome", "")).strip().lower()
            if not check_id or outcome not in _VALID_OUTCOMES:
                continue
            checks[check_id] = {
                "outcome": outcome,
                "source": str(item.get("source", "")),
                "details": str(item.get("details", "")),
            }
        if checks:
            return ModelVerdict(verdict=verdict, checks=checks)
    return None


def merge_evidence(
    protocol_evidence: dict[str, str],
    model_verdict: ModelVerdict | None,
) -> dict[str, Any]:
    """Merge one deterministic result with model observation (SPEC §12.4).

    - 协议 pass/fail（确定性）优先，模型观察不得覆盖（防橡皮图章）；
      冲突记录进 ``model_outcome`` 供审计。
    - 协议 undetermined 时模型显式 pass/fail 填补裁决。
    - 双方均无显式结果 → undetermined。
    """
    merged = dict(protocol_evidence)
    check_id = str(merged.get("check_id", ""))
    model_outcome = model_verdict.outcome_for(check_id) if model_verdict else None
    merged["model_outcome"] = model_outcome or "absent"
    protocol_outcome = str(merged.get("outcome", "undetermined"))
    if protocol_outcome in ("pass", "fail"):
        return merged  # 确定性结果优先
    if model_outcome in ("pass", "fail"):
        entry = model_verdict.checks[check_id] if model_verdict else {}
        merged["outcome"] = model_outcome
        if entry.get("source") and not merged.get("source"):
            merged["source"] = entry["source"]
        if entry.get("details"):
            merged["details"] = f"{merged.get('details', '')} | model: {entry['details']}".strip(
                " |"
            )
    return merged


__all__ = [
    "VERDICT_MARKER",
    "ModelVerdict",
    "VerdictSourceLossError",
    "merge_evidence",
    "parse_verdict_block",
    "verdict_from_output",
]
