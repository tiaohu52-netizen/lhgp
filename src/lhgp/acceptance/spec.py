"""Declarative acceptance specification: composable, heterogeneous judging.

The root problem: "is this done?" is a human judgment, not a machine one.
Current typed-checks automate the machine-judgeable subset but have no way
to compose them with agent-judged or user-judged criteria into a single
verdict.

This module introduces the **acceptance spec**: a declarative JSON structure
where each criterion declares WHO judges it (machine / agent / user) and
the full spec composes into a single verdict via boolean logic.

Structure:
    {
      "all": [                    // AND-composition
        {"judge": "machine", "kind": "file-exists", "target": "report.md"},
        {"judge": "machine", "kind": "command-exit-zero", "target": "pytest"},
        {"judge": "agent", "prompt": "Is the report clear and actionable?", "min_score": 0.7},
        {"any": [                  // OR-composition (sub-group)
          {"judge": "user"},
          {"judge": "agent", "prompt": "Is it acceptable?", "min_score": 0.9}
        ]}
      ]
    }

Judges:
- "machine": deterministic typed checks (15 kinds including file-exists,
  command-exit-zero, output-contains, no-forbidden, json-path-equals, etc.)
- "user": requires explicit user confirmation (cannot be automated away)

Note: "agent" judge was removed. LLM-judged acceptance is non-deterministic
(different runs produce different verdicts), gameable via prompt injection,
and produces pseudo-precision (0.7 vs 0.8 scores are meaningless). If a
criterion can't be expressed as machine or user, the check design isn't
finished — expand the machine vocabulary instead.

This makes the acceptance boundary explicit: which parts are automated,
which are delegated, and which require the user. The user can see and
modify this before approving.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

VALID_JUDGES = ("machine", "user")
VALID_COMBINATORS = ("all", "any")


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of evaluating one criterion."""

    judge: str
    kind: str  # "file-exists", "agent-eval", "user-confirm", etc.
    target: str
    outcome: str  # "pass" | "fail" | "pending" | "not_applicable"
    detail: str = ""
    source: str = ""  # who/what produced this result


@dataclass(frozen=True, slots=True)
class SpecVerdict:
    """Overall verdict from evaluating an acceptance spec."""

    outcome: str  # "pass" | "fail" | "pending" | "partial"
    results: list[CheckResult]
    summary: str
    machine_pass: int = 0
    machine_fail: int = 0
    agent_pending: int = 0
    user_pending: int = 0


def validate_spec(spec: Any) -> list[str]:
    """Validate an acceptance spec structure. Returns list of errors."""
    errors: list[str] = []
    _validate_node(spec, errors, path="spec")
    return errors


def _validate_node(node: Any, errors: list[str], *, path: str) -> None:
    if not isinstance(node, dict):
        errors.append(f"{path}: must be an object")
        return

    # Combinator node: {"all": [...]} or {"any": [...]}
    has_combinator = False
    for comb in VALID_COMBINATORS:
        if comb in node:
            has_combinator = True
            children = node[comb]
            if not isinstance(children, list):
                errors.append(f"{path}.{comb}: must be an array")
                continue
            if not children:
                errors.append(f"{path}.{comb}: must not be empty")
            for i, child in enumerate(children):
                _validate_node(child, errors, path=f"{path}.{comb}[{i}]")

    if has_combinator:
        # Combinator nodes should not also be leaf nodes
        for key in node:
            if key not in VALID_COMBINATORS:
                errors.append(f"{path}: combinator node has unexpected key {key!r}")
        return

    # Leaf node: {"judge": "...", ...}
    judge = node.get("judge")
    if judge not in VALID_JUDGES:
        errors.append(f"{path}.judge: must be one of {VALID_JUDGES}, got {judge!r}")
        return

    if judge == "machine":
        kind = node.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            errors.append(f"{path}.kind: required for machine judge")
        target = node.get("target")
        if not isinstance(target, str) or not target.strip():
            errors.append(f"{path}.target: required for machine judge")

    elif judge == "agent":
        prompt = node.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{path}.prompt: required for agent judge")
        min_score = node.get("min_score")
        if min_score is not None and (
            not isinstance(min_score, (int, float)) or not 0 <= min_score <= 1
        ):
            errors.append(f"{path}.min_score: must be a number in [0,1]")

    elif judge == "user":
        question = node.get("question")
        if not isinstance(question, str) or not question.strip():
            # Default question is OK, just warn
            pass


