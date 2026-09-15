"""三批 Required 收口回归（分支 hardening/required-batches）。

批1 持久化：幂等归属校验 / goals UPSERT / quick_check。
批2 调度：并发限额注入生效 / reconcile 成功后补派验收。
批3 进程：stale 释放租约 / daemon pid 身份检测。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import save_contract, update_contract_state

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _make_contract(
    conn: sqlite3.Connection,
    cid: str,
    *,
    state: str | None = None,
    goal_id: str | None = None,
    title: str = "guard fixture",
) -> None:
    save_contract(
        conn,
        contract_id=cid,
        draft=ContractDraft(
            title=title,
            objective="o",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
                verification_attempts_reserved=2,
            ),
        ),
        now=NOW,
        actor="user",
        goal_id=goal_id,
    )
    if state:
        from lhgp.contracts.schema import ContractState

        update_contract_state(conn, contract_id=cid, new_state=ContractState(state), now=NOW)


class TestIdempotentReplayOwnership:
    """批1-R1：跨合同复用 request_id 必须显式拒绝，不得吞掉本次写入。"""

    def test_cross_contract_request_id_rejected(self, tmp_path: Path) -> None:
        from lhgp import PROTOCOL_VERSION
        from lhgp.rpc.errors import RpcError
        from longtask.rpc.handlers.contract import (
            handle_contract_cancel,
        )
        from longtask.rpc.methods import Method
        from longtask.rpc.server import RequestEnvelope

        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-rb01-a")
            _make_contract(conn, "lt-rb01-b")

            def env(rid: str, cid: str) -> RequestEnvelope:
                return RequestEnvelope(
                    method=Method.CONTRACT_CANCEL,
                    request_id=rid,
                    client_id="longtask-cli",
                    protocol_version=PROTOCOL_VERSION,
                    params={"contract_id": cid},
                )

            handle_contract_cancel(env("rb-req-1", "lt-rb01-a"), conn=conn, now=NOW)
            with pytest.raises(RpcError) as excinfo:
                handle_contract_cancel(env("rb-req-1", "lt-rb01-b"), conn=conn, now=NOW)
            assert excinfo.value.code.name == "VALIDATION_FAILED"
            # 合同 B 的取消必须仍然生效（未被静默吞掉）——重新以新 id 取消
            handle_contract_cancel(env("rb-req-2", "lt-rb01-b"), conn=conn, now=NOW)
            state_b = conn.execute(
                "SELECT state FROM contracts WHERE contract_id='lt-rb01-b'"
            ).fetchone()[0]
            assert state_b == "cancelled"
        finally:
            conn.close()


class TestGoalUpsertNoOverwrite:
    """批1-R2：同一 Goal 下立第二份合同不得覆盖 Goal 标题/目标。"""

    def test_second_contract_preserves_goal_identity(self, tmp_path: Path) -> None:
        conn = _make_conn(tmp_path)
        try:
            _make_contract(conn, "lt-rb02-a", goal_id="lt-rb02-a", title="First contract")
            _make_contract(conn, "lt-rb02-b", goal_id="lt-rb02-a", title="Second contract")
            goal = conn.execute(
                "SELECT title, objective FROM goals WHERE goal_id='lt-rb02-a'"
            ).fetchone()
            assert goal[0] == "First contract"  # 第一份合同的标题保留
        finally:
            conn.close()


class TestDoctorQuickCheck:
    def test_doctor_reports_quick_check_failure(self, tmp_path: Path) -> None:
        """批1-R5：库页损坏时 doctor 的 database_integrity 项必须红。"""
        import longtask.cli.doctor as doctor_mod

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # 真实库 + 人为坏页：直接往文件中部写垃圾
        db = data_dir / "state.db"
        conn = _make_conn(data_dir)
        conn.close()
        size = db.stat().st_size
        with open(db, "r+b") as fh:
            fh.seek(min(4096, max(0, size - 512)))
            fh.write(b"\xde\xad\xbe\xef" * 16)
        report = doctor_mod.run_doctor(data_dir)
        db_check = next(c for c in report.checks if c.name == "database_integrity")
        assert db_check.ok is False
        # 两种正确形态：quick_check 报坏页，或损坏重到 connect/migrate 即抛错
        assert "quick_check" in db_check.message or "database error" in db_check.message
