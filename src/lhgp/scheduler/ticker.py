"""Pure scheduler clock types and one-pass tick evaluation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

DEFAULT_TICK_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ContractClock:
    deadline_at: datetime
    next_wakeup_at: datetime
    arbitrated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClockEntry:
    contract_id: str
    clock: ContractClock


def is_overdue(clock: ContractClock, now: datetime) -> bool:
    return now > clock.deadline_at


def next_wakeup(clock: ContractClock, now: datetime) -> datetime:
    return clock.next_wakeup_at


def run_tick(
    now: datetime, entries: Sequence[ClockEntry], emit: Callable[[str], None]
) -> tuple[ClockEntry, ...]:
    results: list[ClockEntry] = []
    for entry in entries:
        clock = entry.clock
        if not is_overdue(clock, now):
            if clock.next_wakeup_at <= now:
                emit(f"promote:{entry.contract_id}")
            results.append(entry)
        elif clock.arbitrated_at is None:
            emit(f"contract/expired:{entry.contract_id}")
            results.append(ClockEntry(entry.contract_id, replace(clock, arbitrated_at=now)))
        else:
            results.append(entry)
    return tuple(results)


__all__ = [
    "DEFAULT_TICK_SECONDS",
    "ClockEntry",
    "ContractClock",
    "is_overdue",
    "next_wakeup",
    "run_tick",
]
