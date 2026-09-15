"""租约 fencing 与分区互斥（DESIGN §7、§7.1）。

对应 claim: lease-fencing-logic（quality/claims.json）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from longtask.promoter.lease import (
    Lease,
    LeaseFencedError,
    Partition,
    check_partition_compatible,
    check_write_fence,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def make_lease(generation: int = 3, holder: str = "attempt-007") -> Lease:
    return Lease(
        contract_id="lt-20260831-001",
        holder_attempt_id=holder,
        generation=generation,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=5),
    )


class TestHeartbeat:
    def test_alive_within_timeout(self) -> None:
        lease = make_lease()
        assert lease.is_alive(NOW + timedelta(minutes=4))

    def test_dead_after_timeout(self) -> None:
        lease = make_lease()
        assert not lease.is_alive(NOW + timedelta(minutes=6))

    def test_boundary_is_alive(self) -> None:
        lease = make_lease()
        assert lease.is_alive(NOW + timedelta(minutes=5))


class TestWriteFence:
    def test_current_holder_writes_ok(self) -> None:
        check_write_fence(
            make_lease(generation=3), write_generation=3, write_attempt_id="attempt-007"
        )

    def test_stale_generation_fenced(self) -> None:
        # A 卡死 → 租约回收（gen 3→4）→ B 接管 → A 苏醒继续写（DESIGN §7）
        with pytest.raises(LeaseFencedError, match="fenced by lease generation 4"):
            check_write_fence(
                make_lease(generation=4), write_generation=3, write_attempt_id="attempt-007"
            )

    def test_future_generation_fenced(self) -> None:
        # 写回携带比当前更新的 generation 同样拒绝：不允许凭空「认领」代次
        with pytest.raises(LeaseFencedError):
            check_write_fence(
                make_lease(generation=3), write_generation=4, write_attempt_id="attempt-007"
            )

    def test_wrong_attempt_same_generation_fenced(self) -> None:
        with pytest.raises(LeaseFencedError, match="not lease holder"):
            check_write_fence(
                make_lease(generation=3), write_generation=3, write_attempt_id="attempt-999"
            )


class TestPartitionCompatibility:
    def test_disjoint_partitions_ok(self) -> None:
        active = [
            Partition(
                partition_id="p1",
                scope_paths=("~/work/part-a/",),
                scope_stages=("research",),
            )
        ]
        new = Partition(
            partition_id="p2",
            scope_paths=("~/work/part-b/",),
            scope_stages=("draft",),
        )
        assert check_partition_compatible(new, active) == []

    def test_path_prefix_overlap_rejected(self) -> None:
        active = [
            Partition(
                partition_id="p1",
                scope_paths=("~/work/shared/",),
                scope_stages=("research",),
            )
        ]
        new = Partition(
            partition_id="p2",
            scope_paths=("~/work/shared/deeper/",),  # p1 前缀的子路径
            scope_stages=("draft",),
        )
        errors = check_partition_compatible(new, active)
        assert any("scope_paths overlap" in e for e in errors)

    def test_stage_overlap_rejected(self) -> None:
        active = [
            Partition(
                partition_id="p1",
                scope_paths=("~/work/a/",),
                scope_stages=("research", "draft"),
            )
        ]
        new = Partition(
            partition_id="p2",
            scope_paths=("~/work/b/",),
            scope_stages=("draft",),
        )
        errors = check_partition_compatible(new, active)
        assert any("scope_stages overlap" in e for e in errors)

    def test_duplicate_partition_id_rejected(self) -> None:
        active = [Partition(partition_id="p1", scope_paths=("~/a/",), scope_stages=("s1",))]
        new = Partition(partition_id="p1", scope_paths=("~/b/",), scope_stages=("s2",))
        errors = check_partition_compatible(new, active)
        assert any("already active" in e for e in errors)
