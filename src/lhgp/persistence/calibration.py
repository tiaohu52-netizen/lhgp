"""Acceptance calibration: learn which checks predict user satisfaction.

The gap between "machine says pass" and "user says good" is the hardest
problem in automated acceptance.  This module doesn't solve it — it
**measures** it, so check design improves over time.

Three signals:
- verifier pass + user accept → check is a good predictor
- verifier pass + user reject → check missed something (false positive)
- verifier fail + user override-accept → check was too strict (false negative)

Over time, the calibration data tells you which check *kinds* and *targets*
are reliable predictors and which need tightening or loosening.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class CalibrationEntry:
    """One calibration observation."""

    contract_id: str
    check_identity: str
    check_kind: str
    check_target: str
    verifier_outcome: str  # pass | fail | undetermined
    user_outcome: str  # accepted | rejected | modified | pending


def record_acceptance_outcome(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    goal_id: str,
    check_results: list[dict[str, Any]],
    user_action: str,
    now: datetime,
) -> int:
    """Record a calibration observation after a completed contract.

    Called when a contract reaches a terminal state with user involvement.
    ``user_action`` is the user's final judgment: "accepted" or "rejected".
    ``check_results`` is the verifier's evidence list with per-check outcomes.

    Returns the event_id of the calibration record.
    """
    from lhgp.persistence.events import EventType
    from lhgp.persistence.events_query import append_event

    calibrated = [
        {
            "check_id": c.get("check_id", ""),
            "kind": c.get("kind", "unknown"),
            "target": c.get("target", ""),
            "verifier_outcome": c.get("outcome", "undetermined"),
            "user_confirmed": user_action == "accepted",
        }
        for c in check_results
        if isinstance(c, dict)
    ]
    event = append_event(
        conn,
        contract_id=contract_id,
        goal_id=goal_id,
        event_type=EventType.ACCEPTANCE_CALIBRATED,
        payload={
            "user_action": user_action,
            "check_results": calibrated,
            "calibrated_at": now.isoformat(),
        },
        now=now,
        actor="user",
    )
    return event.event_id


def calibration_summary(
    conn: sqlite3.Connection,
    *,
    contract_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate calibration data: which check kinds predict user satisfaction.

    Returns per-kind and per-target reliability metrics:
    - true_positive_rate: verifier pass AND user accept / verifier pass total
    - false_positive_rate: verifier pass BUT user reject / verifier pass total
    - sample_count: number of calibration observations
    """
    base_query = "SELECT payload_json FROM events WHERE event_type = 'acceptance/calibrated'"
    params: list[Any] = []
    if contract_id:
        base_query += " AND contract_id = ?"
        params.append(contract_id)

    rows = conn.execute(base_query, params).fetchall()

    kind_stats: dict[str, dict[str, Any]] = {}
    total_verifier_pass = 0
    total_user_accepted = 0

    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except ValueError:
            continue
        user_action = payload.get("user_action", "pending")
        for check in payload.get("check_results", []):
            kind = check.get("kind", "unknown")
            outcome = check.get("verifier_outcome", "undetermined")

            if outcome != "pass":
                continue
            total_verifier_pass += 1

            if kind not in kind_stats:
                kind_stats[kind] = {"pass": 0, "user_accepted": 0, "user_rejected": 0}
            kind_stats[kind]["pass"] += 1

            if user_action == "accepted":
                kind_stats[kind]["user_accepted"] = kind_stats[kind].get("user_accepted", 0) + 1
                total_user_accepted += 1
            elif user_action == "rejected":
                kind_stats[kind]["user_rejected"] = kind_stats[kind].get("user_rejected", 0) + 1

    # Compute rates
    for kind_data in kind_stats.values():
        p = kind_data.get("pass", 0)
        kind_data["true_positive_rate"] = round(kind_data["user_accepted"] / p, 2) if p else None
        kind_data["false_positive_rate"] = round(kind_data["user_rejected"] / p, 2) if p else None

    return {
        "total_observations": len(rows),
        "total_verifier_pass": total_verifier_pass,
        "total_user_accepted": total_user_accepted,
        "by_kind": kind_stats,
    }


__all__ = [
    "CalibrationEntry",
    "calibration_summary",
    "record_acceptance_outcome",
]
