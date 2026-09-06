"""P6: deadline enforcement with multi-level escalation.

The Reliability v1 milestone gave us a forecast model. This module
makes the deadline itself *binding* in the daemon tick:

  - :class:`DeadlineLevel` — discrete escalation tiers (T-30, T-15,
    T-5, breached) chosen by elapsed-time ratio.
  - :class:`DeadlineEnforcer` — applies each level's effects to a
    contract: re-publish, status change, lock new attempts, push
    notifications.
  - :func:`format_deadline_report` — human-readable breakdown of
    every contract crossing a threshold this tick.

The enforcer is a pure function over a contract view + the current
time; it does not start subprocesses or read files. The daemon tick
is the caller and applies the effects.
"""

from lhgp.enforcement.enforcer import DeadlineEnforcer, EnforcementAction
from lhgp.enforcement.levels import DeadlineLevel, compute_deadline_level
from lhgp.enforcement.report import format_deadline_report, render_text

__all__ = [
    "DeadlineEnforcer",
    "DeadlineLevel",
    "EnforcementAction",
    "compute_deadline_level",
    "format_deadline_report",
    "render_text",
]
