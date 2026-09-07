"""Plan-mode gate for contract attempts (SPEC §12.x).

Before a contract attempt dispatches, the agent must submit a :class:`Plan`
listing the steps it intends to take.  :meth:`Plan.validate` checks four
things against the contract view:

1. Plan has at least one step.
2. Every step's ``action`` is in :data:`ALLOWED_ACTIONS` (the closed set of
   things an executor can do during an attempt).
3. Every step's ``rationale`` is non-empty and contains a keyword from the
   contract's ``objective`` (case-insensitive).  This forces the planner to
   connect each step to the contract's purpose, not pad generic actions.
4. The plan covers every entry in ``contract.acceptance.checks`` — at least
   one step's ``target`` or ``expected_outcome`` must mention each check's
   identifier.  No check can be silently dropped.

The runner enforces the gate by refusing ``DISPATCHING -> RUNNING`` unless
a matching ``PLAN_APPROVED`` event exists in the contract's recent event
stream (see runner; this module is the planning half only).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lhgp.contracts.contract_view_entity import ContractView

# Closed set of step kinds an executor can declare in a plan.  Adding a new
# kind is a contract change: it must be added here, in the skill description,
# and validated against the executor capability surface in the runner.
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "read file",
        "run command",
        "ask user",
        "verify acceptance",
        "write file",
        "search code",
    }
)

# Stop words excluded when picking a keyword from ``contract.objective``.
# Picking a non-stop word forces the rationale to reference something
# concrete ("complete the migration") rather than ("complete the task").
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "by",
        "is",
        "are",
        "be",
        "this",
        "that",
        "it",
        "as",
        "at",
        "from",
        "into",
        "完成",  # "complete" — too generic on its own
        "的",
        "在",
        "并",
    }
)


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One declared step in a plan.

    Attributes:
        step_id: Caller-supplied ordering hint. 1-based by convention.
        action: Must be in :data:`ALLOWED_ACTIONS`.
        target: Free-form. For ``"run command"`` this is the command line.
        rationale: Why this step serves the contract objective. Must be
            non-empty and contain a keyword from the objective.
        expected_outcome: How we'll know the step worked. Used to cover
            acceptance checks.
    """

    step_id: int
    action: str
    target: str
    rationale: str
    expected_outcome: str

    def __post_init__(self) -> None:
        # Frozen dataclass: attribute assignment goes through object.__setattr__.
        # We do not mutate state here; validation lives in Plan.validate so
        # a single Plan can be validated against multiple contracts (or none)
        # without re-constructing steps.
        return


@dataclass(frozen=True, slots=True)
class Plan:
    """A complete plan submitted by an agent before dispatching an attempt."""

    contract_id: str
    steps: tuple[PlanStep, ...]
    submitted_at: datetime
    submitted_by: str  # "agent:<evaluator_id>"

    def validate(self, contract: ContractView) -> PlanValidation:
        """Return a :class:`PlanValidation` against the given contract view.

        Validation is closed-set and deterministic: the same plan + contract
        always produces the same result. Rejection reasons are ordered and
        stable so a UI can render them without re-sorting.
        """
        reasons: list[str] = []

        if not self.steps:
            reasons.append("plan must contain at least one step")
            # No steps to inspect further; fail fast.
            return PlanValidation(approved=False, rejection_reasons=tuple(reasons))

        # Per-step structural checks.
        for index, step in enumerate(self.steps, start=1):
            if step.action not in ALLOWED_ACTIONS:
                reasons.append(
                    f"step {index}: action {step.action!r} not in allowed actions "
                    f"{sorted(ALLOWED_ACTIONS)}"
                )
            if not step.rationale or not step.rationale.strip():
                reasons.append(f"step {index}: rationale must not be empty")
            if not step.target or not step.target.strip():
                reasons.append(f"step {index}: target must not be empty")
            if not step.expected_outcome or not step.expected_outcome.strip():
                reasons.append(f"step {index}: expected_outcome must not be empty")

        # Objective keyword coverage. The objective is the contract's
        # purpose; every step's rationale must connect to it.
        keyword = _pick_keyword(contract.draft.objective)
        if keyword is None:
            # Empty objective already rejected by draft validation, but be
            # defensive: a plan cannot be "aligned" with no objective.
            reasons.append("contract objective has no usable keyword")
        else:
            for index, step in enumerate(self.steps, start=1):
                if not _contains_keyword(step.rationale, keyword):
                    reasons.append(
                        f"step {index}: rationale must mention objective keyword "
                        f"{keyword!r} (case-insensitive)"
                    )

        # Per-acceptance.check coverage. Every check must be referenced
        # by some step's target or expected_outcome.
        check_ids = list(_extract_check_identifiers(contract))
        uncovered = _uncovered_checks(self.steps, check_ids)
        for check_id in uncovered:
            reasons.append(
                f"acceptance check {check_id!r} is not covered by any step's "
                "target or expected_outcome"
            )

        approved = not reasons
        return PlanValidation(approved=approved, rejection_reasons=tuple(reasons))


