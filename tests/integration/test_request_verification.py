"""SPEC §12.4 用户触发验收（contract/request-verification）集成测试。

dogfood v5 发现 2 的修复验证：执行预算耗尽但交付物已就绪时，用户能
直接请求验收——handler 校验+落事件，daemon tick 消费事件派生独立
verifier（RPC handler 无进程表，与 control/interrupt 相同分工）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp.rpc.server import route as canonical_route
from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon_loop import _consume_verification_requests
from longtask.cli.runner import AttemptRunner
from longtask.contracts.schema import (
    Acceptance,
    BlockReason,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.events import EventType
from longtask.persistence.events_query import get_events
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from longtask.promoter.killswitch import KILL_SWITCH_FILE
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.handlers.contract import handle_contract_get, handle_contract_request_verification
from longtask.rpc.server import RequestEnvelope

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def _setup(
    tmp_path: Path,
    *,
    reserved: int = 3,
    state: ContractState | None = None,
    goal_id: str | None = None,
) -> tuple[Path, Any, str, ExecutorRegistry]:
    root = tmp_path / "data"
    ws = root / "ws"
    ws.mkdir(parents=True)
    (ws / "result.txt").write_text("deliverable\n", encoding="utf-8")
    cid = "lt-reqver1"
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="request verification 测试",
            objective="验证用户触发验收",
            deadline_at=NOW + timedelta(hours=1),
            hard_constraints={
                "file_effects": {"mode": "workspace-write", "workspace_root": str(ws)}
            },
            acceptance=Acceptance(
                standard="全部通过",
                checks=({"kind": "file-exists", "target": "result.txt"},),
            ),
            workload_initial_hours=1.5,
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
                verification_attempts_reserved=reserved,
            ),
        ),
        contract_id=cid,
        now=NOW,
        goal_id=goal_id,
    )
    if state is not None:
        # 状态机（审计 B1）：DRAFTED 的出边只有 active/cancelled，所以经 active 落到
        # 目标态。本 fixture 以前直接写目标态（drafted→blocked/complete），走的是
        # handler 早就拒绝、生产不可达的转移——测试不该依赖非法状态。
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
        if state is not ContractState.ACTIVE:
            update_contract_state(
                conn,
                contract_id=cid,
                new_state=state,
                now=NOW,
                blocked_reason=(BlockReason.NEED_USER if state == ContractState.BLOCKED else None),
            )
    reg = ExecutorRegistry()
    for exec_id in ("exec-a", "exec-b"):
        reg.register(
            RegistryEntry(
                id=exec_id,
                kind="fake",
                launch=LaunchSpec(),
                capabilities=FAKE_MANIFEST.capabilities,
                limits={"max_concurrent_attempts": 2},
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
    reg.save_to_file(root / "registry.json")
    return root, conn, cid, reg


def _request(conn: Any, cid: str, request_id: str = "req-ver-1") -> dict:
    from longtask.rpc.methods import Method

    envelope = RequestEnvelope(
        method=Method.CONTRACT_REQUEST_VERIFICATION,
        request_id=request_id,
        client_id="mcp",
        protocol_version=2,
        params={"contract_id": cid},
    )
    return handle_contract_request_verification(envelope, conn=conn, now=NOW)


def _consumed_events(conn: Any, cid: str) -> list[Any]:
    """该合同的「请求已被兑现」凭据事件。"""
    return [
        event
        for event in get_events(conn, contract_id=cid)
        if event.event_type == EventType.VERIFICATION_CONSUMED
    ]


def test_request_on_blocked_contract_resumes_and_records(tmp_path: Path) -> None:
    """dogfood v5 场景：blocked(need-user) + 交付物在 → 请求验收 →
    合同回 active + verification/requested 事件落库。"""
    _root, conn, cid, _reg = _setup(tmp_path, state=ContractState.BLOCKED)
    try:
        result = _request(conn, cid)
        assert result["ok"] is True
        contract = get_contract(conn, cid)
        assert contract.state == ContractState.ACTIVE  # blocked → active
        types = [e.event_type for e in get_events(conn, contract_id=cid)]
        assert EventType.VERIFICATION_REQUESTED.value in types
        assert EventType.CONTRACT_RESUMED.value in types
        requested = [
            e
            for e in get_events(conn, contract_id=cid)
            if e.event_type == EventType.VERIFICATION_REQUESTED
        ]
        assert requested[-1].contract_revision == contract.revision
        # R3 归因修正：role 反映真实发起者（本测试经 mcp 客户端 → model）
        assert requested[-1].role == requested[-1].actor
    finally:
        conn.close()


def test_request_on_active_contract_records_without_resume(tmp_path: Path) -> None:
    _root, conn, cid, _reg = _setup(tmp_path, state=ContractState.ACTIVE)
    try:
        result = _request(conn, cid)
        assert result["ok"] is True
        # active 态不再产生多余的 resume 事件
        types = [e.event_type for e in get_events(conn, contract_id=cid)]
        assert EventType.CONTRACT_RESUMED.value not in types
    finally:
        conn.close()


def test_canonical_rpc_route_exposes_request_verification(tmp_path: Path) -> None:
    """The published ``lhgp`` RPC route must expose the new command surface.

    The legacy route already had the handler, but the canonical namespace is
    what the CLI and model-facing MCP clients use.  A missing canonical
    registration silently turned a valid request into ``not implemented``.
    """
    _root, conn, cid, _reg = _setup(tmp_path, state=ContractState.BLOCKED)
    try:
        from lhgp.rpc.methods import Method as CanonicalMethod

        envelope = RequestEnvelope(
            method=CanonicalMethod.CONTRACT_REQUEST_VERIFICATION,
            request_id="req-ver-canonical",
            client_id="mcp",
            protocol_version=2,
            params={"contract_id": cid},
        )
        result = canonical_route(envelope, conn=conn, now=NOW)
        assert result["ok"] is True
        assert get_contract(conn, cid).state == ContractState.ACTIVE
    finally:
        conn.close()


def test_duplicate_pending_request_is_refused(tmp_path: Path) -> None:
    """daemon 尚未消费时，第二次请求不得重复排队。"""
    _root, conn, cid, _reg = _setup(tmp_path, state=ContractState.ACTIVE)
    try:
        _request(conn, cid, request_id="req-ver-first")
        with pytest.raises(RpcError) as excinfo:
            _request(conn, cid, request_id="req-ver-second")
        assert excinfo.value.code == ErrorCode.STATE_FORBIDDEN
        assert "already pending" in str(excinfo.value)
    finally:
        conn.close()


def test_request_on_terminal_contract_refused(tmp_path: Path) -> None:
    _root, conn, cid, _reg = _setup(tmp_path, state=ContractState.COMPLETE)
    try:
        with pytest.raises(RpcError) as excinfo:
            _request(conn, cid)
        assert excinfo.value.code == ErrorCode.STATE_FORBIDDEN
    finally:
        conn.close()


def test_request_with_running_verifier_refused(tmp_path: Path) -> None:
    _root, conn, cid, _reg = _setup(tmp_path, state=ContractState.ACTIVE)
    try:
        # 已有进行中的 verifier attempt
        conn.execute(
            "INSERT INTO attempts (attempt_id, goal_id, contract_revision, role,"
            " executor_id, model_id, state, lease_generation, admitted_at, updated_at)"
            " VALUES ('ver-running', ?, 1, 'verifier', 'exec-b', '*', 'running', 1, ?, ?)",
            (cid, NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
        with pytest.raises(RpcError) as excinfo:
            _request(conn, cid)
        assert excinfo.value.code == ErrorCode.STATE_FORBIDDEN
        assert "already in progress" in str(excinfo.value)
    finally:
        conn.close()


def test_request_with_exhausted_verification_budget_refused(tmp_path: Path) -> None:
    """验证预算耗尽 → 如实拒绝并说明升级路径（调 reserved 需修订合同）。"""
    _root, conn, cid, _reg = _setup(tmp_path, reserved=1, state=ContractState.ACTIVE)
    try:
        conn.execute(
            "INSERT INTO attempts (attempt_id, goal_id, contract_id, contract_revision, role,"
            " executor_id, model_id, state, lease_generation, admitted_at, updated_at)"
            " VALUES ('ver-old', ?, ?, 1, 'verifier', 'exec-b', '*', 'failed', 1, ?, ?)",
            (cid, cid, NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
        with pytest.raises(RpcError) as excinfo:
            _request(conn, cid)
        assert excinfo.value.code == ErrorCode.STATE_FORBIDDEN
        assert "verification budget exhausted: 1/1" in str(excinfo.value)
    finally:
        conn.close()


def test_daemon_consumes_request_and_dispatches_verifier(tmp_path: Path) -> None:
    """完整链：用户请求事件 → daemon 消费 → 独立 verifier 派生 →
    verification/started 事件；重复消费幂等（不再派第二个）。"""
    root, conn, cid, reg = _setup(tmp_path, state=ContractState.BLOCKED)
    try:
        # 模拟有过 executor attempt（verifier 排除它）
        conn.execute(
            "INSERT INTO attempts (attempt_id, goal_id, contract_revision, role,"
            " executor_id, model_id, state, lease_generation, admitted_at, updated_at)"
            " VALUES ('att-1', ?, 1, 'executor', 'exec-a', '*', 'failed', 1, ?, ?)",
            (cid, NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
        _request(conn, cid)

        runner = AttemptRunner(root, conn, reg)
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=1))

        verifiers = conn.execute(
            "SELECT attempt_id, contract_id, executor_id FROM attempts WHERE role='verifier'"
        ).fetchall()
        assert len(verifiers) == 1
        assert verifiers[0][1] == cid
        assert verifiers[0][2] == "exec-b"  # ≠ 执行者 exec-a
        types = [e.event_type for e in get_events(conn, contract_id=cid)]
        assert EventType.VERIFICATION_STARTED.value in types

        # 幂等：再次消费不再派生
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=2))
        verifiers2 = conn.execute("SELECT COUNT(*) FROM attempts WHERE role='verifier'").fetchone()
        assert verifiers2[0] == 1
    finally:
        conn.close()


def test_daemon_request_idempotence_uses_bound_goal_identity(tmp_path: Path) -> None:
    """绑定长期 Goal 的合同重复消费请求时仍只派生一个 verifier。"""
    root, conn, cid, reg = _setup(tmp_path, state=ContractState.BLOCKED, goal_id="goal-long-lived")
    try:
        conn.execute(
            "INSERT INTO attempts (attempt_id, goal_id, contract_revision, role,"
            " executor_id, model_id, state, lease_generation, admitted_at, updated_at)"
            " VALUES ('att-bound', ?, 1, 'executor', 'exec-a', '*', 'failed', 1, ?, ?)",
            ("goal-long-lived", NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
        _request(conn, cid)

        runner = AttemptRunner(root, conn, reg)
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=1))
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=2))

        count = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE goal_id=? AND role='verifier'",
            ("goal-long-lived",),
        ).fetchone()[0]
        assert count == 1
        lease_events = [
            event
            for event in get_events(conn, contract_id=cid)
            if event.event_type == EventType.LEASE_ACQUIRED
        ]
        assert lease_events and all(event.goal_id == "goal-long-lived" for event in lease_events)
    finally:
        conn.close()


def test_daemon_can_consume_new_request_after_terminal_verifier(tmp_path: Path) -> None:
    """历史 verifier 终态后，新的用户验收请求仍可派生 verifier。"""
    root, conn, cid, reg = _setup(tmp_path, state=ContractState.BLOCKED)
    try:
        conn.execute(
            "INSERT INTO attempts (attempt_id, goal_id, contract_revision, role,"
            " executor_id, model_id, state, lease_generation, admitted_at, terminal_at, updated_at)"
            " VALUES ('ver-old', ?, 1, 'verifier', 'exec-b', '*', 'failed', 1, ?, ?, ?)",
            (cid, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
        _request(conn, cid, request_id="req-ver-new")

        runner = AttemptRunner(root, conn, reg)
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=1))

        count = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE goal_id=? AND role='verifier'",
            (cid,),
        ).fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_daemon_does_not_reconsume_same_request_after_verifier_terminal(
    tmp_path: Path,
) -> None:
    """一个请求事件只兑现一次；重验必须由新的请求事件触发。"""
    root, conn, cid, reg = _setup(tmp_path, state=ContractState.BLOCKED)
    try:
        _request(conn, cid)
        runner = AttemptRunner(root, conn, reg)
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=1))
        verifier_id = conn.execute(
            "SELECT attempt_id FROM attempts WHERE role='verifier'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE attempts SET state='failed', terminal_at=?, updated_at=? WHERE attempt_id=?",
            (NOW.isoformat(), NOW.isoformat(), verifier_id),
        )
        conn.commit()

        # The original request is already consumed, so terminalising its
        # verifier must not silently spend another verification reservation.
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=2))
        count = conn.execute("SELECT COUNT(*) FROM attempts WHERE role='verifier'").fetchone()[0]
        assert count == 1
        consumed = [
            event
            for event in get_events(conn, contract_id=cid)
            if event.event_type == EventType.VERIFICATION_CONSUMED
        ]
        assert len(consumed) == 1
    finally:
        conn.close()


def test_refused_dispatch_does_not_consume_the_request(tmp_path: Path) -> None:
    """审计 B7：拒接（没有派发任何 verifier）不得写「已兑现」凭据。

    ``verification/consumed`` 的语义是「请求已被兑现」——``contract/request-
    verification`` 正是靠它判断是否还有待兑现请求，并据此告诉用户
    「wait for daemon consumption before requesting another」。旧代码对拒接也
    写 consumed（payload 里 outcome=refused），于是杀开关期间提出的验收请求被
    永久丢弃（再也不会重试），同时用户看到「无待兑现请求」的假象。
    """
    root, conn, cid, reg = _setup(tmp_path, state=ContractState.BLOCKED)
    try:
        _request(conn, cid)  # blocked → active，并落 verification/requested
        (root / KILL_SWITCH_FILE).write_text("stop\n", encoding="utf-8")
        runner = AttemptRunner(root, conn, reg)

        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=1))

        verifiers = conn.execute("SELECT COUNT(*) FROM attempts WHERE role='verifier'").fetchone()[
            0
        ]
        assert verifiers == 0, "杀开关生效时不该派出 verifier"
        assert _consumed_events(conn, cid) == [], "没有派发 verifier 却写了消费凭据：请求被永久丢弃"

        # 用户侧仍如实告知「有待兑现请求」，而不是谎称已兑现
        with pytest.raises(RpcError) as excinfo:
            _request(conn, cid, request_id="req-ver-again")
        assert excinfo.value.code == ErrorCode.STATE_FORBIDDEN
        assert "already pending" in str(excinfo.value)

        # 杀开关解除后，同一个请求被兑现——这正是旧代码丢掉的那一次
        (root / KILL_SWITCH_FILE).unlink()
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=2))
        verifiers = conn.execute("SELECT COUNT(*) FROM attempts WHERE role='verifier'").fetchone()[
            0
        ]
        assert verifiers == 1, "解除阻塞后待兑现的请求必须被兑现"
        consumed = _consumed_events(conn, cid)
        assert len(consumed) == 1
        assert json.loads(consumed[0].payload_json or "{}")["outcome"] == "dispatched"
    finally:
        conn.close()


def test_verification_request_survives_store_reopen_before_daemon_consumes(
    tmp_path: Path,
) -> None:
    """请求写入后 daemon 重启/重开数据库仍能唯一兑现它。"""
    root, conn, cid, reg = _setup(tmp_path, state=ContractState.BLOCKED)
    _request(conn, cid)
    conn.close()

    reopened = connect(StoreConfig(db_path=root / "state.db"))
    try:
        runner = AttemptRunner(root, reopened, reg)
        _consume_verification_requests(root, reopened, runner, NOW + timedelta(seconds=3))
        assert (
            reopened.execute("SELECT COUNT(*) FROM attempts WHERE role='verifier'").fetchone()[0]
            == 1
        )
        reopened.commit()
    finally:
        reopened.close()


def test_contract_get_exposes_verification_history(tmp_path: Path) -> None:
    """模型读取合同即可判断验收请求是否已被 daemon 消费。"""
    root, conn, cid, reg = _setup(tmp_path, state=ContractState.BLOCKED)
    try:
        _request(conn, cid)
        runner = AttemptRunner(root, conn, reg)
        _consume_verification_requests(root, conn, runner, NOW + timedelta(seconds=1))
        from longtask.rpc.methods import Method

        result = handle_contract_get(
            RequestEnvelope(
                method=Method.CONTRACT_GET,
                request_id="get-ver-history",
                client_id="mcp",
                protocol_version=2,
                params={"contract_id": cid},
            ),
            conn=conn,
            now=NOW,
        )
        history = result["result"]["verification_history"]
        types = [entry["event_type"] for entry in history]
        assert EventType.VERIFICATION_REQUESTED.value in types
        assert EventType.VERIFICATION_CONSUMED.value in types
        assert EventType.VERIFICATION_STARTED.value in types
    finally:
        conn.close()
