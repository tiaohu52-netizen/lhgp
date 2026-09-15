"""P6: User evaluation and post-acceptance diff feedback loop.

A contract's verdict tells you whether the machine checks passed. It does not
tell you whether the user is *satisfied* with the result. This module
captures the human layer:

  - :class:`UserEvaluation` — explicit accept / partial / reject + 1-5 rating
    + free-form comments, recorded after a contract reaches ``satisfied``,
    ``failed``, or ``cancelled``.
  - :func:`compute_acceptance_diff` — text/file diff between the
    attempt's reported artifacts and the final workspace state, used as an
    improvement signal for :mod:`lhgp.learning`.

The schema (v3) stores these in dedicated tables (``user_evaluations`` and
``acceptance_diffs``) so the planner can :func:`extract_template_signals`
quickly without scanning the entire event log.
"""

from lhgp.feedback.diff import compute_acceptance_diff
from lhgp.feedback.store import (
    get_latest_diff,
    list_diffs,
    list_evaluations,
    record_diff,
    record_evaluation,
)
from lhgp.feedback.types import (
    AcceptanceDiff,
    EvaluationRating,
    EvaluationVerdict,
    UserEvaluation,
)

__all__ = [
    "AcceptanceDiff",
    "EvaluationRating",
    "EvaluationVerdict",
    "UserEvaluation",
    "compute_acceptance_diff",
    "get_latest_diff",
    "list_diffs",
    "list_evaluations",
    "record_diff",
    "record_evaluation",
]
