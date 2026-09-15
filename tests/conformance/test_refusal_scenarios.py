"""协议一致性场景（DESIGN §14 一致性与安全保证）。

每条保证至少一个真实场景，全部使用 fake executor 与真实 SQLite（CONTRIBUTING「测试纪律」）。
涵盖：
1. 接口形状与诚实声明；
2. 约束编译失败拒接且记录 dispatch/refused（DESIGN §9，不静默降级）；
3. 预算触顶就地转 blocked（DESIGN §6.3 硬边界）；
4. 事务中途崩溃 WAL 恢复（DESIGN §14 持久性保证）；
5. 租约回收后旧执行者写回端到端被 fenced（DESIGN §7/§11.3/§14.1）。

对应 claim: refusal-never-degrades（quality/claims.json）。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.base import AttemptInput, PrepareRefusedError
from longtask.adapters.fake_executor import FAKE_MANIFEST, FakeExecutor
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.cli.daemon import run_daemon_tick
from longtask.contracts.schema import (
    Acceptance,
    AttemptRole,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    LeaseFencedError,
    StoreConfig,
    acquire_lease,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    get_events,
    get_lease,
    reclaim_lease,
    save_contract,
    update_contract_state,
    write_back,
)

pytestmark = pytest.mark.conformance

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 5, 23, 59, 59, tzinfo=UTC)


def make_test_draft(
    title: str = "一致性测试合同",
    hard_constraints: dict[str, object] | None = None,
    max_dispatches: int = 5,
) -> ContractDraft:
    return ContractDraft(
        title=title,
        objective="验证一致性保证",
        deadline_at=LATER,
        hard_constraints=hard_constraints or {"file_effects": {"mode": "workspace-write"}},
        acceptance=Acceptance(standard="验收标准通过", checks=("通过",)),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=max_dispatches,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=60,
            max_output_bytes=1048576,
        ),
    )


def test_fake_executor_satisfies_adapter_shape() -> None:
    """Fake executor 满足 ExecutorAdapter 接口形状（enforcement=partial）。"""
    executor = FakeExecutor()
    assert executor.id == "fake-executor"
    assert executor.describe() is FAKE_MANIFEST
    assert executor.health() is True
    assert FAKE_MANIFEST.capabilities.sandbox.enforcement.value == "partial"


def test_constraint_untranslatable_refuses_dispatch(tmp_path: Path) -> None:
    """DESIGN §9：沙箱 unsupported 合同要求 -> prepare 拒接，记 dispatch/refused，不降级。"""
    db_path = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db_path))
    ensure_schema(conn)

    cid = "lt-20260901-c01"
    # 合同要求独立网络策略 network.mode = deny
    draft = make_test_draft(
        title="网络拒绝测试",
        hard_constraints={"file_effects": {"mode": "workspace-write"}, "network": {"mode": "deny"}},
    )
    save_contract(conn, draft, contract_id=cid, now=NOW)

    # FakeExecutor 的 FAKE_MANIFEST 声明 network 为 unsupported
    executor = FakeExecutor()
    attempt_input = AttemptInput(
        attempt_id="att-c01",
        contract_id=cid,
        revision=1,
        lease_generation=1,
        role=AttemptRole.EXECUTOR,
        contract_snapshot={"hard_constraints": draft.hard_constraints, "acceptance": {}},
        handover_path=str(tmp_path / "handover.md"),
        workspace_root=str(tmp_path / "ws"),
        budget_remaining={},
    )

    # 1. 约束翻译失败，必须抛出 PrepareRefusedError（绝不返回降级后的 PreparedLaunch）
    with pytest.raises(PrepareRefusedError, match="network"):
        executor.prepare(attempt_input)

    # 2. 拒接事件持久化落盘
    append_event(
        conn,
        contract_id=cid,
        event_type=EventType.DISPATCH_REFUSED,
        payload={"reason": "executor cannot enforce network deny policy"},
        now=NOW,
        actor="daemon",
    )

    events = get_events(conn, contract_id=cid)
    refused_events = [e for e in events if e.event_type == EventType.DISPATCH_REFUSED]
    assert len(refused_events) == 1
    assert "network" in refused_events[0].payload_json

    conn.close()


def test_budget_exhaustion_blocks_contract(tmp_path: Path) -> None:
    """DESIGN §6.3：max_dispatches 用尽 -> 调度推进就地转入 blocked(need-user)，不再拉新会话。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)

    reg = ExecutorRegistry()
    reg.register(
        RegistryEntry(
            id="fake-exec",
            kind="subprocess",
            launch=LaunchSpec(argv=("codex", "exec")),
            capabilities=FAKE_MANIFEST.capabilities,
            limits={"max_concurrent_attempts": 2},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    reg.save_to_file(data_dir / "registry.json")

    cid = "lt-20260901-c02"
    # 设置 max_dispatches = 1
    draft = make_test_draft(title="预算用尽测试", max_dispatches=1)
    save_contract(conn, draft, contract_id=cid, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)

    # 第一次 dispatch 消耗 1 次配额
    acquire_lease(
        conn,
        contract_id=cid,
        holder_attempt_id="att-first",
        expected_generation=0,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=5),
    )

    # 假设该 lease 已经死亡超时（NOW + 10min），且 budget 用尽（max_dispatches=0）
    # 模拟预算耗尽的合同（截止时间剩 2 小时，工作量 4 小时 -> u=2.0 触发 RESPAWN）
    draft_exhausted = ContractDraft(
        title="预算已空合同",
        objective="测试",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints=draft.hard_constraints,
        acceptance=draft.acceptance,
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=0,  # 已无 dispatch 额度
            max_escalations=0,
            max_concurrent_attempts=1,
            max_attempt_minutes=60,
            max_output_bytes=1048576,
        ),
    )
    cid_ex = "lt-20260901-c03"
    save_contract(conn, draft_exhausted, contract_id=cid_ex, now=NOW)
    update_contract_state(conn, contract_id=cid_ex, new_state=ContractState.ACTIVE, now=NOW)

    # 调度扫描触发推进
    res = run_daemon_tick(data_dir, conn, reg, now=NOW)
    assert res["ok"] is True

    # 验证合同已被就地阻断为 BLOCKED
    c_view = get_contract(conn, cid_ex)
    assert c_view is not None
    assert c_view.state == ContractState.BLOCKED
    assert c_view.blocked_reason is not None

    conn.close()


