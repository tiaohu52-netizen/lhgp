"""Legacy facade: re-exports from the canonical lhgp.feedback namespace.

This file is generated for the compatibility window. New code MUST
import from :mod:`lhgp.feedback` directly. The dual-namespace rule is
documented in ARCHITECTURE.md.
"""

from lhgp.feedback import *  # noqa: F403
from lhgp.feedback import (  # noqa: F401
    AcceptanceDiff,
    EvaluationRating,
    EvaluationVerdict,
    UserEvaluation,
    compute_acceptance_diff,
    get_latest_diff,
    list_diffs,
    list_evaluations,
    record_diff,
    record_evaluation,
)
