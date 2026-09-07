"""Stable persistence boundary dataclasses for LHGP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StoreConfig:
    db_path: Path
    schema_version: int = 4


@dataclass(frozen=True, slots=True)
class StoredLease:
    contract_id: str
    holder_attempt_id: str
    generation: int
    heartbeat_at: datetime
    timeout: timedelta
    partition_id: str | None = None

    def is_alive(self, now: datetime) -> bool:
        return now <= self.heartbeat_at + self.timeout


@dataclass(frozen=True, slots=True)
class StoredEvent:
    event_id: int
    contract_id: str | None
    event_type: str
    payload_json: str
    request_id: str | None
    created_at: datetime
    actor: str
    schema_version: int = 2
    goal_id: str | None = None
    attempt_id: str | None = None
    lease_generation: int | None = None
    contract_revision: int | None = None
    role: str | None = None
    payload_schema_version: int | None = None


@dataclass(frozen=True, slots=True)
class EventInput:
    event_type: Any
    payload: dict[str, Any] = field(default_factory=dict)
    attempt_id: str | None = None
    lease_generation: int | None = None
    request_id: str | None = None
    actor: str | None = None
    contract_revision: int | None = None
    role: str | None = None
    payload_schema_version: int | None = None
    goal_id: str | None = None


@dataclass(frozen=True, slots=True)
class WriteBackResult:
    contract_id: str
    attempt_id: str
    lease_generation: int
    event_ids: tuple[int, ...]
    revision: int | None = None


__all__ = ["EventInput", "StoreConfig", "StoredEvent", "StoredLease", "WriteBackResult"]
