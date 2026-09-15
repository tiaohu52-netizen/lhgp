"""Canonical SQLite schema boundary and pure storage-key helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from longtask.persistence.schema import (
    STORE_SCHEMA_VERSION,
    connect,
    ensure_schema,
)
from longtask.persistence.schema import transaction as _legacy_transaction


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Delegate transaction semantics while keeping the canonical API path."""
    with _legacy_transaction(conn):
        yield conn


def partition_key(partition_id: str | None) -> str:
    """Convert an optional partition id to its persisted key."""
    return partition_id or ""


def parse_partition_key(stored: str) -> str | None:
    """Convert a persisted partition key back to its logical value."""
    return stored or None


def format_event_type(event_type: object) -> str:
    """Normalize an EventType-like value to its wire string."""
    return event_type.value if hasattr(event_type, "value") else str(event_type)


__all__ = [
    "STORE_SCHEMA_VERSION",
    "connect",
    "ensure_schema",
    "format_event_type",
    "parse_partition_key",
    "partition_key",
    "transaction",
]
