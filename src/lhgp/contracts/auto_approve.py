"""User pre-authorised scope for plan auto-approval."""

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

    def inherit_from(self, previous: AutoApprove) -> AutoApprove:
        """Return a new AutoApprove inheriting from a previous stage's scope.

        Used by stage synthesis so the next-stage contract carries
        forward the user's pre-authorised action scope.  If the
        current (self) is already enabled, return self unchanged —
        the user explicitly set it on the synthesised draft.  Else
        adopt the previous's settings.  The previous's settings are
        copied verbatim, not merged, so the user's submission-time
        authorisation flows cleanly to the next stage.
        """
        if self.enabled:
            return self
        if not previous.enabled:
            return self
        return AutoApprove(
            enabled=True,
            actions=previous.actions,
            max_budget_increment=previous.max_budget_increment,
            max_spec_changes=previous.max_spec_changes,
        )

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
