"""Canonical notification outbox API during the compatibility window."""

from longtask.persistence.notifications import (
    Notification,
    claim_notifications,
    drain_notifications,
    enqueue_notification,
    get_by_key,
    list_notifications,
    mark_failed,
    mark_sent,
    prune_sent,
)

__all__ = [
    "Notification",
    "claim_notifications",
    "drain_notifications",
    "enqueue_notification",
    "get_by_key",
    "list_notifications",
    "mark_failed",
    "mark_sent",
    "prune_sent",
]
