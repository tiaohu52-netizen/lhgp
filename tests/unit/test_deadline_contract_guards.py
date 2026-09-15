"""deadline 可靠性与合同防篡改回归（分支 hardening/deadline-contract-guards）。

- T1/T2：contract/patch 是 Principal 决定权；模型客户端拒接；
  冻结区（objective/deadline_at/hard_constraints/authority）不可经 patch 触碰。
- T3：earliest_next_decision_at 对已过期的决策点原样返回（调用方钳 0
  立即唤醒），不再返回 None 导致 daemon 回退整周期休眠。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lhgp import PROTOCOL_VERSION
from lhgp.contracts.schema import Acceptance, BlockReason, Budget, ContractDraft, ContractState
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import (
    save_contract,
    update_contract_state,
)
from lhgp.rpc.errors import ErrorCode, RpcError
from lhgp.rpc.handlers.contract import handle_contract_patch
from lhgp.rpc.methods import Method
from lhgp.rpc.server import RequestEnvelope

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _contract(conn: sqlite3.Connection, cid: str = "lt-dguard01") -> None:
    save_contract(
        conn,
        contract_id=cid,
        draft=ContractDraft(
            title="guard",
            objective="deadline and tamper guards",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=2.0,
            budget=Budget(
                max_dispatches=5,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
            ),
        ),
        now=NOW,
        actor="user",
    )


def _patch_env(params: dict[str, Any], client_id: str, rid: str) -> RequestEnvelope:
    return RequestEnvelope(
        method=Method.CONTRACT_PATCH,
        request_id=rid,
        client_id=client_id,
        protocol_version=PROTOCOL_VERSION,
        params=params,
    )


class TestPatchPrincipalGate:
    def test_model_client_cannot_patch(self, tmp_path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            env = _patch_env(
                {
                    "contract_id": "lt-dguard01",
                    "expected_revision": 1,
                    "patch": {"soft_guidance": {"note": "injected"}},
                },
                client_id="mcp",
                rid="req-patch-mcp",
            )
            with pytest.raises(RpcError) as excinfo:
                handle_contract_patch(env, conn=conn, now=NOW)
            assert excinfo.value.code == ErrorCode.AUTH_FAILED
            view = conn.execute(
                "SELECT revision FROM contracts WHERE contract_id='lt-dguard01'"
            ).fetchone()
            assert view[0] == 1, "revision changed despite rejection"
        finally:
            conn.close()

    def test_user_cli_can_patch_soft_field(self, tmp_path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            env = _patch_env(
                {
                    "contract_id": "lt-dguard01",
                    "expected_revision": 1,
                    "patch": {"soft_guidance": {"note": "user note"}},
                },
                client_id="longtask-cli",
                rid="req-patch-user",
            )
            resp = handle_contract_patch(env, conn=conn, now=NOW)
            assert resp["ok"] is True
        finally:
            conn.close()

    @pytest.mark.parametrize("field", ["deadline_at", "objective", "authority", "budget"])
    def test_frozen_fields_rejected_even_for_user(self, tmp_path, field: str) -> None:
        """冻结区不可触碰：deadline/authority 等即使模型或用户误传也拒接。"""
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            env = _patch_env(
                {
                    "contract_id": "lt-dguard01",
                    "expected_revision": 1,
                    "patch": {field: {"shortened": True}},
                },
                client_id="longtask-cli",
                rid=f"req-patch-frozen-{field}",
            )
            with pytest.raises(RpcError) as excinfo:
                handle_contract_patch(env, conn=conn, now=NOW)
            assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
            row = conn.execute(
                "SELECT deadline_at FROM contracts WHERE contract_id='lt-dguard01'"
            ).fetchone()
            assert row[0] == (NOW + timedelta(hours=4)).isoformat(), "deadline mutated via patch"
        finally:
            conn.close()


class TestEarliestDecisionPoint:
    def test_overdue_decision_point_returned_not_none(self, tmp_path) -> None:
        """T3：过期的 MIN 决策点必须原样返回（调用方钳 0 立即唤醒）。

        修复前：blocked 合同残留过去时刻会把 MIN 拖成过去 → 函数返回
        None → daemon 回退整周期休眠，active 合同的未来决策点被吞。
        """
        from lhgp.persistence.decisions import earliest_next_decision_at

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            # 状态机（审计 B1）：DRAFTED 不能直达 blocked（生产里由 handler 拒绝），
            # 经 active 落下——本用例只关心 blocked 合同的决策点语义。
            update_contract_state(
                conn, contract_id="lt-dguard01", new_state=ContractState.ACTIVE, now=NOW
            )
            update_contract_state(
                conn,
                contract_id="lt-dguard01",
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.NEED_USER,
                next_decision_at=NOW - timedelta(minutes=5),
            )
            got = earliest_next_decision_at(conn, now=NOW)
            assert got is not None, "overdue point swallowed -> daemon oversleeps"
            assert got <= NOW
        finally:
            conn.close()

    def test_future_point_still_returned(self, tmp_path) -> None:
        from lhgp.persistence.decisions import earliest_next_decision_at

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            # 状态机（审计 B1）：同上，经 active 落到 blocked。
            update_contract_state(
                conn, contract_id="lt-dguard01", new_state=ContractState.ACTIVE, now=NOW
            )
            update_contract_state(
                conn,
                contract_id="lt-dguard01",
                new_state=ContractState.BLOCKED,
                now=NOW,
                blocked_reason=BlockReason.NEED_USER,
                next_decision_at=NOW + timedelta(minutes=5),
            )
            got = earliest_next_decision_at(conn, now=NOW)
            assert got is not None and got > NOW
        finally:
            conn.close()


class TestVerificationBudgetExhaustBlocks:
    def test_exhausted_budget_blocks_contract(self, tmp_path) -> None:
        """T5：验证预算耗尽 → 合同转 blocked(need-user)，不再烧执行预算。"""
        from longtask.adapters.fake_executor import FAKE_MANIFEST
        from longtask.adapters.registry import (
            CostHint,
            ExecutorRegistry,
            LaunchSpec,
            RegistryEntry,
        )
        from longtask.cli.runner import AttemptRunner
        from longtask.contracts.schema import ContractState

        root = tmp_path / "data"
        root.mkdir()
        (root / "ws").mkdir()
        conn = sqlite3.connect(root / "state.db")
        try:
            ensure_schema(conn)
            cid = "lt-vbguard1"
            save_contract(
                conn,
                contract_id=cid,
                draft=ContractDraft(
                    title="vb",
                    objective="budget guard",
                    deadline_at=NOW + timedelta(hours=4),
                    hard_constraints={
                        "file_effects": {
                            "mode": "workspace-write",
                            "workspace_root": str(root / "ws"),
                        }
                    },
                    acceptance=Acceptance(standard="s", checks=("c1",)),
                    workload_initial_hours=1.0,
                    budget=Budget(
                        max_dispatches=5,
                        max_escalations=1,
                        max_concurrent_attempts=1,
                        max_attempt_minutes=30,
                        max_output_bytes=1048576,
                        verification_attempts_reserved=1,
                    ),
                ),
                now=NOW,
                actor="user",
            )
            update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
            # 一条已终态的 verifier attempt：预算 1/1 立即耗尽
            conn.execute(
                "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
                " admitted_at, contract_revision, updated_at)"
                " VALUES ('ver-old', ?, ?, 'verifier', 'failed', ?, 1, ?)",
                (cid, cid, NOW.isoformat(), NOW.isoformat()),
            )
            conn.commit()

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
            runner = AttemptRunner(root, conn, reg)
            ok = runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-a")
            assert ok is False
            state = conn.execute(
                "SELECT state, blocked_reason FROM contracts WHERE contract_id=?", (cid,)
            ).fetchone()
            assert state[0] == "blocked", "contract kept dispatching after budget exhausted"
            assert state[1] == "need-user"
            assert ContractState.BLOCKED.value == "blocked"
        finally:
            conn.close()
