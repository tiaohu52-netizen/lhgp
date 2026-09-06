"""Legacy facade: re-exports from canonical lhgp.enforcement namespace."""

from lhgp.enforcement import *  # noqa: F403
from lhgp.enforcement import (  # noqa: F401
    DeadlineEnforcer,
    DeadlineLevel,
    EnforcementAction,
    compute_deadline_level,
    format_deadline_report,
    render_text,
)
