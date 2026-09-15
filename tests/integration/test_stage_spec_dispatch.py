"""Spec-based acceptance: dispatch loop composes verifier outcomes against the spec.

外部 review 3rd-round 建议：仓库已有 acceptance/spec.py，但未接入主执行流程。
本测试覆盖接入路径：
- verifier succeeded + spec verdict pass → contract complete + stage advance
- verifier succeeded + spec verdict fail → contract blocked, no advance
- verifier succeeded + spec verdict pending (user criteria) → contract stays
  active, no advance, ACCEPTANCE_STATUS_CHANGED event records pending

旧 verifier-only 行为（spec is None）保持兼容。
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
    get_events,
    get_goal,
    update_contract_state,
)
from longtask.rpc.handlers.goal import handle_goal_prepare
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _setup(tmp_path: Path) -> tuple[Path, sqlite3.Connection, ExecutorRegistry]:
    root = tmp_path / "data"
    root.mkdir()
    reg = ExecutorRegistry()
    reg.register(
        RegistryEntry(
            id="exec-a",
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
            "spec 阶段",
            "验证 spec dispatch",
            json.dumps({"stages": stages}, ensure_ascii=False),
            "{}",
            NOW.isoformat(),
            NOW.isoformat(),
            2,
        ),
    )
    conn.commit()


def _prepare_with_spec(
    conn: sqlite3.Connection,
    goal_id: str,
    stage_id: str,
    contract_id: str,
    *,
    spec: dict[str, Any],
) -> None:
    spec_hash = "hash-" + stage_id
    draft = {
        "title": f"阶段 {stage_id} 合同",
        "objective": "完成该阶段",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        "acceptance": {
            "standard": "通过",
            "checks": ["result.txt 存在"],
            "verifier": "cross_check",
            "spec": spec,
            "spec_hash": spec_hash,
        },
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 5,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 30,
            "max_output_bytes": 1_048_576,
        },
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


def _record_verifier(
    conn: sqlite3.Connection,
    cid: str,
    *,
    check_results: dict[str, str] | None = None,
    state: str = "succeeded",
) -> None:
    append_event(
        conn,
        contract_id=cid,
        attempt_id="ver-loop",
        event_type=(
            EventType.ATTEMPT_SUCCEEDED if state == "succeeded" else EventType.ATTEMPT_FAILED
        ),
        payload={
            "reported_by": "model",
            "role": "verifier",
            "checks": check_results or {"check1": "pass"},
        },
        now=NOW,
        actor="model",
    )


def _activate(conn: sqlite3.Connection, cid: str) -> None:
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)


def _has_event(conn: sqlite3.Connection, contract_id: str, event_type: str) -> bool:
    return any(
        e.contract_id == contract_id and str(e.event_type) == event_type
        for e in get_events(conn, contract_id=contract_id)
    )


# ── spec is None 旧行为仍正常 ──────────────────────────────────────────


def test_legacy_contract_without_spec_still_passes(tmp_path: Path) -> None:
    """没有 spec 时：verifier succeeded → contract complete，stage advance。

    向后兼容：旧合同（acceptance 没有 spec）走原路径。
    """
    root, conn, reg = _setup(tmp_path)
    _insert_goal(
        conn,
        "goal-legacy",
        [
            {
                "id": "stage-1",
                "title": "实现",
                "acceptance_checks": ["result.txt 存在"],
            }
        ],
    )
    cid = "lt-20260901-legacy"
    # 故意用旧的 draft 路径（不带 spec/spec_hash）
    from lhgp.contracts.acceptance import Acceptance
    from lhgp.contracts.auto_approve import AutoApprove
    from lhgp.contracts.budget import Budget
    from lhgp.contracts.contract_draft import ContractDraft
    from longtask.persistence.store import save_contract

    draft = ContractDraft(
        title="legacy",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",)),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=cid, now=NOW, actor="user", goal_id="goal-legacy")
    _activate(conn, cid)
    _record_verifier(conn, cid, state="succeeded")

    run_daemon_tick(root, conn, reg, now=NOW)

    # 旧合同（无 spec）→ verifier succeeded 仍然 complete
    assert get_contract(conn, cid).state == ContractState.COMPLETE
    # goal progress 未推进（contract 不绑 stage）—— 兼容路径
    progress = get_goal(conn, "goal-legacy")["progress"]
    assert "stage-1" not in progress.get("completed", [])
    conn.close()


# ── spec verdict = pass ──────────────────────────────────────────────


def test_spec_pass_advances_stage(tmp_path: Path) -> None:
    """verifier 报告 file-exists pass → spec verdict pass → 阶段推进。"""
    root, conn, reg = _setup(tmp_path)
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "result.txt"},
        ]
    }
    _insert_goal(
        conn,
        "goal-spec-pass",
        [
            {
                "id": "stage-1",
                "title": "实现",
                "draft": {  # 必填：auto_create 下游会需要
                    "title": "stage-1",
                    "objective": "o",
                    "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                    "hard_constraints": {},
                    "acceptance": {
                        "standard": "s",
                        "checks": ["c"],
                    },
                    "workload_estimate": {"initial_hours": 1.0},
                    "budget": {
                        "max_dispatches": 5,
                        "max_escalations": 1,
                        "max_concurrent_attempts": 1,
                        "max_attempt_minutes": 30,
                        "max_output_bytes": 1_048_576,
                    },
                },
            },
            {"id": "stage-2", "title": "验证"},
        ],
    )
    cid = "lt-20260901-spec-pass"
    _prepare_with_spec(conn, "goal-spec-pass", "stage-1", cid, spec=spec)
    _activate(conn, cid)
    _record_verifier(conn, cid, check_results={"file-exists:result.txt": "pass"})

    run_daemon_tick(root, conn, reg, now=NOW)

    contract = get_contract(conn, cid)
    assert contract.state == ContractState.COMPLETE
    # spec_verdict 落进 contract/completed 事件 payload (the verifier-emitted
    # one, distinguished from update_contract_state's contract/completed by
    # the presence of ``verifier`` key in payload).
    completed = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.CONTRACT_COMPLETED.value
    ]
    assert completed, "应当有 contract/completed 事件"
    verifier_event = next(
        (e for e in completed if "verifier" in (e.payload_json or "")),
        None,
    )
    assert verifier_event is not None, "verifier 触发的 contract/completed 事件缺失"
    payload = json.loads(verifier_event.payload_json or "{}")
    assert payload.get("spec_verdict", {}).get("outcome") == "pass"
    # 阶段推进
    assert get_goal(conn, "goal-spec-pass")["progress"]["completed"] == ["stage-1"]
    conn.close()


# ── spec verdict = fail ──────────────────────────────────────────────


def test_spec_fail_blocks_contract_without_advancing(tmp_path: Path) -> None:
    """verifier 报告 pass 但 spec 要求 file-exists:test.txt，实际报告 file-exists:result.txt=pass
    → spec 视为缺关键 check → fail → contract blocked，stage 不推进。"""
    root, conn, reg = _setup(tmp_path)
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "test.txt"},
        ]
    }
    _insert_goal(
        conn,
        "goal-spec-fail",
        [{"id": "stage-1", "title": "实现"}],
    )
    cid = "lt-20260901-spec-fail"
    _prepare_with_spec(conn, "goal-spec-fail", "stage-1", cid, spec=spec)
    _activate(conn, cid)
    # verifier 上报的是 file-exists:result.txt pass，spec 要求 file-exists:test.txt
    # compose_verdict 会因目标未提及返回 pending，但更直接构造 fail：用 fail
    _record_verifier(conn, cid, check_results={"file-exists:test.txt": "fail"})

    run_daemon_tick(root, conn, reg, now=NOW)

    contract = get_contract(conn, cid)
    # spec fail → 不 complete，退回 active + acceptance_status=FAILED
    assert contract.state == ContractState.ACTIVE
    # 阶段不推进
    progress = get_goal(conn, "goal-spec-fail")["progress"]
    assert "stage-1" not in progress.get("completed", [])
    # CONTRACT_BLOCKED 事件中含 spec_verdict
    blocked = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.CONTRACT_BLOCKED.value
    ]
    assert blocked
    # 找 verifier 触发的那个（payload 含 "verifier" 字段）
    verifier_blocked = next(
        (e for e in blocked if "verifier" in (e.payload_json or "")),
        None,
    )
    assert verifier_blocked is not None
    payload = json.loads(verifier_blocked.payload_json or "{}")
    assert payload.get("spec_verdict", {}).get("outcome") == "fail"
    conn.close()


# ── spec verdict = pending (user criteria) ───────────────────────────


def test_spec_pending_leaves_contract_active(tmp_path: Path) -> None:
    """spec 含 user 判据 → verdict pending → contract 留 active，stage 不推进。"""
    root, conn, reg = _setup(tmp_path)
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "result.txt"},
            {"judge": "user", "question": "demo 满意吗？"},
        ]
    }
    _insert_goal(
        conn,
        "goal-spec-pending",
        [{"id": "stage-1", "title": "实现"}],
    )
    cid = "lt-20260901-spec-pending"
    _prepare_with_spec(conn, "goal-spec-pending", "stage-1", cid, spec=spec)
    _activate(conn, cid)
    _record_verifier(conn, cid, check_results={"file-exists:result.txt": "pass"})

    run_daemon_tick(root, conn, reg, now=NOW)

    contract = get_contract(conn, cid)
    # 留 active
    assert contract.state == ContractState.ACTIVE
    # 阶段不推进
    progress = get_goal(conn, "goal-spec-pending")["progress"]
    assert "stage-1" not in progress.get("completed", [])
    # ACCEPTANCE_STATUS_CHANGED 事件记录 pending 状态
    assert _has_event(conn, cid, EventType.ACCEPTANCE_STATUS_CHANGED.value)
    conn.close()


# ── spec 持久化 round-trip ──────────────────────────────────────────


def test_spec_persists_through_save_and_get(tmp_path: Path) -> None:
    """acceptance.spec / spec_hash 写入数据库后能 round-trip。"""
    _root, conn, _reg = _setup(tmp_path)
    spec = {"all": [{"judge": "machine", "kind": "file-exists", "target": "x"}]}
    _insert_goal(conn, "goal-rt", [{"id": "s1", "title": "t"}])
    cid = "lt-rt-1"
    _prepare_with_spec(conn, "goal-rt", "s1", cid, spec=spec)
    contract = get_contract(conn, cid)
    assert contract.draft.acceptance.spec == spec
    assert contract.draft.acceptance.spec_hash == "hash-s1"
    conn.close()
