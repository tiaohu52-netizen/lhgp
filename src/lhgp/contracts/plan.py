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

    @property
    def content_hash(self) -> str:
        """Stable hash over the plan's substantive fields.

        Used by the gate enforcer to reject stale ``PLAN_APPROVED``
        events whose plan content has been edited (e.g., a step was
        inserted or removed) after approval. The hash covers
        step_id, action, target, rationale, expected_outcome, in
        order; ``contract_id`` and ``submitted_at`` are intentionally
        excluded so the same plan content submitted twice yields the
        same hash.
        """
        import hashlib

        h = hashlib.sha256()
        for step in self.steps:
            h.update(str(step.step_id).encode("utf-8"))
            h.update(b"\x00")
            h.update(step.action.encode("utf-8"))
            h.update(b"\x00")
            h.update(step.target.encode("utf-8"))
            h.update(b"\x00")
            h.update(step.rationale.encode("utf-8"))
            h.update(b"\x00")
            h.update(step.expected_outcome.encode("utf-8"))
            h.update(b"\x01")
        return h.hexdigest()

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
        # purpose; every step's rationale must connect to it (at least one
        # of the candidate keywords).  ``_pick_keyword`` returns a list so
        # CJK objectives work without requiring the whole objective to be
        # embedded in every rationale.
        keywords = _pick_keyword(contract.draft.objective)
        if not keywords:
            # Empty objective already rejected by draft validation, but be
            # defensive: a plan cannot be "aligned" with no objective.
            reasons.append("contract objective has no usable keyword")
        else:
            for index, step in enumerate(self.steps, start=1):
                if not _contains_any_keyword(step.rationale, keywords):
                    preview = ", ".join(repr(k) for k in keywords[:3])
                    reasons.append(
                        f"step {index}: rationale must mention one of objective "
                        f"keywords [{preview}, ...] (case-insensitive)"
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
        # If the plan structurally passes the validator but every
        # step's action is outside the contract's auto-approve
        # scope, mark it for explicit sign-off.  The runner cannot
        # dispatch against it until the user calls
        # ``lhgp_plan_signoff`` (or the call is via the MCP
        # tool that already has sign-off baked in).
        requires_signoff = False
        if approved and not contract.draft.auto_approve.enabled:
            requires_signoff = True
        elif approved:
            for step in self.steps:
                if not contract.draft.auto_approve.covers_action(step.action):
                    requires_signoff = True
                    break
        return PlanValidation(
            approved=approved,
            rejection_reasons=tuple(reasons),
            requires_signoff=requires_signoff,
        )


@dataclass(frozen=True, slots=True)
class PlanValidation:
    """Outcome of :meth:`Plan.validate`."""

    approved: bool
    rejection_reasons: tuple[str, ...]
    # True when the plan structurally passes the validator but
    # is outside the contract's auto-approve scope — the user
    # must sign off via ``lhgp_plan_signoff`` before the runner
    # can dispatch against it.  Default False: a rejected plan
    # is rejected on its merits, not because it needs sign-off.
    requires_signoff: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "rejection_reasons": list(self.rejection_reasons),
            "requires_signoff": self.requires_signoff,
        }


def _pick_keyword(objective: str) -> list[str]:
    """Return a list of candidate keywords from ``objective``.

    The validator checks that each step's rationale contains at least one
    of these.  Returning a list (not a single string) avoids the trap
    that ``"修复登录错误并补充测试".split()`` yields the whole CJK
    string as one token, which would then require every rationale to
    contain the entire objective verbatim.

    Strategy:
    - Latin tokens: emit each non-stop, length >= 3 word.
    - CJK characters: emit each unique CJK ideograph that survives
      filtering (stop-word CJK tokens are filtered as full tokens only).
    - Mixed: collect both, dedup, return.
    """
    keywords: list[str] = []
    seen: set[str] = set()
    cjk_chars: list[str] = []
    cjk_seen: set[str] = set()

    for raw in objective.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in _STOP_WORDS:
            continue
        if _is_cjk(cleaned):
            # CJK: keep single ideographs (each is a real semantic unit).
            for ch in cleaned:
                if not _is_cjk(ch):
                    continue
                if ch in cjk_seen:
                    continue
                cjk_seen.add(ch)
                cjk_chars.append(ch)
            continue
        if len(lowered) < 3:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        keywords.append(lowered)

    # CJK keywords (chars) first when present, since the reviewer noted
    # CJK objectives are the common case where the single-keyword heuristic
    # failed completely.
    return cjk_chars + keywords


def _contains_any_keyword(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


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