@dataclass(frozen=True, slots=True)
class PlanValidation:
    """Outcome of :meth:`Plan.validate`."""

    approved: bool
    rejection_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "rejection_reasons": list(self.rejection_reasons),
        }


def _pick_keyword(objective: str) -> str | None:
    """Return the first non-stop word in ``objective`` (lowercased).

    The keyword is used as a connectivity check — every step's rationale
    must contain it.  Picking a long, specific word (vs the first token
    or the whole objective) avoids trivial matches like "the".
    """
    for raw in objective.split():
        # Strip ASCII + CJK punctuation that is rarely meaningful.
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        lowered = cleaned.lower()
        if not lowered or lowered in _STOP_WORDS:
            continue
        # Single/double-char Latin tokens are too easy to match by accident.
        # Keep single CJK characters: they are real semantic units.
        if len(lowered) < 3 and not _is_cjk(cleaned):
            continue
        return lowered
    return None


def _is_cjk(token: str) -> bool:
    """True if ``token`` contains at least one CJK ideograph."""

    for ch in token:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:  # CJK Unified Ideographs
            return True
        if 0x3400 <= code <= 0x4DBF:  # CJK Extension A
            return True
    return False


def _contains_keyword(text: str, keyword: str) -> bool:
    """Case-insensitive substring match for ``keyword`` inside ``text``."""
    return keyword in text.lower()


def _extract_check_identifiers(contract: ContractView) -> Iterable[str]:
    """Yield a stable identifier for each acceptance check.

    The identifier is what a plan step must mention to "cover" the check.
    For string checks, the full string is the identifier. For typed
    :class:`CheckSpec` checks, ``<kind>:<target>`` is used — short,
    deterministic, and matches how the verifier emits failure messages.
    """
    from lhgp.acceptance.checks import CheckSpec

    for check in contract.draft.acceptance.checks:
        if isinstance(check, CheckSpec):
            yield f"{check.kind.value}:{check.target}"
        elif isinstance(check, str):
            yield check


def _uncovered_checks(steps: Iterable[PlanStep], check_ids: list[str]) -> list[str]:
    """Return check identifiers not mentioned in any step's target or outcome.

    We concatenate ``target`` and ``expected_outcome`` per step into one
    searchable haystack per step. A check is covered if any haystack
    contains the identifier (case-insensitive substring).
    """
    if not check_ids:
        return []
    covered: set[str] = set()
    for step in steps:
        haystack = f"{step.target} {step.expected_outcome}".lower()
        for cid in check_ids:
            if cid.lower() in haystack:
                covered.add(cid)
    return [cid for cid in check_ids if cid not in covered]


__all__ = [
    "ALLOWED_ACTIONS",
    "Plan",
    "PlanStep",
    "PlanValidation",
]
