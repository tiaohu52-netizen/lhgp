"""MCP server 集成测试（DESIGN §11.1、§17）。

真实 stdio 通信：spawn `longtask-mcp` 子进程，line-delimited JSON-RPC。
覆盖：协议握手（initialize / tools.list）、核心工具调通
（health / list_executors / prepare_contract / approve / get / attach_to_executor）、
错误处理（unknown tool / invalid args → JSON-RPC error）。

MCP 薄层对模型的意义：任何支持 MCP 的 agent harness（Claude Desktop、
agent-zero、cli-bridge 等）只需 `longtask-mcp` 一个 stdio 入口就能让模型
发现并使用协议，无需自己解析 CLI。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.wait_budget import budget

pytestmark = pytest.mark.integration


def test_mcp_request_id_is_stable_when_omitted() -> None:
    from longtask.mcp_server import _mcp_request_id
    from longtask.rpc.methods import Method

    args = {"contract_id": "lt-idempotent", "revision": 1}
    first = _mcp_request_id(Method.CONTRACT_APPROVE, args)
    second = _mcp_request_id(Method.CONTRACT_APPROVE, dict(args))
    assert first == second
    assert first.startswith("mcp:contract/approve:")
    assert _mcp_request_id(Method.CONTRACT_PATCH, args) != first
    assert (
        _mcp_request_id(Method.CONTRACT_APPROVE, {**args, "request_id": "user-key"}) == "user-key"
    )


def test_mcp_prepare_retry_without_request_id_is_idempotent(tmp_path: Path) -> None:
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.mcp_server import tool_prepare_contract
    from longtask.persistence.store import StoreConfig, connect, ensure_schema

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    args = {
        "title": "stable retry",
        "objective": "same MCP request must not create duplicate contracts",
        "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "acceptance_standard": "contract exists",
        "acceptance_checks": ["contract exists"],
    }
    ctx = {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path}
    try:
        first = tool_prepare_contract(args, ctx)
        second = tool_prepare_contract(dict(args), ctx)
        assert first == second
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 1
    finally:
        conn.close()


def test_mcp_prepare_rejects_boolean_workload(tmp_path: Path) -> None:
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.mcp_server import tool_prepare_contract
    from longtask.persistence.store import StoreConfig, connect, ensure_schema
    from longtask.rpc.errors import ErrorCode, RpcError

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    args = {
        "title": "invalid workload",
        "objective": "boolean workload must be rejected",
        "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "acceptance_standard": "contract exists",
        "acceptance_checks": ["contract exists"],
        "workload_initial_hours": True,
    }
    try:
        with pytest.raises(RpcError) as exc_info:
            tool_prepare_contract(
                args, {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path}
            )
        assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
    finally:
        conn.close()


def test_mcp_prepare_rejects_string_acceptance_checks(tmp_path: Path) -> None:
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.mcp_server import tool_prepare_contract
    from longtask.persistence.store import StoreConfig, connect, ensure_schema
    from longtask.rpc.errors import ErrorCode, RpcError

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    args = {
        "title": "invalid checks",
        "objective": "checks must be a list",
        "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "acceptance_standard": "contract exists",
        "acceptance_checks": "contract exists",
    }
    try:
        with pytest.raises(RpcError) as exc_info:
            tool_prepare_contract(
                args, {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path}
            )
        assert exc_info.value.code is ErrorCode.VALIDATION_FAILED
    finally:
        conn.close()


def test_mcp_prepare_cannot_impersonate_user_client(tmp_path: Path) -> None:
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.mcp_server import tool_prepare_contract
    from longtask.persistence.events import EventType
    from longtask.persistence.store import StoreConfig, connect, ensure_schema, get_events

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    args = {
        "title": "actor boundary",
        "objective": "MCP identity must remain model",
        "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "acceptance_standard": "contract exists",
        "acceptance_checks": ["contract exists"],
        "client_id": "longtask-cli",
    }
    try:
        result = tool_prepare_contract(
            args, {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path}
        )
        assert result["ok"] is True
        prepared = [e for e in get_events(conn) if e.event_type == EventType.CONTRACT_PREPARED]
        assert prepared
        assert prepared[-1].actor == "model"
    finally:
        conn.close()


def test_mcp_submit_plan_approved_emits_audit_events(tmp_path: Path) -> None:
    """End-to-end: lhgp_submit_plan writes plan/submitted + plan/approved
    when validation passes, and surfaces the verdict in the response.

    5th-round follow-up: MCP-issued contracts can no longer
    self-authorize via the per-contract ``auto_approve`` field;
    the trusted source is the bound Goal's
    ``plan.pre_authorized`` (user-pinned at ``goal/update`` time).
    This test sets that pre-authorization on the Goal so the
    submit-plan path can still auto-approve the plan.
    """
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.mcp_server import tool_prepare_contract, tool_submit_plan
    from longtask.persistence.events import EventType
    from longtask.persistence.store import (
        StoreConfig,
        connect,
        ensure_schema,
        get_events,
        patch_goal,
    )

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    goal_id = "lt-tpa-mcp-plan"
    # Bootstrap the goal via save_contract (which auto-creates
    # the goal row); patch_goal then CAS-updates the plan with
    # the user-pinned pre_authorized scope.  Use a separate
    # contract_id so the timestamp-generated id does not
    # collide with the test contract below.
    tool_prepare_contract(
        {
            "title": "goal bootstrap",
            "objective": "x",
            "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "acceptance_standard": "x",
            "acceptance_checks": ["x"],
            "contract_id": f"{goal_id}-bootstrap",
            "goal_id": goal_id,
        },
        {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path},
    )
    patch_goal(
        conn,
        goal_id=goal_id,
        now=datetime.now(UTC),
        expected_revision=1,
        actor="user",
        plan={
            "pre_authorized": {
                "enabled": True,
                "actions": ["verify acceptance"],
            },
        },
    )
    try:
        prepared = tool_prepare_contract(
            {
                "title": "plan-gate e2e",
                "objective": "verify plan submission lands the right events",
                "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                "acceptance_standard": "plan approved",
                "acceptance_checks": ["plan approved"],
                "contract_id": "lt-tpa-mcp-plan-main",
                "goal_id": goal_id,
            },
            {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path},
        )
        cid = prepared["result"]["contract_id"]
        result = tool_submit_plan(
            {
                "contract_id": cid,
                "steps": [
                    {
                        "step_id": 1,
                        "action": "verify acceptance",
                        "target": "plan approved",
                        "rationale": (
                            "verify acceptance of objective 'plan submitted lands the right events'"
                        ),
                        "expected_outcome": "plan approved",
                    }
                ],
            },
            {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path},
        )
        assert result["approved"] is True
        assert result["step_count"] == 1
        types = [e.event_type for e in get_events(conn, contract_id=cid)]
        assert EventType.PLAN_SUBMITTED in types
        assert EventType.PLAN_APPROVED in types
    finally:
        conn.close()


def test_mcp_submit_plan_rejected_when_rationale_omits_objective(
    tmp_path: Path,
) -> None:
    """End-to-end: rationale missing the objective keyword triggers
    plan/rejected, not plan/approved.

    P2 review (2026-09-08): the validator now uses an any-keyword
    match rather than the old "whole objective string in rationale"
    rule, so the rationale must avoid *all* of the objective's
    non-stop words to be rejected — a single shared word (e.g. just
    "keyword") would be enough to slip through.  Use a rationale
    that is completely unrelated to the objective's vocabulary.
    """
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.mcp_server import tool_prepare_contract, tool_submit_plan
    from longtask.persistence.events import EventType
    from longtask.persistence.store import (
        StoreConfig,
        connect,
        ensure_schema,
        get_events,
    )

    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    try:
        prepared = tool_prepare_contract(
            {
                "title": "plan rejection e2e",
                "objective": "bluebird quartz must reference the objective word",
                "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                "acceptance_standard": "ok",
                "acceptance_checks": ["ok"],
            },
            {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path},
        )
        cid = prepared["result"]["contract_id"]
        result = tool_submit_plan(
            {
                "contract_id": cid,
                "steps": [
                    {
                        "step_id": 1,
                        "action": "verify acceptance",
                        "target": "ok",
                        "rationale": "this rationale deliberately omits every vocabulary term",
                        "expected_outcome": "ok",
                    }
                ],
            },
            {"conn": conn, "registry": ExecutorRegistry(), "root": tmp_path},
        )
        assert result["approved"] is False
        assert any("rationale" in r.lower() for r in result["rejection_reasons"])
        types = [e.event_type for e in get_events(conn, contract_id=cid)]
        assert EventType.PLAN_REJECTED in types
        assert EventType.PLAN_APPROVED not in types
    finally:
        conn.close()


def test_mcp_resume_attempt_writes_resumed_audit_event(tmp_path: Path) -> None:
    """End-to-end: lhgp_resume_attempt reads active.md + handover.md,
    returns a self-contained brief, and writes an attempt/resumed
    audit event.

    P1 review: the resume helper now requires the attempt to be
    recorded under the contract in the DB before it will read the
    on-disk files, so this fixture pre-inserts the matching row.
    """
    from longtask.adapters.registry import ExecutorRegistry
    from longtask.mcp_server import tool_resume_attempt
    from longtask.persistence.events import EventType
    from longtask.persistence.store import (
        StoreConfig,
        connect,
        ensure_schema,
        get_events,
    )

    root = tmp_path
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    cid = "lt-resume-e2e"
    # Stage the markdown files the resume tool reads. handover.md lives
    # at the contract root (one per contract); active.md is per-attempt.
    contract_dir = root / "contracts" / cid
    attempt_dir = contract_dir / "context" / "attempts" / "att-1"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "active.md").write_text(
        "# active snapshot\nobjective: deliver plan gate\n",
        encoding="utf-8",
    )
    (contract_dir / "handover.md").write_text(
        "# handover\nnext_action: continue plan gate work\n",
        encoding="utf-8",
    )
    # Record the attempt so the resume helper's path-binding check
    # passes (P1 review fix: attempt must be in the DB when conn is
    # supplied, otherwise the read is refused as a path-binding
    # mismatch — i.e. a file claiming to belong to a contract the
    # contract has never heard of).
    conn.execute(
        "INSERT INTO attempts "
        "(attempt_id, goal_id, contract_id, contract_revision, role, state, "
        " admitted_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "att-1",
            cid,
            cid,
            1,
            "executor",
            "admitted",
            "2026-09-08T09:00:00+00:00",
            "2026-09-08T09:00:00+00:00",
        ),
    )
    conn.commit()
    try:
        result = tool_resume_attempt(
            {"contract_id": cid, "attempt_id": "att-1"},
            {"conn": conn, "registry": ExecutorRegistry(), "root": root},
        )
        assert result["contract_id"] == cid
        assert result["attempt_id"] == "att-1"
        assert "active snapshot" in result["body"]
        assert "continue plan gate work" in result["body"]
        types = [e.event_type for e in get_events(conn, contract_id=cid)]
        assert EventType.ATTEMPT_RESUMED in types
    finally:
        conn.close()


def _spawn_mcp(data_dir: Path) -> subprocess.Popen[bytes]:
    """启动 longtask-mcp 子进程，stdio 用 bytes 收发。"""
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "longtask.mcp_server", "--data-dir", str(data_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[2],  # 仓库根，src 在 PYTHONPATH 之外
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )


def _roundtrip(
    proc: subprocess.Popen[bytes], method: str, params: Any = None, _id: int = 1
) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None
    req = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
    proc.stdin.flush()
    line = proc.stdout.readline().decode("utf-8").strip()
    return json.loads(line)


def _result_text(resp: dict[str, Any]) -> dict[str, Any]:
    """MCP tools/call 响应：content[0].text 是 JSON 字符串。"""
    assert "result" in resp, f"error response: {resp}"
    text = resp["result"]["content"][0]["text"]
    return json.loads(text)


def _stop_mcp(proc: subprocess.Popen[bytes]) -> None:
    """停止 MCP 子进程并关闭父端管道，避免测试句柄泄漏。"""
    if proc.poll() is None:
        proc.terminate()
    proc.wait(timeout=budget(15.0))
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """每个测试一个临时数据目录（不污染 ~/.longtask）。"""
    d = tmp_path / "mcp-data"
    d.mkdir()
    return d


class TestMCPDiscovery:
    """MCP 协议握手：模型先调 initialize / tools.list 来发现可用工具。"""

    def test_initialize_and_list_tools(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            init = _roundtrip(
                proc,
                "initialize",
                {"protocolVersion": "2024-11-05", "clientInfo": {"name": "test"}},
            )
            assert init["result"]["serverInfo"]["name"] == "longtask-mcp"
            tools = _roundtrip(proc, "tools/list", {}, _id=2)
            names = {t["name"] for t in tools["result"]["tools"]}
            expected = {
                "longtask_health",
                "longtask_doctor",
                "longtask_list_executors",
                "longtask_prepare_contract",
                "longtask_approve_contract",
                "longtask_get_contract",
                "longtask_list_contracts",
                "longtask_attach_to_executor",
            }
            assert expected.issubset(names)
            assert {
                "lhgp_prepare_goal",
                "lhgp_attempt_status",
                "lhgp_interrupt_attempt",
                "lhgp_write_back",
                "lhgp_notifications",
            }.issubset(names)
            assert {"longtask_get_goal", "longtask_list_goals"}.issubset(names)
            assert {"longtask_update_goal", "lhgp_update_goal"}.issubset(names)
            assert {"longtask_advance_goal", "lhgp_advance_goal"}.issubset(names)
            assert {"longtask_next_goal_action", "lhgp_next_goal_action"}.issubset(names)
            assert {"longtask_goal_contract_draft", "lhgp_goal_contract_draft"}.issubset(names)
            # 合同读取/列出此前只挂 longtask_*，补齐规范别名后两个命名空间都要有
            assert {"longtask_get_contract", "lhgp_get_contract"}.issubset(names)
            assert {"longtask_list_contracts", "lhgp_list_contracts"}.issubset(names)
            # 用户触发验收（§12.4）在两个命名空间都要有
            assert {
                "longtask_request_verification",
                "lhgp_request_verification",
            }.issubset(names)
            assert {"longtask_doctor", "lhgp_doctor"}.issubset(names)
            # P6 added 6 new tools: lhgp_submit_evaluation, lhgp_compute_diff,
            # lhgp_evolve_templates, lhgp_portfolio, lhgp_trace,
            # lhgp_deadline_report — 41 + 6 = 47.
            # Plan-mode gate added lhgp_submit_plan — 47 + 1 = 48.
            # attempt/resume entry point added lhgp_resume_attempt — 48 + 1 = 49.
            # 3rd-round review: plan auto-approve added lhgp_plan_signoff
            # — 49 + 1 = 50.
            # 3rd-round follow-up: user-confirm path for CANDIDATE→PASSED
            # added longtask_user_confirm_spec_verdict — 50 + 1 = 51.
            assert len(names) == 51
            by_name = {item["name"]: item for item in tools["result"]["tools"]}
            assert by_name["lhgp_notifications"]["annotations"] == {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            }
            assert by_name["lhgp_interrupt_attempt"]["annotations"] == {
                "readOnlyHint": False,
                "destructiveHint": True,
                "openWorldHint": False,
            }
            for name in (
                "lhgp_health",
                "lhgp_list_executors",
                "lhgp_get_goal",
                "lhgp_list_goals",
            ):
                assert by_name[name]["annotations"] == {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                }
            for name in ("lhgp_approve_goal", "lhgp_attach_executor"):
                assert by_name[name]["annotations"] == {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "openWorldHint": False,
                }

            # Every exposed state-changing tool must advertise its durable
            # side effect so an MCP host can apply its confirmation policy.
            mutating = {
                "longtask_prepare_contract",
                "longtask_approve_contract",
                "longtask_request_verification",
                "longtask_update_goal",
                "longtask_advance_goal",
                "longtask_attach_to_executor",
                "lhgp_prepare_goal",
                "lhgp_approve_goal",
                "lhgp_request_verification",
                "lhgp_update_goal",
                "lhgp_advance_goal",
                "lhgp_attach_executor",
                "lhgp_interrupt_attempt",
                "lhgp_write_back",
            }
            for name in mutating:
                assert by_name[name]["annotations"] == {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "openWorldHint": False,
                }
            read_only = {
                "longtask_health",
                "longtask_doctor",
                "longtask_list_executors",
                "longtask_get_contract",
                "longtask_list_contracts",
                "longtask_get_goal",
                "longtask_list_goals",
                "longtask_next_goal_action",
                "longtask_goal_contract_draft",
                "lhgp_health",
                "lhgp_doctor",
                "lhgp_list_executors",
                "lhgp_get_contract",
                "lhgp_list_contracts",
                "lhgp_get_goal",
                "lhgp_list_goals",
                "lhgp_next_goal_action",
                "lhgp_goal_contract_draft",
                "lhgp_attempt_status",
                "lhgp_notifications",
            }
            for name in read_only:
                assert by_name[name]["annotations"] == {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                }
        finally:
            _stop_mcp(proc)

    def test_canonical_entrypoint_reports_canonical_server_name(self, data_dir: Path) -> None:
        """The installed LHGP entrypoint should not identify itself as the legacy shim."""
        proc = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import sys; sys.argv[0] = 'lhgp-mcp'; "
                "from longtask.mcp_server import main; main()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        )
        try:
            init = _roundtrip(proc, "initialize", {"protocolVersion": "2024-11-05"})
            assert init["result"]["serverInfo"]["name"] == "lhgp-mcp"
        finally:
            _stop_mcp(proc)


class TestMCPHealth:
    def test_health_returns_protocol_info(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            resp = _roundtrip(proc, "tools/call", {"name": "longtask_health", "arguments": {}})
            result = _result_text(resp)
            assert result["status"] == "ok"
            assert "protocol_version" in result
            assert "longtask_prepare_contract" in result["tools"]
            listed = _roundtrip(proc, "tools/list", {}, _id=3)
            assert result["tool_count"] == len(listed["result"]["tools"])
            assert result["tool_count"] == len(result["tools"])
            doctor = _result_text(
                _roundtrip(proc, "tools/call", {"name": "lhgp_doctor", "arguments": {}}, _id=4)
            )
            assert doctor["all_ok"] is True
        finally:
            _stop_mcp(proc)

    def test_doctor_reports_missing_executor_over_mcp(self, data_dir: Path) -> None:
        from longtask.adapters.fake_executor import FAKE_MANIFEST
        from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry

        registry = ExecutorRegistry()
        registry.register(
            RegistryEntry(
                id="missing-cli",
                kind="subprocess",
                launch=LaunchSpec(argv=("definitely-missing-lhgp-cli",)),
                capabilities=FAKE_MANIFEST.capabilities,
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
        registry.save_to_file(data_dir / "registry.json")

        proc = _spawn_mcp(data_dir)
        try:
            doctor = _result_text(
                _roundtrip(proc, "tools/call", {"name": "lhgp_doctor", "arguments": {}})
            )
            assert doctor["all_ok"] is False
            check = next(item for item in doctor["checks"] if item["name"] == "executor_registry")
            assert "executable not found" in check["details"]
        finally:
            _stop_mcp(proc)

    def test_list_executors_rejects_non_boolean_filter(self, data_dir: Path) -> None:
        """Schema 声明 boolean 时，运行时不能把字符串真值化。"""
        proc = _spawn_mcp(data_dir)
        try:
            response = _roundtrip(
                proc,
                "tools/call",
                {
                    "name": "lhgp_list_executors",
                    "arguments": {"enabled_only": "false"},
                },
            )
            assert response["error"]["code"] == -32602
            # R1：dispatch 层 schema 校验先行，错误信息标准化
            msg = response["error"]["message"]
            assert "enabled_only" in msg and "boolean" in msg
        finally:
            _stop_mcp(proc)


class TestMCPLifecycle:
    """AI 工具链完整走一遍：立合同 → 批准 → 拉起执行者（认领 attempt）。"""

    def test_prepare_approve_get_and_attach(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            cid = "lt-20260901-mcp01"
            now = datetime.now(UTC)
            deadline = (now + timedelta(hours=2)).isoformat()

            # 1. 立合同
            resp = _roundtrip(
                proc,
                "tools/call",
                {
                    "name": "longtask_prepare_contract",
                    "arguments": {
                        "title": "MCP e2e 测试",
                        "objective": "让 MCP 走通立合同-批准-执行者认领三步",
                        "deadline_at": deadline,
                        "acceptance_standard": "合同进入 active 且执行者读得到上下文",
                        "acceptance_checks": (
                            "contract_id == lt-mcp-01",
                            "state == active after approve",
                            "执行者读到 active.md 全文",
                        ),
                        "hard_constraints": {
                            "file_effects": {
                                "mode": "workspace-write",
                                "workspace_root": str(data_dir / "ws"),
                            }
                        },
                        "workload_initial_hours": 1.5,
                        "contract_id": cid,
                    },
                },
                _id=10,
            )
            prepared = _result_text(resp)
            view = prepared.get("result", prepared)
            assert view.get("contract_id") == cid

            # 2. 批准是 Principal 决定权（安全审查 RPC-C1）：模型客户端
            # 必须收到 AUTH_FAILED 指引（引导用户在 CLI 执行 approve），
            # 而不是自批准。
            resp = _roundtrip(
                proc,
                "tools/call",
                {
                    "name": "longtask_approve_contract",
                    "arguments": {"contract_id": cid},
                },
                _id=11,
            )
            approved_text = str(resp)  # 错误响应在 resp["error"]，不走 _result_text
            assert "AUTH_FAILED" in approved_text or "Principal" in approved_text, (
                f"model self-approval was not rejected: {approved_text}"
            )

            # 用户从 CLI 批准（actor=user 的受信通道）
            from longtask import PROTOCOL_VERSION as _PV
            from longtask.persistence.store import StoreConfig, connect, ensure_schema
            from longtask.rpc.handlers.contract import handle_contract_approve
            from longtask.rpc.methods import Method
            from longtask.rpc.server import RequestEnvelope

            conn_u = connect(StoreConfig(db_path=data_dir / "state.db"))
            ensure_schema(conn_u)
            env_u = RequestEnvelope(
                method=Method.CONTRACT_APPROVE,
                request_id="req-user-approve",
                client_id="longtask-cli",
                protocol_version=_PV,
                params={"contract_id": cid},
            )
            approved = handle_contract_approve(env_u, conn=conn_u, now=now)
            conn_u.close()
            assert approved["ok"] is True

            # 3. 查询状态
            resp = _roundtrip(
                proc,
                "tools/call",
                {"name": "longtask_get_contract", "arguments": {"contract_id": cid}},
                _id=12,
            )
            view = _result_text(resp)
            # 视图里 state 应为 active
            state = view.get("state") or view.get("result", {}).get("state")
            assert state == "active", f"expected active, got {state}: {view}"

            # 4. 执行者认领：先手工给 attempt 一个 lease + 写入
            # （完整执行者收尾链路在 AttemptRunner 测试里覆盖；这里验证
            # MCP 工具能读 context/snapshot 而非写回——纯读 path）
            from longtask.persistence.store import (
                acquire_lease,
                connect,
                ensure_schema,
            )

            conn = connect(
                __import__("longtask.persistence.store", fromlist=["StoreConfig"]).StoreConfig(
                    db_path=data_dir / "state.db"
                )
            )
            ensure_schema(conn)
            aid = "att-mcp-test"
            # O4 修复后 attempt_status 对未知 attempt 报 UNKNOWN_ATTEMPT，
            # 租约必须对应真实的 attempts 行。
            conn.execute(
                "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state,"
                " admitted_at, contract_revision, updated_at)"
                " VALUES (?, ?, ?, 'executor', 'running', ?, 1, ?)",
                (aid, cid, cid, now.isoformat(), now.isoformat()),
            )
            conn.commit()
            acquire_lease(
                conn,
                contract_id=cid,
                holder_attempt_id=aid,
                expected_generation=0,
                heartbeat_at=now,
                timeout=timedelta(minutes=30),
            )
            conn.close()

            # 现在通过 MCP 走一轮 daemon tick（间接通过 attach_to_executor 走 status）
            resp = _roundtrip(
                proc,
                "tools/call",
                {
                    "name": "longtask_attach_to_executor",
                    "arguments": {
                        "contract_id": cid,
                        "attempt_id": aid,
                        # 不带 report_state = 仅读 attempt/status + 上下文快照
                    },
                },
                _id=13,
            )
            attached = _result_text(resp)
            assert attached["status"]["lease"]["holder_attempt_id"] == aid
            assert attached["status"]["lease"]["is_alive"] is True
            # §4.1 上下文快照：fixture 未派工，路径只回 hint（无 active.md）
            snapshot = attached["snapshot"]
            assert "hint" in snapshot
            assert snapshot.get("active_content") is None
        finally:
            _stop_mcp(proc)


class TestMCPErrors:
    """错误处理：未知工具 / 缺必填 / RPC 错误都按 JSON-RPC 规范回 error 对象。"""

    def test_unknown_tool_returns_error(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            resp = _roundtrip(
                proc,
                "tools/call",
                {"name": "longtask_nonexistent", "arguments": {}},
            )
            assert "error" in resp
            assert resp["error"]["code"] == -32602
        finally:
            _stop_mcp(proc)

    def test_missing_required_argument(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            resp = _roundtrip(
                proc,
                "tools/call",
                {"name": "longtask_get_contract", "arguments": {}},
            )
            assert "error" in resp
            assert resp["error"]["code"] == -32602
        finally:
            _stop_mcp(proc)

    def test_non_object_arguments_return_invalid_params(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            resp = _roundtrip(
                proc,
                "tools/call",
                {"name": "lhgp_notifications", "arguments": ["unexpected"]},
            )
            assert resp["error"]["code"] == -32602
            assert "arguments" in resp["error"]["message"]
        finally:
            _stop_mcp(proc)

    def test_unknown_notification_status_returns_invalid_params(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            resp = _roundtrip(
                proc,
                "tools/call",
                {
                    "name": "lhgp_notifications",
                    "arguments": {"status": "queued"},
                },
            )
            assert resp["error"]["code"] == -32602
            assert "unknown notification status" in resp["error"]["message"]
        finally:
            _stop_mcp(proc)

    def test_boolean_notification_limit_returns_invalid_params(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            resp = _roundtrip(
                proc,
                "tools/call",
                {"name": "lhgp_notifications", "arguments": {"limit": True}},
            )
            assert resp["error"]["code"] == -32602
            # R1：dispatch 层 schema 校验先行
            msg = resp["error"]["message"]
            assert "limit" in msg and "integer" in msg
        finally:
            _stop_mcp(proc)

    def test_non_boolean_include_payload_returns_invalid_params(self, data_dir: Path) -> None:
        """字符串 false 不能意外打开通知 payload，避免上下文泄露。"""
        proc = _spawn_mcp(data_dir)
        try:
            resp = _roundtrip(
                proc,
                "tools/call",
                {
                    "name": "lhgp_notifications",
                    "arguments": {"include_payload": "false"},
                },
            )
            assert resp["error"]["code"] == -32602
            assert (
                "include_payload" in resp["error"]["message"]
                and "boolean" in resp["error"]["message"]
            )
        finally:
            _stop_mcp(proc)

    def test_non_string_notification_filters_return_invalid_params(self, data_dir: Path) -> None:
        proc = _spawn_mcp(data_dir)
        try:
            for arguments, expected in (
                ({"status": ["pending"]}, "status"),
                ({"goal_id": 42}, "goal_id"),
            ):
                resp = _roundtrip(
                    proc,
                    "tools/call",
                    {"name": "lhgp_notifications", "arguments": arguments},
                )
                assert resp["error"]["code"] == -32602
                assert expected in resp["error"]["message"]
        finally:
            _stop_mcp(proc)


# ── 5th-round follow-up: end-to-end MCP submit-and-leave
# with Goal-level pre_authorized + execution_config
# ────────────────────────────────────────────────────────


def test_mcp_submit_and_leave_e2e_with_goal_pre_authorized_and_execution_config(
    tmp_path: Path,
) -> None:
    """MCP submit-and-leave happy path: a single model
    call to ``lhgp_prepare_contract`` + ``lhgp_submit_plan``
    lands a contract that the daemon runs to completion
    without a human follow-up — because the bound Goal's
    ``plan.pre_authorized`` covers the model's claimed
    action scope (with ``wildcard=True`` for the
    no-claim MCP path) and the model has supplied a
    complete draft (workspace + executor_grant).

    5th-round follow-up: the previous flow let the model
    self-authorize via the per-contract ``auto_approve``;
    the trusted source is now ``Goal.plan.pre_authorized``
    (user-pinned).  This test exercises the full MCP
    path end-to-end (prepare → submit_plan → daemon tick
    → dispatch → executor → verifier → contract COMPLETE)
    so a regression in any step is caught.

    The reviewer's previous-round test only asserted the
    PLAN_APPROVED event landed; this version runs the
    daemon to actual completion so a regression in the
    chain (e.g. the plan-approval-migration fix) is
    caught at the end-state, not just the audit event.
    """
    from datetime import UTC, datetime, timedelta

    from lhgp.contracts.acceptance import Acceptance
    from lhgp.contracts.budget import Budget
    from lhgp.contracts.contract_draft import ContractDraft
    from longtask.cli.daemon import run_daemon_tick
    from longtask.cli.runner import AttemptRunner
    from longtask.persistence.events import EventType
    from longtask.persistence.events_query import get_events
    from longtask.persistence.store import (
        StoreConfig,
        connect,
        ensure_schema,
        patch_goal,
        save_contract,
    )

    root = tmp_path / "data"
    root.mkdir()
    workspace = root / "ws"
    workspace.mkdir()

    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    try:
        goal_id = "lt-mcp-sl-e2e"
        # Bootstrap the goal row (auto-creates via save_contract).
        # The deadline is short (1h) so the urgency tier
        # reaches RESPAWN on the test's tick (workload=4h,
        # time-left=1h → u=4 → RESPAWN).
        save_contract(
            conn,
            draft=ContractDraft(
                title="goal bootstrap",
                objective="x",
                deadline_at=datetime.now(UTC) + timedelta(hours=1),
                hard_constraints={},
                acceptance=Acceptance(standard="s", checks=("c1",)),
                workload_initial_hours=4.0,
                budget=Budget(5, 1, 1, 30, 1048576, 2),
            ),
            contract_id=f"{goal_id}-bootstrap",
            now=datetime.now(UTC),
            actor="user",
            goal_id=goal_id,
        )
        # Pin pre_authorized on the Goal — the user-side
        # grant that the MCP submit-plan path reads.
        # ``wildcard=True`` so the no-claim MCP path (parse
        # strip removes ``auto_approve`` from the draft) is
        # still covered by a user-pinned "I trust this whole
        # goal" sign-off.  The contract's plan will pass the
        # gate and the contract's DRAFTED→ACTIVE auto-promote
        # will fire on the next tick.
        patch_goal(
            conn,
            goal_id=goal_id,
            now=datetime.now(UTC),
            expected_revision=1,
            actor="user",
            plan={
                "pre_authorized": {
                    "enabled": True,
                    "wildcard": True,
                    "actions": ["verify acceptance", "write file"],
                },
            },
        )
        from longtask.adapters.fake_executor import FAKE_MANIFEST
        from longtask.adapters.registry import (
            CostHint,
            ExecutorRegistry,
            LaunchSpec,
            RegistryEntry,
        )
        from longtask.mcp_server import tool_prepare_contract, tool_submit_plan
        from longtask.persistence.store import get_contract

        registry = ExecutorRegistry()
        registry.register(
            RegistryEntry(
                id="exec-mcp-sl",
                kind="fake",
                launch=LaunchSpec(),
                capabilities=FAKE_MANIFEST.capabilities,
                limits={"max_concurrent_attempts": 1},
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )

        # 1) lhgp_prepare_contract — the model supplies a
        # full draft (workspace + executor grant inline).
        # The MCP path strips any client-side auto_approve
        # and binds the contract to the Goal.
        prepared = tool_prepare_contract(
            {
                "title": "submit-and-leave happy path",
                "objective": "verify acceptance — write the verdict block",
                "deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                "acceptance_standard": "plan approved",
                "acceptance_checks": ["plan approved"],
                "goal_id": goal_id,
                "hard_constraints": {
                    "file_effects": {
                        "mode": "workspace-write",
                        "workspace_root": str(workspace),
                    }
                },
                "authority": {
                    "executor_policy": "explicit_allow",
                    "executors": [
                        {
                            "executor_id": "exec-mcp-sl",
                            "models": ["*"],
                            "roles": ["executor"],
                        },
                        {
                            "executor_id": "ver-mcp-sl",
                            "models": ["*"],
                            "roles": ["verifier"],
                        },
                    ],
                },
            },
            {"conn": conn, "registry": registry, "root": tmp_path},
        )
        assert prepared.get("ok") is True
        cid = prepared["result"]["contract_id"]

        view = get_contract(conn, cid)
        assert view is not None
        # 2) The MCP path stripped the model's
        # auto_approve (if any) — the contract has no
        # self-sign.
        assert view.draft.auto_approve.enabled is False, (
            f"MCP-prepared contract must NOT carry the model's "
            f"auto_approve claim; got {view.draft.auto_approve!r}"
        )
        # 3) lhgp_submit_plan — the plan claims the
        # ``verify acceptance`` action which is inside the
        # Goal's pre_authorized scope.  The MCP path's
        # submit-plan check (now reading the Goal's grant)
        # auto-approves the plan.
        result = tool_submit_plan(
            {
                "contract_id": cid,
                "steps": [
                    {
                        "step_id": 1,
                        "action": "verify acceptance",
                        "target": "plan approved",
                        "rationale": (
                            "verify acceptance of objective 'submit-and-leave happy path'"
                        ),
                        "expected_outcome": "plan approved",
                    }
                ],
            },
            {"conn": conn, "registry": registry, "root": tmp_path},
        )
        assert result["approved"] is True, (
            f"MCP submit_plan must auto-approve when the claimed "
            f"action is inside the Goal's pre_authorized scope; "
            f"got {result!r}"
        )
        # 4) The auto-approved plan lands a PLAN_APPROVED
        # event in the audit log.
        events = get_events(conn, contract_id=cid)
        assert EventType.PLAN_APPROVED in [e.event_type for e in events], (
            f"MCP submit-and-leave must emit PLAN_APPROVED; got {[e.event_type for e in events]}"
        )
        # The plan-approval is at revision=1; the next tick
        # bumps the contract to revision=2 via auto-promote;
        # the migration in update_contract_state must keep the
        # PLAN_APPROVED's contract_revision aligned so the
        # gate still binds.  (Verified post-tick below.)

        # 5) Run the daemon tick.  The contract is still
        # DRAFTED at this point; the auto-approve primitive
        # promotes it to ACTIVE on the next tick (Goal
        # pre_authorized is in effect), the dispatch picks
        # the fake executor, the executor "succeeds", the
        # runner auto-dispatches an independent verifier
        # (registered below as ``ver-mcp-sl``), the
        # verifier also "succeeds", and the contract reaches
        # COMPLETE on the next judge tick.
        #
        # Two registry entries are required: a single entry
        # would force the runner to exclude the executor
        # itself from the verifier pool (§5.2 独立核对),
        # leaving no verifier candidate and the runner
        # would escalate to user.  The MCP-prepared contract
        # declares both entries in its authority.executors
        # binding so ``match_candidates(requested_role=
        # "verifier")`` returns ``ver-mcp-sl`` only.
        from longtask.adapters.fake_executor import FAKE_MANIFEST
        from longtask.adapters.registry import (
            CostHint,
            ExecutorRegistry,
            LaunchSpec,
            RegistryEntry,
        )

        registry = ExecutorRegistry()
        registry.register(
            RegistryEntry(
                id="exec-mcp-sl",
                kind="fake",
                launch=LaunchSpec(),
                capabilities=FAKE_MANIFEST.capabilities,
                limits={"max_concurrent_attempts": 1},
                cost_hint=CostHint.LOW,
                enabled=True,
            )
        )
        registry.register(
            RegistryEntry(
                id="ver-mcp-sl",
                kind="fake",
                launch=LaunchSpec(),
                capabilities=FAKE_MANIFEST.capabilities,
                limits={"max_concurrent_attempts": 1},
                cost_hint=CostHint.MEDIUM,
                enabled=True,
            )
        )
        # Inject the FakeExecutor under both ids; the
        # default script is "succeed" so executor + verifier
        # both report a green outcome.  The verifier stdout
        # carries a ``lhgp-verdict`` pass block so the
        # runner's collect path recognises the structured
        # acceptance evidence (otherwise the contract
        # would land in ``acceptance=FAILED`` per §12.4
        # verification-evidence-missing).
        from longtask.adapters.fake_executor import FakeAttemptScript, FakeExecutor

        _verifier_stdout = (
            "fake verifier echo\n"
            "```lhgp-verdict\n"
            + json.dumps(
                {
                    "verdict": "succeeded",
                    "checks": [
                        {
                            "check_id": "plan approved",
                            "outcome": "pass",
                            "source": "fake-verifier",
                        }
                    ],
                }
            )
            + "\n```\n"
        )
        fake = FakeExecutor(
            default_script=FakeAttemptScript(outcome="succeeded", stdout=_verifier_stdout)
        )
        runner = AttemptRunner(tmp_path / "data", conn, registry)
        runner._adapters["exec-mcp-sl"] = fake
        runner._adapters["ver-mcp-sl"] = fake

        # The contract's revision was 1; submit_plan wrote
        # PLAN_APPROVED at revision=1.  The auto-activate
        # tick will bump the contract to revision=2 and the
        # plan-approval migration (this round's fix) must
        # keep the PLAN_APPROVED aligned.
        #
        # ``run_daemon_tick`` does NOT call the
        # auto-approve sweep (that's the daemon main loop's
        # job); for the test we call it directly so the
        # contract transitions DRAFTED→ACTIVE before the
        # dispatch loop.
        from longtask.persistence.store import (
            auto_approve_drafted_contract,
            get_contract,
        )

        pre_tick_view = get_contract(conn, cid)
        assert pre_tick_view is not None
        promoted = auto_approve_drafted_contract(conn, pre_tick_view, datetime.now(UTC))
        assert promoted is True, (
            f"auto_approve_drafted_contract must promote the "
            f"DRAFTED contract to ACTIVE; got promoted={promoted}, "
            f"contract.auto_approve={pre_tick_view.draft.auto_approve!r}"
        )
        tick = run_daemon_tick(tmp_path / "data", conn, registry, now=datetime.now(UTC))
        assert tick["attempts_started"], f"daemon tick must dispatch the contract; got {tick!r}"
        started = tick["attempts_started"][0]
        assert runner.start_attempt(
            datetime.now(UTC) + timedelta(seconds=1),
            contract_id=started["contract_id"],
            attempt_id=started["attempt_id"],
            executor_id=started["executor_id"],
        )
        # Drain the runner until the executor attempt is
        # collected and the runner auto-dispatches the
        # verifier.  Two passes cover the executor→verifier
        # chain for the FakeExecutor (its observe() reports
        # the terminal outcome on the first poll).
        import time

        deadline = time.monotonic() + budget(60.0)
        while time.monotonic() < deadline and runner._running:
            time.sleep(0.05)
            runner.poll_attempts(datetime.now(UTC) + timedelta(seconds=2))
        assert not runner._running, (
            f"executor+verifier did not reach terminal; running={list(runner._running.keys())}"
        )

        # 6) The judge tick turns the verifier's success
        # into a contract COMPLETE.
        from longtask.cli.tick import _judge_verifier_outcomes
        from longtask.contracts.schema import ContractState

        _judge_verifier_outcomes(tmp_path / "data", conn, datetime.now(UTC) + timedelta(seconds=3))
        view = get_contract(conn, cid)
        assert view is not None
        assert view.state == ContractState.COMPLETE, (
            f"MCP submit-and-leave must drive the contract to "
            f"COMPLETE; got state={view.state!r}, "
            f"acceptance={view.acceptance_status.value!r}"
        )
        assert view.acceptance_status.value == "passed"
        # The end-state audit chain is intact: a single
        # PLAN_APPROVED landed (submit_plan), the contract
        # auto-promoted through DRAFTED→ACTIVE, the executor
        # succeeded, the verifier succeeded, the judge
        # promoted ACTIVE→COMPLETE.  A regression in any of
        # these (e.g. the plan-approval-migration fix) would
        # break the COMPLETE state — the dedicated
        # ``test_plan_approval_migration`` unit tests pin
        # the migration SQL behaviour.
        post_events = get_events(conn, contract_id=cid)
        event_types = [str(e.event_type) for e in post_events]
        for required in (
            EventType.PLAN_APPROVED.value,
            EventType.ATTEMPT_STARTED.value,
            EventType.ATTEMPT_SUCCEEDED.value,
            EventType.CONTRACT_COMPLETED.value,
        ):
            assert required in event_types, f"audit chain missing {required!r}; got {event_types}"
    finally:
        conn.close()


def test_lhgp_prepare_contract_schema_declares_spec_and_spec_hash() -> None:
    """7th-round P2: ``longtask_prepare_contract`` (and its
    ``lhgp_prepare_contract`` alias) must declare
    ``spec`` and ``spec_hash`` in their JSON Schema.  The
    reviewer's real-MCP request returned
    ``invalid arguments: unknown parameter: spec`` because
    the function accepted the args but the schema
    validator (the same code path the stdio transport
    uses) rejected them.  Without a schema declaration
    the plan-gate + CANDIDATE path is unreachable via
    the public tool surface.
    """
    from longtask.mcp_server import TOOLS

    for tool_name in ("longtask_prepare_contract", "lhgp_prepare_contract"):
        if tool_name not in TOOLS:
            continue
        _, metadata = TOOLS[tool_name]
        properties = metadata["inputSchema"]["properties"]
        assert "spec" in properties, (
            f"{tool_name} inputSchema must declare 'spec' (7th-round P2 fix); "
            f"got properties={sorted(properties)}"
        )
        assert "spec_hash" in properties, (
            f"{tool_name} inputSchema must declare 'spec_hash' (7th-round P2 fix); "
            f"got properties={sorted(properties)}"
        )
        assert properties["spec"]["type"] == "object"
        assert properties["spec_hash"]["type"] == "string"
