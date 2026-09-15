"""Contract view axes and frozen-field helpers (LHGP canonical implementation).

The legacy :mod:`longtask.contracts.contract_view` module re-exports this
implementation so both import namespaces share the exact same enum identity.
"""

from __future__ import annotations

from enum import StrEnum


class ContractState(StrEnum):
    """commitment lifecycle axis (DESIGN §5, SPEC §7.1)."""

    DRAFTED = "drafted"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"  # legacy: equivalent to satisfied
    SATISFIED = "satisfied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class DeadlineStatus(StrEnum):
    """deadline_status axis (SPEC §7.2)."""

    NOT_DUE = "not_due"
    AT_RISK = "at_risk"
    MET = "met"
    MISSED = "missed"
    WAIVED = "waived"


class AcceptanceStatus(StrEnum):
    """acceptance_status axis (SPEC §7.3)."""

    PENDING = "pending"
    CANDIDATE = "candidate"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED = "failed"
    UNDETERMINED = "undetermined"
    NOT_REQUIRED = "not_required"


class BlockReason(StrEnum):
    """Canonical blocked reason codes (DESIGN §5, SPEC §10.5)."""

    NEED_USER = "need-user"
    LEASE_DEAD = "lease-dead"
    BUDGET_EXHAUSTED = "budget-exhausted"
    CONSTRAINT_REFUSED = "constraint-refused"
    NO_EXECUTOR = "no-executor"
    ACCEPTANCE_FAILED = "acceptance-failed"
    DEADLINE_MISSED = "deadline-missed"
    NEED_ARBITRATION = "need-arbitration"
    # P1 review (2026-09-08): capacity-recoverable block. Every
    # eligible executor is busy (per-executor max_concurrent_attempts
    # reached), but a candidate *does* exist — the contract should
    # auto-recover as soon as a lease is released, not sit blocked
    # forever the way NO_EXECUTOR would.
    CAPACITY_FULL = "capacity-full"


class AttemptRole(StrEnum):
    """Attempt role (DESIGN §5.2, SPEC §7.4)."""

    EXECUTOR = "executor"
    VERIFIER = "verifier"
    PLANNER = "planner"


class AttemptState(StrEnum):
    """Attempt lifecycle axis (SPEC §7.4)."""

    ADMITTED = "admitted"
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    ORPHANED = "orphaned"


class Enforcement(StrEnum):
    """Constraint enforcement capability level (DESIGN §12.4)."""

    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"
    UNSUPPORTED = "unsupported"


class EventActor(StrEnum):
    """Actor recorded in protocol events (DESIGN §11.7)."""

    USER = "user"
    DAEMON = "daemon"
    PROMOTER = "promoter"
    EXECUTOR = "executor"
    VERIFIER = "verifier"
    SCHEDULER = "scheduler"
    SYSTEM = "system"


FROZEN_FIELDS: frozenset[str] = frozenset(
    {"objective", "deadline_at", "hard_constraints", "authority", "budget"}
)


def to_state_dict(state: ContractState) -> str:
    """Serialize a contract state to its wire value."""

    return state.value


def from_state_dict(value: str) -> ContractState:
    """Parse a wire value into a contract state."""

    return ContractState(value)


__all__ = [
    "FROZEN_FIELDS",
    "AcceptanceStatus",
    "AttemptRole",
    "AttemptState",
    "BlockReason",
    "ContractState",
    "DeadlineStatus",
    "Enforcement",
    "EventActor",
    "from_state_dict",
    "to_state_dict",
]
