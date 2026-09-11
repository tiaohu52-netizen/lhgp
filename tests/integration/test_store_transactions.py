"""持久层事务写入与一致性保证集成测试（DESIGN §3.1、§7、§11.3、§13.3、§14）。

真实 SQLite（tmp_path）：
1. 单事务原子性：状态变更 + 租约变更 + 事件追加在同一事务中提交，中途异常全部回滚无半截状态；
2. 租约 CAS：acquire_lease / reclaim_lease 带 expected_generation，
   冲突抛 LeaseCASError，成功 generation+1；
3. 幂等去重：request_id 保证重放返回原结果且不产生第二个事件；
4. fencing 写回：带 write_generation 的写回，generation/attempt 不符抛 LeaseFencedError；
5. 回调注入：支持注入 promoter.lease 的 check_write_fence 校验回调，保证四平面分层约束。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    EventInput,
    IdempotencyMismatchError,
    LeaseCASError,
    LeaseFencedError,
    RevisionConflictError,
    StoreConfig,
    acquire_lease,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    get_events,
    get_lease,
    reclaim_lease,
    release_lease,
    renew_lease,
    save_contract,
    transaction,
    update_contract_state,
    write_back,
)
from longtask.promoter.lease import check_write_fence

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)


def make_draft(title: str = "实现持久层事务写入") -> ContractDraft:
    """构造合法测试合同草稿（DESIGN §4、§11.6）。"""
    return ContractDraft(
        title=title,
        objective="完成 state.db 单事务原子性、CAS、幂等与 fencing 写回",
        deadline_at=LATER,
        hard_constraints={
            "file_effects": "workspace-write",
            "network": "deny",
            "process": "deny",
            "package_install": "deny",
        },
        acceptance=Acceptance(
            standard="全量集成测试通过且无未决异常",
            checks=(
                "CAS 冲突抛异常",
                "中途失败全部回滚",
                "request_id 幂等去重",
                "fencing 隔离有效",
            ),
            verifier="cross_check",
        ),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=3,
            max_concurrent_attempts=2,
            max_attempt_minutes=60,
            max_output_bytes=1048576,
        ),
    )


def setup_store(tmp_path: Path) -> StoreConfig:
    """初始化测试用 store 数据库（DESIGN §13.3）。"""
    db_path = tmp_path / "state.db"
    config = StoreConfig(db_path=db_path)
    conn = connect(config)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    return config


class TestLeaseCASAndGeneration:
    """租约 CAS 机制与代次递增测试（DESIGN §7）。"""

    def test_acquire_lease_cas_success_and_conflict(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-001"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)

            # 1. 首次获取租约：expected_generation=0 成功，generation 变为 1
            lease = acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
            )
            assert lease.generation == 1
            assert lease.holder_attempt_id == "attempt-001"
            assert lease.is_alive(NOW + timedelta(minutes=4))
            assert not lease.is_alive(NOW + timedelta(minutes=6))

            events = get_events(conn, contract_id=contract_id)
            acquired_events = [e for e in events if e.event_type == EventType.LEASE_ACQUIRED]
            assert len(acquired_events) == 1
            assert acquired_events[0].lease_generation == 1
            assert acquired_events[0].attempt_id == "attempt-001"

            # 2. 传入错误的 expected_generation（如 0 或 2）触发 CAS 冲突
            with pytest.raises(LeaseCASError, match="CAS failed"):
                acquire_lease(
                    conn,
                    contract_id=contract_id,
                    holder_attempt_id="attempt-002",
                    expected_generation=0,  # 当前应为 1
                    heartbeat_at=NOW,
                    timeout=timedelta(minutes=5),
                )

            with pytest.raises(LeaseCASError, match="CAS failed"):
                acquire_lease(
                    conn,
                    contract_id=contract_id,
                    holder_attempt_id="attempt-002",
                    expected_generation=2,  # 超前代次
                    heartbeat_at=NOW,
                    timeout=timedelta(minutes=5),
                )

            # 租约状态未被破坏，代次仍为 1
            current = get_lease(conn, contract_id)
            assert current is not None
            assert current.generation == 1
            assert current.holder_attempt_id == "attempt-001"

            # 3. 正确传入 expected_generation=1 成功换人，代次递增至 2
            next_lease = acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-002",
                expected_generation=1,
                heartbeat_at=NOW + timedelta(minutes=1),
                timeout=timedelta(minutes=5),
            )
            assert next_lease.generation == 2
            assert next_lease.holder_attempt_id == "attempt-002"
        finally:
            conn.close()

    def test_reclaim_lease_cas_and_events(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-002"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)
            acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
            )

            # 1. 尝试用错误代次回收抛异常
            with pytest.raises(LeaseCASError, match="reclaim lease CAS failed"):
                reclaim_lease(
                    conn,
                    contract_id=contract_id,
                    expected_generation=2,
                    heartbeat_at=NOW + timedelta(minutes=6),
                    timeout=timedelta(minutes=5),
                )

            # 2. 正确 expected_generation=1 回收成功，代次变为 2，追加 lease/reclaimed 事件
            reclaimed = reclaim_lease(
                conn,
                contract_id=contract_id,
                new_holder_attempt_id="attempt-002",
                expected_generation=1,
                heartbeat_at=NOW + timedelta(minutes=6),
                timeout=timedelta(minutes=5),
                reason="heartbeat timeout after 5m",
            )
            assert reclaimed.generation == 2
            assert reclaimed.holder_attempt_id == "attempt-002"

            events = get_events(conn, contract_id=contract_id)
            reclaimed_events = [e for e in events if e.event_type == EventType.LEASE_RECLAIMED]
            assert len(reclaimed_events) == 1
            assert reclaimed_events[0].lease_generation == 2
            assert reclaimed_events[0].attempt_id == "attempt-002"
        finally:
            conn.close()

    def test_renew_and_release_lease(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-003"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)
            acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
            )

            # 1. 正常心跳续约（代次不变，时间更新）
            renewed = renew_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                lease_generation=1,
                heartbeat_at=NOW + timedelta(minutes=3),
                timeout=timedelta(minutes=5),
            )
            assert renewed.generation == 1
            assert renewed.heartbeat_at == NOW + timedelta(minutes=3)

            # 2. 过期代次续约被拒
            with pytest.raises(LeaseFencedError, match="renew lease fenced"):
                renew_lease(
                    conn,
                    contract_id=contract_id,
                    holder_attempt_id="attempt-001",
                    lease_generation=0,
                    heartbeat_at=NOW + timedelta(minutes=4),
                    timeout=timedelta(minutes=5),
                )

            # 3. 正常释放租约
            release_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                lease_generation=1,
                now=NOW + timedelta(minutes=4),
            )
            assert get_lease(conn, contract_id) is None

            events = get_events(conn, contract_id=contract_id)
            released_events = [e for e in events if e.event_type == EventType.LEASE_RELEASED]
            assert len(released_events) == 1
        finally:
            conn.close()


class TestTransactionAtomicity:
    """单事务原子性与异常回滚测试（DESIGN §3.1、§13.3）。"""

    def test_transaction_rollback_on_intermediate_failure(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-010"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)
            initial_events = get_events(conn, contract_id=contract_id)
            assert len(initial_events) == 1  # 仅 contract/prepared

            # 在单事务内执行：状态变更 + 租约获取 + 事件追加，但中途抛出异常
            with (
                pytest.raises(RuntimeError, match="simulated failure before commit"),
                transaction(conn),
            ):
                update_contract_state(
                    conn,
                    contract_id=contract_id,
                    new_state=ContractState.ACTIVE,
                    now=NOW + timedelta(seconds=1),
                )
                acquire_lease(
                    conn,
                    contract_id=contract_id,
                    holder_attempt_id="attempt-001",
                    expected_generation=0,
                    heartbeat_at=NOW + timedelta(seconds=1),
                    timeout=timedelta(minutes=5),
                )
                append_event(
                    conn,
                    contract_id=contract_id,
                    event_type=EventType.ESCALATION_REMINDED,
                    payload={"reason": "test reminder"},
                    now=NOW + timedelta(seconds=1),
                )
                raise RuntimeError("simulated failure before commit")

            # 验证回滚结果：合同仍为 DRAFTED，修订版本仍为 1，无租约，无任何半截事件
            contract = get_contract(conn, contract_id)
            assert contract is not None
            assert contract.state == ContractState.DRAFTED
            assert contract.revision == 1

            lease = get_lease(conn, contract_id)
            assert lease is None

            events = get_events(conn, contract_id=contract_id)
            assert len(events) == 1
            assert events[0].event_type == EventType.CONTRACT_PREPARED
        finally:
            conn.close()

    def test_transaction_commit_atomicity(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-011"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)

            with transaction(conn):
                update_contract_state(
                    conn,
                    contract_id=contract_id,
                    new_state=ContractState.ACTIVE,
                    now=NOW + timedelta(seconds=1),
                    expected_revision=1,
                )
                acquire_lease(
                    conn,
                    contract_id=contract_id,
                    holder_attempt_id="attempt-001",
                    expected_generation=0,
                    heartbeat_at=NOW + timedelta(seconds=1),
                    timeout=timedelta(minutes=5),
                )
                append_event(
                    conn,
                    contract_id=contract_id,
                    event_type=EventType.ATTEMPT_STARTED,
                    payload={"attempt_id": "attempt-001"},
                    now=NOW + timedelta(seconds=1),
                    attempt_id="attempt-001",
                    lease_generation=1,
                )

            # 提交后全量可见
            contract = get_contract(conn, contract_id)
            assert contract is not None
            assert contract.state == ContractState.ACTIVE
            assert contract.revision == 2

            lease = get_lease(conn, contract_id)
            assert lease is not None
            assert lease.generation == 1
            assert lease.holder_attempt_id == "attempt-001"

            events = get_events(conn, contract_id=contract_id)
            event_types = [e.event_type for e in events]
            assert event_types == [
                EventType.CONTRACT_PREPARED,
                EventType.CONTRACT_APPROVED,
                EventType.LEASE_ACQUIRED,
                EventType.ATTEMPT_STARTED,
            ]
        finally:
            conn.close()


class TestIdempotency:
    """基于 request_id 的幂等去重与重放测试（DESIGN §11.3）。"""

    def test_save_contract_request_id_replay(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-020"
            req_id = "req-contract-create-001"

            # 首次执行
            v1 = save_contract(
                conn,
                make_draft(),
                contract_id=contract_id,
                now=NOW,
                request_id=req_id,
            )
            assert v1.contract_id == contract_id
            events_1 = get_events(conn, contract_id=contract_id)
            assert len(events_1) == 1

            # 同 request_id 重放：返回原合同视图，不产生第二个事件
            v2 = save_contract(
                conn,
                make_draft(),
                contract_id=contract_id,
                now=NOW + timedelta(seconds=5),
                request_id=req_id,
            )
            assert v2.contract_id == v1.contract_id
            assert v2.revision == v1.revision

            events_2 = get_events(conn, contract_id=contract_id)
            assert len(events_2) == 1  # 依然只有一条事件

            # 同 request_id 但草案漂移必须拒绝，避免重试时静默吞掉参数变更。
            with pytest.raises(IdempotencyMismatchError):
                save_contract(
                    conn,
                    make_draft(title="漂移后的合同"),
                    contract_id=contract_id,
                    now=NOW + timedelta(seconds=10),
                    request_id=req_id,
                )

            assert len(get_events(conn, contract_id=contract_id)) == 1
        finally:
            conn.close()

    def test_update_contract_state_request_id_replay(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-021"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)

            req_id = "req-contract-approve-001"
            # 首次 approve
            v1 = update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=ContractState.ACTIVE,
                now=NOW + timedelta(seconds=1),
                request_id=req_id,
            )
            assert v1.state == ContractState.ACTIVE
            assert v1.revision == 2

            events_1 = get_events(conn, contract_id=contract_id)
            assert len(events_1) == 2

            # 重放同一 request_id
            v2 = update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=ContractState.ACTIVE,
                now=NOW + timedelta(seconds=10),
                request_id=req_id,
            )
            assert v2.revision == 2  # revision 不会再次 +1
            events_2 = get_events(conn, contract_id=contract_id)
            assert len(events_2) == 2  # 不产生第二条 approved 事件
        finally:
            conn.close()

    def test_acquire_and_reclaim_lease_request_id_replay(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-022"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)

            req_acquire = "req-lease-acquire-001"
            l1 = acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
                request_id=req_acquire,
            )
            assert l1.generation == 1

            # 重放 acquire_lease
            l1_replay = acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW + timedelta(seconds=2),
                timeout=timedelta(minutes=5),
                request_id=req_acquire,
            )
            assert l1_replay.generation == 1

            # reclaim 首次与重放
            req_reclaim = "req-lease-reclaim-001"
            l2 = reclaim_lease(
                conn,
                contract_id=contract_id,
                expected_generation=1,
                heartbeat_at=NOW + timedelta(minutes=6),
                timeout=timedelta(minutes=5),
                new_holder_attempt_id="attempt-002",
                request_id=req_reclaim,
            )
            assert l2.generation == 2

            l2_replay = reclaim_lease(
                conn,
                contract_id=contract_id,
                expected_generation=1,
                heartbeat_at=NOW + timedelta(minutes=7),
                timeout=timedelta(minutes=5),
                new_holder_attempt_id="attempt-002",
                request_id=req_reclaim,
            )
            assert l2_replay.generation == 2

            # 检查总事件数：1 (prepared) + 1 (acquired) + 1 (reclaimed) = 3
            events = get_events(conn, contract_id=contract_id)
            assert len(events) == 3
        finally:
            conn.close()


class TestFencedWriteBack:
    """Fencing 写回与旧执行者隔离测试（DESIGN §7、§11.3、§14.1）。"""

    def test_fenced_write_back_rejected(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-030"
            # 状态机（审计 B1）：本用例最后一步把合同写成 complete，而 DRAFTED 没有
            # 到 complete 的边；写回通道在生产里也只出现在已派工的合同上（租约、
            # attempt 都要求合同已激活）。按真实生命周期建为 active。
            save_contract(
                conn,
                make_draft(),
                contract_id=contract_id,
                now=NOW,
                state=ContractState.ACTIVE,
            )
            # attempt-001 获得 gen=1 租约
            acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
            )

            # 1. attempt-001 正常写回（gen=1）
            wb1 = write_back(
                conn,
                contract_id=contract_id,
                attempt_id="attempt-001",
                write_generation=1,
                now=NOW + timedelta(minutes=1),
                events=[
                    EventInput(
                        event_type=EventType.CONTEXT_SCRATCH_UPDATED,
                        payload={"note": "step 1 finished"},
                    )
                ],
            )
            assert len(wb1.event_ids) == 1

            # 2. 发生换人（例如 attempt-001 卡死后被 reclaim），代次变为 2，holder 为 attempt-002
            reclaim_lease(
                conn,
                contract_id=contract_id,
                new_holder_attempt_id="attempt-002",
                expected_generation=1,
                heartbeat_at=NOW + timedelta(minutes=6),
                timeout=timedelta(minutes=5),
            )

            # 3. 此时已苏醒的旧 attempt-001 试图携带旧代次 1 写回进度：被拒抛 LeaseFencedError
            with pytest.raises(LeaseFencedError, match="fenced by lease generation 2"):
                write_back(
                    conn,
                    contract_id=contract_id,
                    attempt_id="attempt-001",
                    write_generation=1,  # 过期代次
                    now=NOW + timedelta(minutes=7),
                    events=[
                        EventInput(
                            event_type=EventType.CONTEXT_SCRATCH_UPDATED,
                            payload={"note": "malicious/stale write"},
                        )
                    ],
                    contract_state=ContractState.COMPLETE,  # 绝不污染状态
                )

            # 验证合同状态未被篡改
            contract = get_contract(conn, contract_id)
            assert contract is not None
            assert contract.state == ContractState.ACTIVE

            # 4. 冒充者 attempt-999 用代次 2 写回：因 attempt 不符被拒
            with pytest.raises(LeaseFencedError, match="not lease holder"):
                write_back(
                    conn,
                    contract_id=contract_id,
                    attempt_id="attempt-999",
                    write_generation=2,
                    now=NOW + timedelta(minutes=8),
                    events=[
                        EventInput(
                            event_type=EventType.ATTEMPT_SUCCEEDED,
                            payload={},
                        )
                    ],
                )

            # 5. 合法 attempt-002（gen=2）写回成功并更新合同状态为 COMPLETE
            wb2 = write_back(
                conn,
                contract_id=contract_id,
                attempt_id="attempt-002",
                write_generation=2,
                now=NOW + timedelta(minutes=9),
                events=[
                    EventInput(
                        event_type=EventType.ATTEMPT_SUCCEEDED,
                        payload={"evidence": "checks pass"},
                    )
                ],
                contract_state=ContractState.COMPLETE,
            )
            assert len(wb2.event_ids) == 1

            contract_final = get_contract(conn, contract_id)
            assert contract_final is not None
            assert contract_final.state == ContractState.COMPLETE
        finally:
            conn.close()

    def test_write_back_with_injected_fence_checker(self, tmp_path: Path) -> None:
        """测试注入 promoter.lease.check_write_fence 校验回调（分层解耦）。"""
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-031"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)
            acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
            )

            # 传入 promoter.lease.check_write_fence 回调进行写回校验
            res = write_back(
                conn,
                contract_id=contract_id,
                attempt_id="attempt-001",
                write_generation=1,
                now=NOW + timedelta(minutes=1),
                events=[
                    EventInput(
                        event_type=EventType.CONTEXT_SCRATCH_UPDATED,
                        payload={"note": "verified with promoter checker"},
                    )
                ],
                fence_checker=check_write_fence,
            )
            assert len(res.event_ids) == 1
        finally:
            conn.close()

    def test_write_back_idempotency(self, tmp_path: Path) -> None:
        """写回幂等去重测试（DESIGN §11.3）。"""
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-032"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)
            acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-001",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
            )

            req_wb = "req-writeback-001"
            res1 = write_back(
                conn,
                contract_id=contract_id,
                attempt_id="attempt-001",
                write_generation=1,
                now=NOW + timedelta(minutes=1),
                events=[
                    EventInput(
                        event_type=EventType.CONTEXT_SCRATCH_UPDATED,
                        payload={"data": 123},
                    )
                ],
                request_id=req_wb,
            )

            # 重放 write_back
            res2 = write_back(
                conn,
                contract_id=contract_id,
                attempt_id="attempt-001",
                write_generation=1,
                now=NOW + timedelta(minutes=2),
                events=[
                    EventInput(
                        event_type=EventType.CONTEXT_SCRATCH_UPDATED,
                        payload={"data": 123},
                    )
                ],
                request_id=req_wb,
            )

            assert res1.event_ids == res2.event_ids
            # 事件总数：1 (prepared) + 1 (acquired) + 1 (scratch-updated) = 3
            events = get_events(conn, contract_id=contract_id)
            assert len(events) == 3
        finally:
            conn.close()


class TestPartitionedLeases:
    """分区租约互斥与独立代次递增测试（DESIGN §7.1）。"""

    def test_partition_independent_cas_and_fencing(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-040"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)

            # 分区 A 获取租约
            lease_a = acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-worker-a",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
                partition_id="part-a",
            )
            assert lease_a.partition_id == "part-a"
            assert lease_a.generation == 1

            # 分区 B 获取租约（独立 generation=1）
            lease_b = acquire_lease(
                conn,
                contract_id=contract_id,
                holder_attempt_id="attempt-worker-b",
                expected_generation=0,
                heartbeat_at=NOW,
                timeout=timedelta(minutes=5),
                partition_id="part-b",
            )
            assert lease_b.partition_id == "part-b"
            assert lease_b.generation == 1

            # 分区 A 单独递增（reclaim 后 gen=2）
            reclaim_a = reclaim_lease(
                conn,
                contract_id=contract_id,
                new_holder_attempt_id="attempt-worker-a2",
                expected_generation=1,
                heartbeat_at=NOW + timedelta(minutes=6),
                timeout=timedelta(minutes=5),
                partition_id="part-a",
            )
            assert reclaim_a.generation == 2

            # 分区 B 的租约保持 gen=1 不受分区 A 影响
            current_b = get_lease(conn, contract_id, partition_id="part-b")
            assert current_b is not None
            assert current_b.generation == 1

            # 分区 B 正常写回（gen=1）
            wb_b = write_back(
                conn,
                contract_id=contract_id,
                attempt_id="attempt-worker-b",
                write_generation=1,
                partition_id="part-b",
                now=NOW + timedelta(minutes=7),
                events=[
                    EventInput(
                        event_type=EventType.CONTEXT_SCRATCH_UPDATED,
                        payload={"partition": "part-b"},
                    )
                ],
            )
            assert len(wb_b.event_ids) == 1
        finally:
            conn.close()


class TestRevisionConflict:
    """合同修订版本并发冲突测试（DESIGN §11.2、§11.7）。"""

    def test_revision_conflict_raises_error(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            contract_id = "lt-20260831-050"
            save_contract(conn, make_draft(), contract_id=contract_id, now=NOW)

            # 第一次更新：revision 1 -> 2
            update_contract_state(
                conn,
                contract_id=contract_id,
                new_state=ContractState.ACTIVE,
                now=NOW + timedelta(seconds=1),
                expected_revision=1,
            )

            # 携带过期的 expected_revision=1 更新：抛 RevisionConflictError
            with pytest.raises(RevisionConflictError, match="revision conflict"):
                update_contract_state(
                    conn,
                    contract_id=contract_id,
                    new_state=ContractState.PAUSED,
                    now=NOW + timedelta(seconds=2),
                    expected_revision=1,
                )

            contract = get_contract(conn, contract_id)
            assert contract is not None
            assert contract.state == ContractState.ACTIVE
            assert contract.revision == 2
        finally:
            conn.close()


class TestCrashRecovery:
    """真实进程崩溃恢复（DESIGN §14 持久性保证：只对已提交事务负责）。

    claim: persistence-transactional-writes 的 crash_recovery 证据——
    子进程在事务中途 os._exit 硬崩（无回滚、无清理、无 flush），
    重开数据库后未提交变更全部不可见，无半截状态。
    """

    def test_hard_crash_mid_transaction_leaves_no_partial_state(self, tmp_path: Path) -> None:
        config = setup_store(tmp_path)
        conn = connect(config)
        try:
            save_contract(
                conn,
                make_draft(title="崩溃恢复验证"),
                contract_id="lt-crash-001",
                now=NOW,
            )
            events_before = len(get_events(conn))
        finally:
            conn.close()

        # 子进程：开事务、改合同状态，commit 前 os._exit 硬崩。
        # 脚本与解释器均为固定字面量，无任何不可信输入拼接。
        script = textwrap.dedent(
            """
            import os
            import sys
            from datetime import UTC, datetime
            from pathlib import Path
            from longtask.contracts.schema import ContractState
            from longtask.persistence.store import (
                StoreConfig,
                connect,
                transaction,
                update_contract_state,
            )

            conn = connect(StoreConfig(db_path=Path(sys.argv[1])))
            with transaction(conn):
                update_contract_state(
                    conn,
                    contract_id="lt-crash-001",
                    new_state=ContractState.ACTIVE,
                    now=datetime(2026, 9, 1, tzinfo=UTC),
                )
                os._exit(9)  # 硬崩：跳过 commit 与 rollback
            """
        )
        result = subprocess.run(  # noqa: S603 - 固定脚本+sys.executable，无不可信输入
            [sys.executable, "-c", script, str(config.db_path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 9, result.stderr.decode("utf-8", "replace")

        # 崩溃后重开：WAL 丢弃未提交帧，状态与事件均回到崩溃前
        conn = connect(config)
        try:
            contract = get_contract(conn, "lt-crash-001")
            assert contract is not None
            assert contract.state == ContractState.DRAFTED
            assert contract.revision == 1
            assert len(get_events(conn)) == events_before
        finally:
            conn.close()
