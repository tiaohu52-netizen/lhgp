"""Pure contract and attempt state transition rules for LHGP."""

from __future__ import annotations

from lhgp.contracts.contract_view import (
    AcceptanceStatus,
    AttemptState,
    ContractState,
    DeadlineStatus,
)

TERMINAL_STATES = frozenset(
    {
        ContractState.COMPLETE,
        ContractState.SATISFIED,
        ContractState.CANCELLED,
        ContractState.ARCHIVED,
    }
)
NON_TERMINAL_STATES = frozenset(
    {
        ContractState.DRAFTED,
        ContractState.ACTIVE,
        ContractState.PAUSED,
        ContractState.BLOCKED,
        ContractState.EXPIRED,
    }
)
LEGAL_TRANSITIONS = {
    ContractState.DRAFTED: frozenset({ContractState.ACTIVE, ContractState.CANCELLED}),
    ContractState.ACTIVE: frozenset(
        {
            ContractState.PAUSED,
            ContractState.BLOCKED,
            ContractState.SATISFIED,
            ContractState.COMPLETE,
            ContractState.EXPIRED,
            ContractState.CANCELLED,
        }
    ),
    ContractState.PAUSED: frozenset({ContractState.ACTIVE, ContractState.CANCELLED}),
    ContractState.BLOCKED: frozenset(
        {
            ContractState.ACTIVE,
            ContractState.SATISFIED,
            ContractState.COMPLETE,
            ContractState.ARCHIVED,
            ContractState.EXPIRED,
            ContractState.CANCELLED,
        }
    ),
    ContractState.EXPIRED: frozenset(
        {
            ContractState.SATISFIED,
            ContractState.COMPLETE,
            ContractState.ARCHIVED,
            ContractState.ACTIVE,
            ContractState.CANCELLED,
        }
    ),
    ContractState.SATISFIED: frozenset(),
    ContractState.COMPLETE: frozenset(),
    ContractState.CANCELLED: frozenset(),
    ContractState.ARCHIVED: frozenset(),
}


def is_terminal_state(state: ContractState) -> bool:
    return state in TERMINAL_STATES


def is_valid_transition(from_state: ContractState, to_state: ContractState) -> bool:
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


ATTEMPT_TERMINAL_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.STALE,
        AttemptState.ORPHANED,
    }
)
ATTEMPT_LEGAL_TRANSITIONS = {
    AttemptState.ADMITTED: frozenset(
        {
            AttemptState.STARTING,
            AttemptState.RUNNING,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
            AttemptState.STALE,
            AttemptState.ORPHANED,
        }
    ),
    AttemptState.STARTING: frozenset(
        {
            AttemptState.RUNNING,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
            AttemptState.STALE,
            AttemptState.ORPHANED,
        }
    ),
    AttemptState.RUNNING: frozenset(
        {
            AttemptState.WAITING,
            AttemptState.SUCCEEDED,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
            AttemptState.STALE,
            AttemptState.ORPHANED,
        }
    ),
    AttemptState.WAITING: frozenset(
        {
            AttemptState.RUNNING,
            AttemptState.SUCCEEDED,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
            AttemptState.STALE,
            AttemptState.ORPHANED,
        }
    ),
    AttemptState.SUCCEEDED: frozenset(),
    AttemptState.FAILED: frozenset(),
    AttemptState.CANCELLED: frozenset(),
    AttemptState.STALE: frozenset(),
    AttemptState.ORPHANED: frozenset(),
}


def is_terminal_attempt_state(state: AttemptState) -> bool:
    return state in ATTEMPT_TERMINAL_STATES


def is_valid_attempt_transition(from_state: AttemptState, to_state: AttemptState) -> bool:
    return to_state in ATTEMPT_LEGAL_TRANSITIONS.get(from_state, frozenset())


def is_valid_deadline_transition(
    from_status: DeadlineStatus,
    to_status: DeadlineStatus,
    *,
    deadline_passed: bool,
) -> bool:
    if to_status == DeadlineStatus.WAIVED:
        return True
    if deadline_passed and to_status in (DeadlineStatus.NOT_DUE, DeadlineStatus.AT_RISK):
        return False
    return not (
        from_status == DeadlineStatus.MISSED
        and to_status in (DeadlineStatus.NOT_DUE, DeadlineStatus.AT_RISK)
    )


def is_valid_acceptance_transition(
    from_status: AcceptanceStatus,
    to_status: AcceptanceStatus,
) -> bool:
    if from_status == to_status:
        return True
    if from_status == AcceptanceStatus.PASSED:
        return False
    if to_status == AcceptanceStatus.NOT_REQUIRED:
        return True
    if to_status == AcceptanceStatus.FAILED:
        return from_status in {
            AcceptanceStatus.PENDING,
            AcceptanceStatus.CANDIDATE,
            AcceptanceStatus.VERIFYING,
        }
    # CANDIDATE → PASSED is the user-confirmation transition (a
    # ``judge == "user"`` spec criterion explicitly approved by the
    # Principal). Without it a CANDIDATE contract would be
    # permanently stuck — the dispatcher would keep skipping it
    # while no code path could move it on. Allowed from CANDIDATE
    # only; PENDING → PASSED remains the standard verifier path.
    if to_status == AcceptanceStatus.PASSED:
        return from_status == AcceptanceStatus.CANDIDATE
    return False


__all__ = [
    "ATTEMPT_LEGAL_TRANSITIONS",
    "ATTEMPT_TERMINAL_STATES",
    "LEGAL_TRANSITIONS",
    "NON_TERMINAL_STATES",
    "TERMINAL_STATES",
    "is_terminal_attempt_state",
    "is_terminal_state",
    "is_valid_acceptance_transition",
    "is_valid_attempt_transition",
    "is_valid_deadline_transition",
    "is_valid_transition",
]
