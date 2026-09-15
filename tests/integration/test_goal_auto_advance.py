"""verifier 通过 → Goal 阶段自动推进的完整闭环集成测试。

外部申报的闭环是：
Goal stage → stage acceptance requirements → goal/prepare（绑定）
→ verifier passed → Goal stage auto-advance。

此前测试只覆盖「verifier succeeded → 合同 complete」这一段（test_verifier.py），
「合同 complete → 阶段推进」这段（tick._advance_goal_after_verified_contract
→ store.advance_goal）从未被任何测试执行过。本文件补齐整条链路：

- 绑定合同的 verifier 通过 → 阶段完成、current 指向下一阶段；
- 最后一个阶段完成 → goal 状态 satisfied；
- verifier 失败 → 阶段不推进（完成只能由证据推导，§4.3）；
- 推进后 goal/next-action 指向下一阶段的 create_contract。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.fake_executor import FAKE_MANIFEST
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.daemon import run_daemon_tick
from longtask.contracts.schema import ContractState
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    get_contract,
    get_goal,
    update_contract_state,
)
from longtask.rpc.handlers.goal import handle_goal_next, handle_goal_prepare
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

STAGES = [
    {
        "id": "stage-1",
        "title": "实现",
        "acceptance_checks": ["result.txt 存在"],
    },
    {"id": "stage-2", "title": "验证", "acceptance_checks": ["report.txt 存在"]},
]


def _setup(tmp_path: Path) -> tuple[Path, sqlite3.Connection, ExecutorRegistry]:
    root = tmp_path / "data"
    root.mkdir()
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
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    return root, conn, reg


def _insert_goal(conn: sqlite3.Connection, goal_id: str, stages: list[dict]) -> None:
    conn.execute(
        "INSERT INTO goals (goal_id, revision, title, objective, plan_json, progress_json,"
        " created_at, updated_at, schema_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            goal_id,
            1,
            "闭环目标",
            "跨阶段交付",
            json.dumps({"stages": stages}, ensure_ascii=False),
            "{}",
            NOW.isoformat(),
            NOW.isoformat(),
            2,
        ),
    )
    conn.commit()


def _prepare_bound_contract(
    conn: sqlite3.Connection,
    goal_id: str,
    stage_id: str,
    contract_id: str,
    *,
    check: str = "result.txt 存在",
    executor_id: str | None = None,
) -> None:
    draft = {
        "title": f"阶段 {stage_id} 合同",
        "objective": "完成该阶段",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        "acceptance": {"standard": "通过", "checks": [check], "verifier": "cross_check"},
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 5,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1048576,
        },
    }
    if executor_id:
        draft["authority"] = {
            "executor_policy": "explicit_allow",
            "executors": [{"executor_id": executor_id, "models": ["*"], "roles": ["executor"]}],
        }
    envelope = RequestEnvelope(
        method=Method.GOAL_PREPARE,
        request_id=f"req-{contract_id}",
        client_id="mcp",
        protocol_version=2,
        params={
            "contract_id": contract_id,
            "goal_id": goal_id,
            "stage_id": stage_id,
            "draft": draft,
        },
    )
    handle_goal_prepare(envelope, conn=conn, now=NOW)


def _record_verifier(conn: sqlite3.Connection, cid: str, state: str) -> None:
    append_event(
        conn,
        contract_id=cid,
        attempt_id="ver-loop",
        event_type=(
            EventType.ATTEMPT_SUCCEEDED if state == "succeeded" else EventType.ATTEMPT_FAILED
        ),
        payload={"reported_by": "model", "role": "verifier", "checks": {"check1": "pass"}},
        now=NOW,
        actor="model",
    )


def _activate(conn: sqlite3.Connection, cid: str) -> None:
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)


def _progress(conn: sqlite3.Connection, goal_id: str) -> dict[str, Any]:
    goal = get_goal(conn, goal_id)
    assert goal is not None
    return goal["progress"]


def test_verified_contract_advances_bound_stage(tmp_path: Path) -> None:
    """绑定合同的 verifier 通过 → 阶段完成并指向下一阶段。"""
    root, conn, _reg = _setup(tmp_path)
    _insert_goal(conn, "goal-loop-1", STAGES)
    cid = "lt-20260901-stage1"
    _prepare_bound_contract(conn, "goal-loop-1", "stage-1", cid)
    _activate(conn, cid)
    _record_verifier(conn, cid, "succeeded")

    run_daemon_tick(root, conn, ExecutorRegistry.load_from_file(root / "registry.json"), now=NOW)

    assert get_contract(conn, cid).state == ContractState.COMPLETE
    progress = _progress(conn, "goal-loop-1")
    assert progress["completed"] == ["stage-1"]
    assert progress["current"] == "stage-2"
    assert progress["status"] == "active"
    conn.close()


def test_final_stage_completion_marks_goal_satisfied(tmp_path: Path) -> None:
    """最后一个阶段完成 → goal 状态 satisfied，current 为空。"""
    root, conn, _reg = _setup(tmp_path)
    # 单阶段计划：完成即满足
    _insert_goal(conn, "goal-loop-2", [{"id": "only", "title": "唯一阶段"}])
    cid = "lt-20260901-onlyst"
    _prepare_bound_contract(conn, "goal-loop-2", "only", cid)
    _activate(conn, cid)
    _record_verifier(conn, cid, "succeeded")

    run_daemon_tick(root, conn, ExecutorRegistry.load_from_file(root / "registry.json"), now=NOW)

    progress = _progress(conn, "goal-loop-2")
    assert progress["completed"] == ["only"]
    assert progress["current"] is None
    assert progress["status"] == "satisfied"
    conn.close()


def test_verifier_failure_does_not_advance_stage(tmp_path: Path) -> None:
    """verifier 失败 → 合同退回 active，阶段不推进（完成只能由证据推导）。"""
    root, conn, _reg = _setup(tmp_path)
    _insert_goal(conn, "goal-loop-3", STAGES)
    cid = "lt-20260901-stage3"
    _prepare_bound_contract(conn, "goal-loop-3", "stage-1", cid)
    _activate(conn, cid)
    _record_verifier(conn, cid, "failed")

    run_daemon_tick(root, conn, ExecutorRegistry.load_from_file(root / "registry.json"), now=NOW)

    assert get_contract(conn, cid).state == ContractState.ACTIVE
    progress = _progress(conn, "goal-loop-3")
    assert progress.get("completed", []) == []
    conn.close()


def test_next_action_points_to_next_stage_after_advance(tmp_path: Path) -> None:
    """阶段推进后 goal/next 指向下一阶段的 create_contract。"""
    root, conn, _reg = _setup(tmp_path)
    _insert_goal(conn, "goal-loop-4", STAGES)
    cid = "lt-20260901-stage4"
    _prepare_bound_contract(conn, "goal-loop-4", "stage-1", cid)
    _activate(conn, cid)
    _record_verifier(conn, cid, "succeeded")

    run_daemon_tick(root, conn, ExecutorRegistry.load_from_file(root / "registry.json"), now=NOW)

    envelope = RequestEnvelope(
        method=Method.GOAL_NEXT,
        request_id="req-next-after-advance",
        client_id="mcp",
        protocol_version=2,
        params={"goal_id": "goal-loop-4"},
    )
    result = handle_goal_next(envelope, conn=conn)
    action = result["result"]["next"]
    assert action["stage_id"] == "stage-2"
    assert action["action"] == "create_contract"
    conn.close()


def test_advance_writes_auditable_goal_event(tmp_path: Path) -> None:
    """阶段推进产生 goal/amendment 事件，推进可审计（actor=verifier）。"""
    from longtask.persistence.store import get_events as _get_events_all

    root, conn, _reg = _setup(tmp_path)
    _insert_goal(conn, "goal-loop-5", STAGES)
    cid = "lt-20260901-stage5"
    _prepare_bound_contract(conn, "goal-loop-5", "stage-1", cid)
    _activate(conn, cid)
    _record_verifier(conn, cid, "succeeded")

    run_daemon_tick(root, conn, ExecutorRegistry.load_from_file(root / "registry.json"), now=NOW)

    goal_events = [e for e in _get_events_all(conn) if e.goal_id == "goal-loop-5"]
    amendments = [e for e in goal_events if "amend" in str(e.event_type)]
    assert amendments, "阶段推进应产生可审计的 goal 修订事件"
    advance_events = [e for e in amendments if e.actor == "verifier"]
    assert advance_events, "推进事件应标记 actor=verifier（由证据推导，非执行者声明）"
    conn.close()


def test_goal_continuity_reopens_and_switches_executor(tmp_path: Path) -> None:
    """阶段 1 完成后模拟会话/daemon 重启，阶段 2 可绑定另一 executor。"""
    root, conn, reg = _setup(tmp_path)
    _insert_goal(conn, "goal-continuity", STAGES)
    _prepare_bound_contract(conn, "goal-continuity", "stage-1", "contract-a")
    _activate(conn, "contract-a")
    _record_verifier(conn, "contract-a", "succeeded")
    run_daemon_tick(root, conn, reg, now=NOW)
    assert _progress(conn, "goal-continuity")["current"] == "stage-2"
    conn.close()  # 原会话与 daemon 进程退出

    # 新会话重新打开权威 SQLite，并让不同 executor 接力阶段 2。
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    _prepare_bound_contract(
        conn,
        "goal-continuity",
        "stage-2",
        "contract-b",
        check="report.txt 存在",
        executor_id="exec-b",
    )
    view = get_contract(conn, "contract-b")
    assert view is not None
    assert view.goal_id == "goal-continuity"
    assert view.draft.authority.executors[0].executor_id == "exec-b"
    assert get_goal(conn, "goal-continuity")["plan"]["stages"][1]["contract_id"] == "contract-b"
    conn.close()
