"""P6 learning loop data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class QualityScore:
    """Single contract's quality score, in [0.0, 1.0].

    Weighted by user rating (60%), machine acceptance result (25%),
    and diff size relative to workspace (15%, smaller-is-better).
    """

    contract_id: str
    contract_revision: int
    user_rating: float  # 1..5 normalised to 0..1
    acceptance_pass_rate: float  # 0..1
    diff_efficiency: float  # 1.0 = no diff, 0.0 = many spurious changes
    overall: float  # weighted sum

    @classmethod
    def from_components(
        cls,
        contract_id: str,
        contract_revision: int,
        user_rating: float,
        acceptance_pass_rate: float,
        diff_efficiency: float,
    ) -> QualityScore:
        overall = max(
            0.0,
            min(
                1.0,
                user_rating * 0.6 + acceptance_pass_rate * 0.25 + diff_efficiency * 0.15,
            ),
        )
        return cls(
            contract_id=contract_id,
            contract_revision=contract_revision,
            user_rating=user_rating,
            acceptance_pass_rate=acceptance_pass_rate,
            diff_efficiency=diff_efficiency,
            overall=overall,
        )


@dataclass(frozen=True, slots=True)
class TemplateSignal:
    """A pattern observed in past contracts that is worth replicating.

    ``suggested_acceptance_vocabulary`` is the union of machine check
    kinds that high-rated contracts used; the draft tool can recommend
    these to new contracts of similar shape.
    """

    pattern_id: str
    contract_id: str
    contract_revision: int
    quality: QualityScore
    acceptance_vocabulary: tuple[str, ...] = ()
    objective_keywords: tuple[str, ...] = ()
    title_hint: str = ""
    notes: str = ""


@dataclass(frozen=True, slots=True)
class DraftSuggestion:
    """Non-destructive advisory for a draft under preparation."""

    pattern_id: str
    add_acceptance_kinds: tuple[str, ...] = ()
    tighten_deadline_hours: float | None = None
    suggest_objective_keywords: tuple[str, ...] = ()
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