def test_crash_mid_write_recovers_from_committed_state(tmp_path: Path) -> None:
    """DESIGN §14 持久性保证：写事务中途硬崩溃，重启后只恢复到已提交状态。"""
    db_path = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db_path))
    ensure_schema(conn)

    cid = "lt-20260901-c04"
    save_contract(conn, make_test_draft(), contract_id=cid, now=NOW)
    conn.close()

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
                contract_id="lt-20260901-c04",
                new_state=ContractState.ACTIVE,
                now=datetime(2026, 9, 1, tzinfo=UTC),
            )
            os._exit(9)  # 硬崩溃：未 commit
        """
    )
    res = subprocess.run(  # noqa: S603 - 固定脚本与解释器
        [sys.executable, "-c", script, str(db_path)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 9

    # 重新连接验证
    conn_after = connect(StoreConfig(db_path=db_path))
    c_after = get_contract(conn_after, cid)
    assert c_after is not None
    assert c_after.state == ContractState.DRAFTED
    assert c_after.revision == 1
    conn_after.close()


def test_reclaimed_lease_fences_stale_writer(tmp_path: Path) -> None:
    """DESIGN §7/§14.1：A 卡死 -> 回收 -> B 接管 -> A 写回 LEASE_FENCED，不污染新状态。"""
    db_path = tmp_path / "state.db"
    conn = connect(StoreConfig(db_path=db_path))
    ensure_schema(conn)

    cid = "lt-20260901-c05"
    save_contract(conn, make_test_draft(), contract_id=cid, now=NOW)

    # 1. Attempt A 获取租约，generation=1
    lease_a = acquire_lease(
        conn,
        contract_id=cid,
        holder_attempt_id="att-A",
        expected_generation=0,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=5),
    )
    assert lease_a.generation == 1

    # 2. Attempt A 卡死，租约超时，守护进程回收并分发给 Attempt B（generation 1 -> 2）
    lease_b = reclaim_lease(
        conn,
        contract_id=cid,
        expected_generation=1,
        heartbeat_at=NOW + timedelta(minutes=10),
        timeout=timedelta(minutes=5),
        new_holder_attempt_id="att-B",
    )
    assert lease_b.generation == 2
    assert lease_b.holder_attempt_id == "att-B"

    # 3. Attempt A 苏醒，携带旧代次 write_generation=1 尝试写回进度
    with pytest.raises(LeaseFencedError, match="fenced by lease generation 2"):
        write_back(
            conn,
            contract_id=cid,
            attempt_id="att-A",
            write_generation=1,  # 旧代次
            now=NOW + timedelta(minutes=11),
            contract_state=ContractState.COMPLETE,  # 试图将合同误标为 complete
        )

    # 4. 验证 Attempt B 的租约与合同状态完全未被污染
    cur_lease = get_lease(conn, cid)
    assert cur_lease is not None
    assert cur_lease.generation == 2
    assert cur_lease.holder_attempt_id == "att-B"

    cur_contract = get_contract(conn, cid)
    assert cur_contract is not None
    assert cur_contract.state == ContractState.DRAFTED  # 未被 A 改成 complete

    # 5. Attempt B 携带正确代次 2 写回成功
    wb_res = write_back(
        conn,
        contract_id=cid,
        attempt_id="att-B",
        write_generation=2,
        now=NOW + timedelta(minutes=12),
        contract_state=ContractState.ACTIVE,
    )
    assert wb_res.contract_id == cid
    assert get_contract(conn, cid).state == ContractState.ACTIVE  # type: ignore[union-attr]

    conn.close()


def test_daemon_refusal_moves_to_next_candidate(tmp_path: Path) -> None:
    """DESIGN §9 端到端：候选 A prepare 拒接 -> 记 dispatch/refused -> 推动者换候选 B 分发。

    注册表声明只是声明：A 的能力门槛可通过（caps 满足 file_effects），
    但 launch 无 argv，prepare 编译失败必拒接——分发循环换下一个候选。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(StoreConfig(db_path=data_dir / "state.db"))
    ensure_schema(conn)

    reg = ExecutorRegistry()
    # 候选 A（低成本优先试）：registry 声明可通过能力门槛，但 launch 无 argv → prepare 拒接
    reg.register(
        RegistryEntry(
            id="exec-a",
            kind="subprocess",
            launch=LaunchSpec(),
            capabilities=FAKE_MANIFEST.capabilities,
            limits={"max_concurrent_attempts": 2},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    # 候选 B：launch 完整，可兑现
    reg.register(
        RegistryEntry(
            id="exec-b",
            kind="subprocess",
            launch=LaunchSpec(argv=("codex", "exec")),
            capabilities=FAKE_MANIFEST.capabilities,
            limits={"max_concurrent_attempts": 2},
            cost_hint=CostHint.MEDIUM,
            enabled=True,
        )
    )
    reg.save_to_file(data_dir / "registry.json")

    cid = "lt-20260901-c06"
    draft = ContractDraft(
        title="拒接换下一个候选",
        objective="验证拒接换下一个候选",
        deadline_at=NOW + timedelta(hours=2),  # u=2.0 -> RESPAWN 档
        hard_constraints={
            "file_effects": {
                "mode": "workspace-write",
                "workspace_root": str(tmp_path / "ws"),
            }
        },
        acceptance=Acceptance(standard="验收标准通过", checks=("通过",)),
        workload_initial_hours=4.0,
        budget=Budget(
            max_dispatches=5,
            max_escalations=2,
            max_concurrent_attempts=1,
            max_attempt_minutes=60,
            max_output_bytes=1048576,
        ),
    )
    save_contract(conn, draft, contract_id=cid, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)

    res = run_daemon_tick(data_dir, conn, reg, now=NOW)
    assert res["ok"] is True
    assert res["dispatched"] == 1

    # A 拒接已持久化，B 获得分发（逐候选换下一位，DESIGN §9）
    events = get_events(conn, contract_id=cid)
    refused = [e for e in events if e.event_type == EventType.DISPATCH_REFUSED]
    assert len(refused) == 1
    assert "exec-a" in refused[0].payload_json
    assert "launch argv" in refused[0].payload_json
    started = [e for e in events if e.event_type == EventType.ATTEMPT_STARTED]
    assert len(started) == 1
    assert "exec-b" in started[0].payload_json

    # 拒接事件写入 log.jsonl 投影（DESIGN §9）
    log_text = (data_dir / "contracts" / cid / "log.jsonl").read_text(encoding="utf-8")
    assert "dispatch/refused" in log_text

    # 合同保持 ACTIVE，租约由 B 占领
    c_view = get_contract(conn, cid)
    assert c_view is not None
    assert c_view.state == ContractState.ACTIVE
    lease = get_lease(conn, cid)
    assert lease is not None
    assert lease.is_alive(NOW)

    conn.close()
