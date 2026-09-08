"""User pre-authorised scope for plan auto-approval.

3rd-round review (2026-09-08): the user can declare a scope at
contract-prep time that lets the runner auto-approve plans,
retries, and stage advances without an explicit sign-off — as
long as the proposed action stays inside the scope.  Anything
outside the scope falls back to the existing
``tool_submit_plan`` / ``lhgp_plan_signoff`` flow with a human
in the loop.

Three things the scope locks down:

- ``actions``: which ``ALLOWED_ACTIONS`` tokens the plan is
  allowed to use.  Empty list = no auto-approve (always require
  sign-off).  All-listed = the runner can approve any plan whose
  every step's action is in the set.
- ``max_budget_increment``: how many times the plan may
  declare a higher ``max_dispatches`` than the contract
  declares without going to the user.  ``0`` is the strict
  default — plans that ask for more budget always need sign-off.
- ``max_spec_changes``: how many times the plan may add new
  acceptance checks without sign-off.  ``0`` is the strict
  default.

The ``enabled`` flag is a kill switch: a contract author can
declare the scope fields but leave ``enabled=False`` to keep the
old sign-off-required behaviour while the rest of the team
experiments with the auto-approve format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AutoApprove:
    """Pre-authorised scope for plan auto-approval.

    The default-constructed instance (``AutoApprove()``) is the
    safe baseline: no actions, no budget increment, no spec
    changes, ``enabled=False``.  A contract that wants the
    runner to auto-approve must opt in explicitly.
    """

    enabled: bool = False
    actions: tuple[str, ...] = ()
    max_budget_increment: int = 0
    max_spec_changes: int = 0

    def covers_action(self, action: str) -> bool:
        return action in self.actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "actions": list(self.actions),
            "max_budget_increment": self.max_budget_increment,
            "max_spec_changes": self.max_spec_changes,
        }


def from_dict(data: dict[str, Any] | None) -> AutoApprove:
    if not data:
        return AutoApprove()
    actions_raw = data.get("actions") or ()
    if not isinstance(actions_raw, (list, tuple)):
        raise TypeError("auto_approve.actions must be a list of action strings")
    actions = tuple(str(a) for a in actions_raw)
    return AutoApprove(
        enabled=bool(data.get("enabled", False)),
        actions=actions,
        max_budget_increment=int(data.get("max_budget_increment", 0) or 0),
        max_spec_changes=int(data.get("max_spec_changes", 0) or 0),
    )


__all__ = ["AutoApprove", "from_dict"]
