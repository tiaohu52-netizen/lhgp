"""Agent messaging layer: structured communication between user, daemon, and agents.

Three message kinds:
- directive: user tells the agent what to do differently ("skip check X", "use approach B")
- context: agent shares findings with the next agent (beyond handover.md)
- question: agent asks the user/daemon a question that blocks progress

Messages are events (auditable) that get injected into the next attempt's
context snapshot, so the working agent sees them without polling.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event, get_events

VALID_KINDS = ("directive", "context", "question", "handover")


def send_message(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    from_actor: str,
    kind: str,
    text: str,
    now: datetime,
    goal_id: str | None = None,
    to_agent: str | None = None,
) -> int:
    """Send a structured message on a contract's event stream.

    Messages are first-class events: auditable, visible to all parties,
    and injected into the next attempt's context snapshot.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid message kind {kind!r}; must be one of {VALID_KINDS}")
    event = append_event(
        conn,
        contract_id=contract_id,
        goal_id=goal_id or contract_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "from": from_actor,
            "kind": kind,
            "text": text,
            "to_agent": to_agent,
        },
        now=now,
        actor=from_actor,
    )
    return event.event_id


def get_messages(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    kind: str | None = None,
    after_event_id: int = 0,
) -> list[dict[str, Any]]:
    """Read messages for a contract, optionally filtered by kind."""
    messages = []
    for e in get_events(conn, contract_id=contract_id, after_event_id=after_event_id):
        if str(e.event_type) != EventType.AGENT_MESSAGE.value:
            continue
        import json

        try:
            payload = json.loads(e.payload_json or "{}")
        except ValueError:
            continue
        if kind and payload.get("kind") != kind:
            continue
        messages.append(
            {
                "event_id": e.event_id,
                "at": e.created_at.isoformat() if e.created_at else "",
                "from": payload.get("from", "unknown"),
                "kind": payload.get("kind", "unknown"),
                "text": payload.get("text", ""),
                "to_agent": payload.get("to_agent"),
            }
        )
    return messages


def pending_directives(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    after_event_id: int = 0,
    to_agent: str | None = None,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
    dedup_seen: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Get unread directives for the working agent.

    Directives with ``to_agent`` set are A2A-targeted: only the
    addressed agent (executor_id) sees them.  Directives with
    ``to_agent=None`` are broadcast to every agent working the
    contract.  Pass ``to_agent`` to receive only the subset the
    caller should actually consume; pass None to receive the
    broadcast slice only (the common case for a user → all-agents
    blast).

    A2A delivery hardening (3rd-round review 2026-09-08):
    - ``max_age_seconds`` (optional): drop directives older than this
      many seconds.  The directive remains in the event log for
      audit; the filter is purely about what a fresh executor should
      act on.  Expiry does not delete the event.
    - ``dedup_seen`` (optional): an externally-tracked set of
      directive event_ids the caller has already consumed.  When
      provided, directives whose event_id is already in the set are
      dropped.  Use this to avoid the same directive being re-applied
      after a snapshot rebuild or context replay.
    """
    out: list[dict[str, Any]] = []
    for m in get_messages(
        conn, contract_id=contract_id, kind="directive", after_event_id=after_event_id
    ):
        target = m.get("to_agent")
        if target is not None and to_agent is not None and to_agent != target:
            # Targeted at another agent; not for us.
            continue
        if max_age_seconds is not None and now is not None and m.get("at"):
            try:
                from datetime import datetime as _dt

                at = _dt.fromisoformat(m["at"])
                if (now - at).total_seconds() > max_age_seconds:
                    continue
            except ValueError:
                pass
        if dedup_seen is not None:
            eid = m.get("event_id")
            if isinstance(eid, int) and eid in dedup_seen:
                continue
        out.append(m)
    return out


__all__ = ["VALID_KINDS", "get_messages", "pending_directives", "send_message"]