def evaluate_machine_checks(
    spec: dict[str, Any],
    *,
    check_results: dict[str, str],
) -> list[CheckResult]:
    """Evaluate all machine-judge criteria in a spec against provided results.

    Args:
        spec: The acceptance spec.
        check_results: Map from check identity (kind:target) to outcome string.

    Returns:
        List of CheckResult for machine criteria only.
    """
    results: list[CheckResult] = []
    _collect_machine(spec, check_results, results, path="spec")
    return results


def _collect_machine(
    node: Any,
    check_results: dict[str, str],
    results: list[CheckResult],
    *,
    path: str,
) -> None:
    if not isinstance(node, dict):
        return
    for comb in VALID_COMBINATORS:
        if comb in node and isinstance(node[comb], list):
            for child in node[comb]:
                _collect_machine(child, check_results, results, path=path)
            return

    if node.get("judge") != "machine":
        return
    kind = node.get("kind", "unknown")
    target = node.get("target", "")
    identity = f"{kind}:{target}"
    outcome = check_results.get(identity, "pending")
    results.append(
        CheckResult(
            judge="machine",
            kind=kind,
            target=target,
            outcome=outcome,
            detail=f"check {identity} returned {outcome}",
            source="typed-check",
        )
    )


def compose_verdict(
    spec: dict[str, Any],
    *,
    machine_results: list[CheckResult],
) -> SpecVerdict:
    """Compose all check results into a single verdict.

    Machine results are provided; agent and user criteria are counted as
    pending (they require asynchronous evaluation).
    """
    machine_map = {f"{r.kind}:{r.target}": r for r in machine_results}
    counters = {"machine_pass": 0, "machine_fail": 0, "user_pending": 0}
    all_results: list[CheckResult] = list(machine_results)

    def eval_node(node: Any) -> str:
        if not isinstance(node, dict):
            return "pending"
        for comb in VALID_COMBINATORS:
            if comb in node and isinstance(node[comb], list):
                child_outcomes = [eval_node(c) for c in node[comb]]
                if comb == "all":
                    if any(o == "fail" for o in child_outcomes):
                        return "fail"
                    if any(o == "pending" for o in child_outcomes):
                        return "pending"
                    return "pass"
                else:  # any
                    if any(o == "pass" for o in child_outcomes):
                        return "pass"
                    if any(o == "pending" for o in child_outcomes):
                        return "pending"
                    return "fail"

        judge = node.get("judge")
        if judge == "machine":
            identity = f"{node.get('kind', '')}:{node.get('target', '')}"
            result = machine_map.get(identity)
            if result is None:
                return "pending"
            if result.outcome == "pass":
                counters["machine_pass"] += 1
            elif result.outcome == "fail":
                counters["machine_fail"] += 1
            return result.outcome
        # agent judge removed: non-deterministic, gameable, pseudo-precise.
        # If a criterion can't be expressed as machine or user, the spec
        # isn't finished being designed yet.
        elif judge == "user":
            counters["user_pending"] += 1
            all_results.append(
                CheckResult(
                    judge="user",
                    kind="user-confirm",
                    target=node.get("question", "user confirmation required"),
                    outcome="pending",
                    detail="requires user",
                    source="user",
                )
            )
            return "pending"
        return "pending"

    overall = eval_node(spec)
    summary_parts = []
    if counters["machine_pass"]:
        summary_parts.append(f"machine: {counters['machine_pass']} pass")
    if counters["machine_fail"]:
        summary_parts.append(f"machine: {counters['machine_fail']} fail")
    if counters["user_pending"]:
        summary_parts.append(f"user: {counters['user_pending']} pending")

    return SpecVerdict(
        outcome=overall,
        results=all_results,
        summary="; ".join(summary_parts) or "no criteria",
        machine_pass=counters["machine_pass"],
        machine_fail=counters["machine_fail"],
        user_pending=counters["user_pending"],
    )


def spec_to_dict(spec: dict[str, Any]) -> str:
    """Serialize spec to JSON for storage in contract acceptance field."""
    import json

    return json.dumps(spec, ensure_ascii=False, indent=2)


def spec_from_dict(raw: str | dict[str, Any]) -> dict[str, Any] | None:
    """Parse a spec from JSON string or dict. Returns None if invalid."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "VALID_JUDGES",
    "CheckResult",
    "SpecVerdict",
    "compose_verdict",
    "evaluate_machine_checks",
    "spec_from_dict",
    "spec_to_dict",
    "validate_spec",
]
