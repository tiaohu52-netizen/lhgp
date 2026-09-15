"""Canonical persistence error hierarchy."""

from __future__ import annotations


class StoreError(Exception):
    """Base class for persistence failures."""


class StoreTamperedError(StoreError):
    """The store was externally modified or uses an unsupported schema."""


class LeaseCASError(StoreError):
    """A lease compare-and-swap expectation failed."""


LeaseConflictError = LeaseCASError


class LeaseFencedError(StoreError):
    """A write-back used an expired lease or mismatched attempt."""


class RevisionConflictError(StoreError):
    """A contract revision compare-and-swap failed."""


class IllegalStateTransitionError(StoreError):
    """A state write would make an illegal lifecycle transition (审计 B1)。

    RPC handler 早就在守这张表（approve/pause/resume/cancel/arbitrate 五处调用
    ``is_valid_transition``），但 store 与几处 CAS 直写绕过它们——同一个非法转移，
    走 RPC 被拒、走守护进程或直调却能落库，审计记录与实际状态因此可以互相矛盾。
    这个类型是那层兜底的信号，RPC 边界把它映射为 ``STATE_FORBIDDEN``。
    """


class IdempotencyMismatchError(StoreError):
    """A request id was replayed with different input."""


__all__ = [
    "IdempotencyMismatchError",
    "IllegalStateTransitionError",
    "LeaseCASError",
    "LeaseConflictError",
    "LeaseFencedError",
    "RevisionConflictError",
    "StoreError",
    "StoreTamperedError",
]
