"""Legacy facade: re-exports from canonical lhgp.learning namespace."""

from lhgp.learning import *  # noqa: F403
from lhgp.learning import (  # noqa: F401
    DraftSuggestion,
    QualityScore,
    TemplateEvolver,
    TemplateSignal,
    auto_evolve,
    extract_template_signals,
    score_contract_quality,
    suggest_draft_improvements,
)
