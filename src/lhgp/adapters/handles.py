"""Canonical external-run handles for restart-safe continuity (SPEC §11.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RECOVERY_REATTACH = "reattach"
RECOVERY_POLL = "poll"
RECOVERY_NONRECOVERABLE = "nonrecoverable"
RECOVERY_STRATEGIES = (RECOVERY_REATTACH, RECOVERY_POLL, RECOVERY_NONRECOVERABLE)
EXTERNAL_STATE_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExternalRunHandle:
    """Persisted locator and capability evidence for one external run."""

    external_run_id: str
    session_locator: str
    recovery_strategy: str
    capability_snapshot: dict[str, Any] = field(default_factory=dict)
    process_identity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_run_id": self.external_run_id,
            "session_locator": self.session_locator,
            "recovery_strategy": self.recovery_strategy,
            "capability_snapshot": dict(self.capability_snapshot),
            "process_identity": dict(self.process_identity),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExternalRunHandle:
        return cls(
            external_run_id=str(data.get("external_run_id", "")),
            session_locator=str(data.get("session_locator", "")),
            recovery_strategy=str(data.get("recovery_strategy", RECOVERY_NONRECOVERABLE)),
            capability_snapshot=dict(data.get("capability_snapshot") or {}),
            process_identity=dict(data.get("process_identity") or {}),
        )

    def is_recoverable(self) -> bool:
        return self.recovery_strategy in (RECOVERY_REATTACH, RECOVERY_POLL)


def parse_legacy_session_ref(session_ref: str) -> ExternalRunHandle:
    """Convert pre-P3 ``session_ref`` strings into a persisted handle."""
    parts = session_ref.split(":")
    if len(parts) >= 3 and parts[0] == "subprocess":
        return ExternalRunHandle(
            external_run_id=parts[2],
            session_locator=parts[1],
            recovery_strategy=RECOVERY_POLL,
            capability_snapshot={"transport": "subprocess"},
            process_identity={"pid": parts[2]},
        )
    if len(parts) >= 2 and parts[0] == "fake":
        return ExternalRunHandle(
            external_run_id=parts[1],
            session_locator=parts[1],
            recovery_strategy=RECOVERY_NONRECOVERABLE,
            capability_snapshot={"transport": "fake"},
        )
    return ExternalRunHandle(
        external_run_id=session_ref,
        session_locator=session_ref,
        recovery_strategy=RECOVERY_NONRECOVERABLE,
        capability_snapshot={},
    )


__all__ = [
    "EXTERNAL_STATE_UNKNOWN",
    "RECOVERY_NONRECOVERABLE",
    "RECOVERY_POLL",
    "RECOVERY_REATTACH",
    "RECOVERY_STRATEGIES",
    "ExternalRunHandle",
    "parse_legacy_session_ref",
]
