"""P6: extract signals from past contracts and evolve templates.

The :mod:`lhgp.learning` module closes the feedback loop:

  1. :func:`extract_template_signals` reads ``user_evaluations`` and
     ``acceptance_diffs``, scores each finished contract, and surfaces
     patterns (high-rated, low-diff, broad acceptance vocabulary).
  2. :class:`TemplateEvolver` materialises a high-quality contract as a
     new entry under ``templates/`` so future ``prepare`` calls can ``use``
     it. The user explicitly chose "auto_evolve" — high-rated
     contracts become new templates without human gating.
  3. :func:`suggest_draft_improvements` is a non-destructive counterpart
     that returns a list of advisory changes the user can apply.

This module is read-mostly on the contract side, write-only on
template generation. It never mutates contracts or events.
"""

from lhgp.learning.evolver import TemplateEvolver, auto_evolve
from lhgp.learning.extractor import (
    extract_template_signals,
    score_contract_quality,
    suggest_draft_improvements,
)
from lhgp.learning.types import (
    DraftSuggestion,
    QualityScore,
    TemplateSignal,
)

__all__ = [
    "DraftSuggestion",
    "QualityScore",
    "TemplateEvolver",
    "TemplateSignal",
    "auto_evolve",
    "extract_template_signals",
    "score_contract_quality",
    "suggest_draft_improvements",
]
