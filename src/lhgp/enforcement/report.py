"""P6: human-readable deadline report.

The daemon tick calls :func:`format_deadline_report` after enforcer
runs, to feed the operator-visible ``tick`` log + the
``notification_outbox`` ``notify/dispatched`` payload.
"""

from __future__ import annotations

from typing import Any

from lhgp.enforcement.enforcer import EnforcementAction
from lhgp.enforcement.levels import DeadlineLevel

_LEVEL_LABEL = {
    DeadlineLevel.NORMAL: "ok",
    DeadlineLevel.WARNING: "warn",
    DeadlineLevel.URGENT: "urgent",
    DeadlineLevel.BREACHED: "BREACHED",
}


def format_deadline_report(actions: list[EnforcementAction]) -> dict[str, Any]:
    """Aggregate a tick's enforcer output into a per-bucket report."""
    by_level: dict[str, list[EnforcementAction]] = {
        "warning": [],
        "urgent": [],
        "breached": [],
    }
    for act in actions:
        if act.level == DeadlineLevel.WARNING:
            by_level["warning"].append(act)
        elif act.level == DeadlineLevel.URGENT:
            by_level["urgent"].append(act)
        elif act.level == DeadlineLevel.BREACHED:
            by_level["breached"].append(act)

    def _short(a: EnforcementAction) -> dict[str, Any]:
        return {
            "contract_id": a.contract_id,
            "actions": list(a.actions),
            "rationale": a.rationale,
            "remaining_seconds": a.metadata.get("remaining_seconds"),
            "elapsed_ratio": a.metadata.get("elapsed_ratio"),
        }

    return {
        "totals": {k: len(v) for k, v in by_level.items()},
        "warning": [_short(a) for a in by_level["warning"]],
        "urgent": [_short(a) for a in by_level["urgent"]],
        "breached": [_short(a) for a in by_level["breached"]],
    }


def render_text(actions: list[EnforcementAction]) -> str:
    """One-line-per-contract text for the tick log."""
    if not actions:
        return "deadline: no escalations this tick"
    parts: list[str] = []
    for act in actions:
        label = _LEVEL_LABEL.get(act.level, act.level.value)
        remaining = act.metadata.get("remaining_seconds")
        parts.append(
            f"{label:>8s} {act.contract_id} -> {','.join(act.actions) or '(no-op)'} "
            f"(rem={remaining:.0f}s)"
            if remaining is not None
            else f"{label:>8s} {act.contract_id}"
        )
    return "deadline tick:\n  " + "\n  ".join(parts)
