"""Pure lease fencing and partition compatibility rules.

**接线状态**：``check_write_fence`` 是生产路径（写回 fencing）；而
:class:`Partition` / :func:`check_partition_compatible` 是**设计先行、尚未接线**
的规则——没有任何生产调用点，``partition_id`` 在所有派工处都取默认空值，
``lease/partition-conflict`` 事件与 ``PARTITION_CONFLICT`` 错误码也从未被
写出。它们保留在这里是被 ADR-005 记录的未来工作（档 4 并行加派/工作区隔离），
单测锁住规则本身，但**不得被文档或工具面描述为已有能力**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class LeaseFencedError(Exception):
    """A stale attempt tried to write through a newer lease."""


@dataclass(frozen=True, slots=True)
class Lease:
    contract_id: str
    holder_attempt_id: str
    generation: int
    heartbeat_at: datetime
    timeout: timedelta
    partition_id: str | None = None

    def is_alive(self, now: datetime) -> bool:
        return now <= self.heartbeat_at + self.timeout


def check_write_fence(lease: Lease, write_generation: int, write_attempt_id: str) -> None:
    if write_generation != lease.generation:
        raise LeaseFencedError(
            f"write generation {write_generation} fenced by lease generation "
            f"{lease.generation} (contract {lease.contract_id})"
        )
    if write_attempt_id != lease.holder_attempt_id:
        raise LeaseFencedError(
            f"write attempt {write_attempt_id} is not lease holder "
            f"{lease.holder_attempt_id} (contract {lease.contract_id})"
        )


@dataclass(frozen=True, slots=True)
class Partition:
    partition_id: str
    scope_paths: tuple[str, ...]
    scope_stages: tuple[str, ...]
    description: str = ""


def _path_prefix_overlap(a: str, b: str) -> bool:
    a_norm, b_norm = a.rstrip("/"), b.rstrip("/")
    return a_norm == b_norm or a_norm.startswith(b_norm + "/") or b_norm.startswith(a_norm + "/")


def check_partition_compatible(new: Partition, active: list[Partition]) -> list[str]:
    errors: list[str] = []
    for other in active:
        if other.partition_id == new.partition_id:
            errors.append(f"partition id already active: {new.partition_id}")
        for path in new.scope_paths:
            for other_path in other.scope_paths:
                if _path_prefix_overlap(path, other_path):
                    errors.append(
                        f"scope_paths overlap with partition {other.partition_id}: "
                        f"{path} vs {other_path}"
                    )
        shared_stages = set(new.scope_stages) & set(other.scope_stages)
        if shared_stages:
            errors.append(
                f"scope_stages overlap with partition {other.partition_id}: {sorted(shared_stages)}"
            )
    return errors


__all__ = [
    "Lease",
    "LeaseFencedError",
    "Partition",
    "check_partition_compatible",
    "check_write_fence",
]
