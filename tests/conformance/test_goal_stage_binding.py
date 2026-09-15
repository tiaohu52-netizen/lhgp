"""Goal stage 与合同验收标准的强绑定一致性场景。

覆盖 goal/prepare 在指定 stage_id 时的全部判定分支：

- Goal 不存在 → UNKNOWN_CONTRACT
- stage_id 不在 Goal plan → VALIDATION_FAILED
- stage 已被其他合同占用 → VALIDATION_FAILED
- 合同 checks 未覆盖 stage 要求 → VALIDATION_FAILED（并列出缺失项）
- 覆盖通过 → 合同落库且 contract_id 写回 stage
- 规范化 typed check（SPEC §12.1）与遗留字符串 check 两侧可比（回归：
  旧实现对 typed check 抛 TypeError，因 CheckSpec 不可哈希）
- typed check 可持久化并原样读回

这些分支在引入时没有测试覆盖（覆盖率报告显示 handlers/goal.py
仅 54%，新增绑定块整段未命中），本文件补齐。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    get_goal,
)
from longtask.rpc.errors import ErrorCode, RpcError
from longtask.rpc.handlers.goal import handle_goal_prepare
from longtask.rpc.methods import Method
from longtask.rpc.server import RequestEnvelope

pytestmark = pytest.mark.conformance

NOW = datetime(2026, 9, 1, 22, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=4)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    return conn


def _insert_goal(conn: sqlite3.Connection, goal_id: str, stages: list[dict]) -> None:
    """Seed a Goal whose plan already carries stages.

    Production callers reach this state through ``handle_goal_update``; the
    insert is direct because this file only exercises the prepare-time binding.
    """
    conn.execute(
        "INSERT INTO goals (goal_id, revision, title, objective, plan_json, progress_json,"
        " created_at, updated_at, schema_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            goal_id,
            1,
            "stage binding goal",
            "验证 stage 与合同验收的绑定",
            json.dumps({"stages": stages}, ensure_ascii=False),
            "{}",
            NOW.isoformat(),
            NOW.isoformat(),
            2,
        ),
    )
    conn.commit()


def _draft(checks: list) -> dict:
    return {
        "title": "stage binding 合同",
        "objective": "验证 stage acceptance 覆盖判定",
        "deadline_at": LATER.isoformat(),
        "hard_constraints": {"file_effects": {"mode": "workspace-write"}},
        "acceptance": {"standard": "通过", "checks": checks, "verifier": "cross_check"},
        "workload_estimate": {"initial_hours": 1.0},
        "budget": {
            "max_dispatches": 3,
            "max_escalations": 1,
            "max_concurrent_attempts": 1,
            "max_attempt_minutes": 60,
            "max_output_bytes": 1048576,
        },
    }


def _prepare(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    goal_id: str,
    stage_id: str,
    checks: list,
    contract_id: str = "contract-stage-1",
) -> dict:
    envelope = RequestEnvelope(
        method=Method.GOAL_PREPARE,
        request_id=request_id,
        client_id="mcp",
        protocol_version=2,
        params={
            "contract_id": contract_id,
            "goal_id": goal_id,
            "stage_id": stage_id,
            "draft": _draft(checks),
        },
    )
    return handle_goal_prepare(envelope, conn=conn, now=NOW)


def _stage_contract_id(conn: sqlite3.Connection, goal_id: str, stage_id: str):
    goal = get_goal(conn, goal_id)
    assert goal is not None
    for stage in goal["plan"].get("stages", []):
        if str(stage.get("id")) == stage_id:
            return stage.get("contract_id")
    return None


def test_stage_without_requirements_binds_contract(tmp_path) -> None:
    """stage 未声明 acceptance_checks 时不应产生额外要求。"""
    conn = _conn(tmp_path)
    _insert_goal(conn, "goal-a", [{"id": "s1", "title": "build"}])
    _prepare(conn, "req-a", goal_id="goal-a", stage_id="s1", checks=["result.txt 存在"])
    assert _stage_contract_id(conn, "goal-a", "s1") == "contract-stage-1"
    conn.close()


def test_legacy_check_matching_text_satisfies_stage(tmp_path) -> None:
    """遗留字符串 check 与 stage 要求文本一致时通过。"""
    conn = _conn(tmp_path)
    _insert_goal(
        conn,
        "goal-b",
        [{"id": "s1", "title": "build", "acceptance_checks": ["result.txt 存在"]}],
    )
    _prepare(conn, "req-b", goal_id="goal-b", stage_id="s1", checks=["result.txt 存在"])
    assert _stage_contract_id(conn, "goal-b", "s1") == "contract-stage-1"
    conn.close()


def test_legacy_check_mismatch_refused_with_missing_list(tmp_path) -> None:
    """文本不一致时拒接，并把缺失项回给调用方（模型可据此修正草案）。"""
    conn = _conn(tmp_path)
    _insert_goal(
        conn,
        "goal-c",
        [{"id": "s1", "title": "build", "acceptance_checks": ["result.txt 存在"]}],
    )
    with pytest.raises(RpcError) as excinfo:
        _prepare(
            conn,
            "req-c",
            goal_id="goal-c",
            stage_id="s1",
            checks=["另一个完全不同的检查"],
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details == {"missing_checks": ["result.txt 存在"]}
    assert _stage_contract_id(conn, "goal-c", "s1") is None
    conn.close()


def test_typed_check_satisfies_stage_kind_target(tmp_path) -> None:
    """规范化 typed check 满足 stage 的 kind:target 要求。

    回归：旧实现用 set(draft.acceptance.checks) 比较，CheckSpec 因 args 为
    dict 而不可哈希，任何 typed check 都会抛 TypeError 而非可处理错误。
    """
    conn = _conn(tmp_path)
    _insert_goal(
        conn,
        "goal-d",
        [{"id": "s1", "title": "build", "acceptance_checks": ["file-exists:result.txt"]}],
    )
    _prepare(
        conn,
        "req-d",
        goal_id="goal-d",
        stage_id="s1",
        checks=[{"kind": "file-exists", "target": "result.txt"}],
    )
    assert _stage_contract_id(conn, "goal-d", "s1") == "contract-stage-1"
    conn.close()


def test_typed_check_args_do_not_affect_identity(tmp_path) -> None:
    """args 不参与身份比较：同一 kind:target 即视为覆盖。"""
    conn = _conn(tmp_path)
    _insert_goal(
        conn,
        "goal-d2",
        [{"id": "s1", "title": "build", "acceptance_checks": ["command-exit-zero:pytest"]}],
    )
    _prepare(
        conn,
        "req-d2",
        goal_id="goal-d2",
        stage_id="s1",
        checks=[{"kind": "command-exit-zero", "target": "pytest", "args": {"cwd": "tests"}}],
    )
    assert _stage_contract_id(conn, "goal-d2", "s1") == "contract-stage-1"
    conn.close()


def test_partial_coverage_refused_listing_only_missing(tmp_path) -> None:
    """部分覆盖仍拒接，缺失列表只包含未覆盖项。"""
    conn = _conn(tmp_path)
    _insert_goal(
        conn,
        "goal-e",
        [
            {
                "id": "s1",
                "title": "build",
                "acceptance_checks": ["file-exists:a.txt", "file-exists:b.txt"],
            }
        ],
    )
    with pytest.raises(RpcError) as excinfo:
        _prepare(
            conn,
            "req-e",
            goal_id="goal-e",
            stage_id="s1",
            checks=[{"kind": "file-exists", "target": "a.txt"}],
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    assert excinfo.value.details == {"missing_checks": ["file-exists:b.txt"]}
    conn.close()


def test_stage_not_in_plan_refused(tmp_path) -> None:
    """stage_id 不在 Goal plan 中 → VALIDATION_FAILED。"""
    conn = _conn(tmp_path)
    _insert_goal(conn, "goal-f", [{"id": "other", "title": "x"}])
    with pytest.raises(RpcError) as excinfo:
        _prepare(conn, "req-f", goal_id="goal-f", stage_id="nope", checks=["r"])
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    conn.close()


def test_stage_already_bound_to_other_contract_refused(tmp_path) -> None:
    """stage 已绑定其他合同 → 拒绝覆盖，避免阶段计划与合同脱节。"""
    conn = _conn(tmp_path)
    _insert_goal(
        conn,
        "goal-g",
        [{"id": "s1", "title": "build", "contract_id": "someone-else"}],
    )
    with pytest.raises(RpcError) as excinfo:
        _prepare(
            conn,
            "req-g",
            goal_id="goal-g",
            stage_id="s1",
            checks=["r"],
            contract_id="contract-new",
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_FAILED
    assert _stage_contract_id(conn, "goal-g", "s1") == "someone-else"
    conn.close()


def test_unknown_goal_refused(tmp_path) -> None:
    """Goal 不存在 → UNKNOWN_CONTRACT。"""
    conn = _conn(tmp_path)
    with pytest.raises(RpcError) as excinfo:
        _prepare(conn, "req-h", goal_id="ghost", stage_id="s1", checks=["r"])
    assert excinfo.value.code == ErrorCode.UNKNOWN_CONTRACT
    conn.close()


def test_typed_check_survives_persistence_round_trip(tmp_path) -> None:
    """typed check 可写入权威库并原样读回为 CheckSpec。

    回归：校验路径接受 typed check，但序列化路径直接 json.dumps 整个
    CheckSpec，导致任何 typed check 合同无法落库。
    """
    from lhgp.acceptance.checks import CheckSpec

    conn = _conn(tmp_path)
    _insert_goal(
        conn,
        "goal-i",
        [{"id": "s1", "title": "build", "acceptance_checks": ["file-exists:result.txt"]}],
    )
    _prepare(
        conn,
        "req-i",
        goal_id="goal-i",
        stage_id="s1",
        checks=[{"kind": "file-exists", "target": "result.txt"}, "legacy text"],
    )
    stored = get_contract(conn, "contract-stage-1")
    assert stored is not None
    checks = stored.draft.acceptance.checks
    typed = [c for c in checks if isinstance(c, CheckSpec)]
    assert len(typed) == 1
    assert typed[0].kind.value == "file-exists"
    assert typed[0].target == "result.txt"
    assert "legacy text" in checks
    assert stored.draft.validate() == []
    conn.close()
