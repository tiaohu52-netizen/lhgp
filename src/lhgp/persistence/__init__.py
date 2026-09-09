"""Canonical persistence boundary for the LHGP state store.

Only this package should be used by integrations that need the authoritative
SQLite connection, schema bootstrap, and event vocabulary.
"""

__all__ = [
    "STORE_SCHEMA_VERSION",
    "EventInput",
    "EventType",
    "Notification",
    "ResumeBrief",
    "ResumeBriefError",
    "StoreConfig",
    "StoredEvent",
    "StoredLease",
    "WriteBackResult",
    "build_resume_brief",
    "claim_notifications",
    "connect",
    "drain_notifications",
    "enqueue_notification",
    "ensure_schema",
    "get_events",
    "list_notifications",
    "mark_failed",
    "mark_sent",
    "prune_sent",
    "transaction",
]


def __getattr__(name: str) -> object:
    """Resolve public symbols lazily to avoid legacy store import cycles."""
    if name == "EventType":
        from lhgp.persistence.events import EventType

        return EventType
    if name in {
        "Notification",
        "claim_notifications",
        "drain_notifications",
        "enqueue_notification",
        "list_notifications",
        "mark_failed",
        "mark_sent",
        "prune_sent",
    }:
        from lhgp.persistence import notifications

        return getattr(notifications, name)
    if name in {"EventInput", "StoredEvent", "StoredLease", "WriteBackResult"}:
        from lhgp.persistence import types

        return getattr(types, name)
    if name in {
        "STORE_SCHEMA_VERSION",
        "StoreConfig",
        "connect",
        "ensure_schema",
        "get_events",
        "transaction",
    }:
        from lhgp.persistence import store

        return getattr(store, name)
    if name in {"ResumeBrief", "ResumeBriefError", "build_resume_brief"}:
        from lhgp.persistence import resume

        return getattr(resume, name)
    raise AttributeError(name)
