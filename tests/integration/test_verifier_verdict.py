"""SPEC §12.4 通道 2 集成：CLI verifier stdout 判定块进裁决。

dogfood v5 发现 4 的端到端修复验证：headless verifier 无法调
attempt/write-back RPC，其 lhgp-verdict 判定块被 runner 收尾解析，
与协议确定性评估合成（undetermined 由模型填补），最终驱动合同
complete / blocked 裁决。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.fake_executor import FAKE_MANIFEST, FakeAttemptScript, FakeExecutor
from longtask.adapters.registry import (
    CostHint,
    ExecutorRegistry,
    LaunchSpec,
    RegistryEntry,
)
from longtask.cli.tick import _judge_verifier_outcomes
from longtask.contracts.schema import (
    Acceptance,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)

VERDICT_OK = """我独立核验了两条检查。

```lhgp-verdict
{"verdict": "succeeded", "checks": [
  {"check_id": "file-exists:result.txt", "outcome": "pass", "source": "ws/result.txt"},
  {"check_id": "command-exit-zero:python test_all.py", "outcome": "pass",
   "source": "venv python 运行", "details": "All tests passed"}
]}
```
"""

VERDICT_FAIL = """发现问题。

```lhgp-verdict
{"verdict": "failed", "checks": [
  {"check_id": "file-exists:result.txt", "outcome": "pass", "source": "ws/result.txt"},
  {"check_id": "command-exit-zero:python test_all.py", "outcome": "fail",
   "source": "运行测试", "details": "2 tests failed"}
]}
```
"""

NO_VERDICT = "看起来都完成了，我确认这些检查通过。"  # 无判定块


def _setup(tmp_path: Path, *, checks: tuple) -> tuple[Path, Any, str]:
    """两 fake 执行器合同：active + 交付物已存在（file check 可过）。"""
    root = tmp_path / "data"
    ws = root / "ws"
    ws.mkdir(parents=True)
    (ws / "result.txt").write_text("deliverable\n", encoding="utf-8")
    cid = "lt-verdict1"
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="verdict 通道测试",
            objective="验证 stdout 判定块进裁决",
            deadline_at=NOW + timedelta(hours=1),
            hard_constraints={
                "file_effects": {"mode": "workspace-write", "workspace_root": str(ws)}
            },
            acceptance=Acceptance(standard="全部通过", checks=checks),
            workload_initial_hours=1.5,
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
                verification_attempts_reserved=3,
            ),
        ),
        contract_id=cid,
        now=NOW,
    )
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    return root, conn, cid


def _run_cli_verifier(root: Path, conn: Any, cid: str, stdout: str) -> None:
    """派一个带 stdout 的 fake verifier 并走 tick 裁决。

    直接构造 AttemptRunner，向其 adapter 注入脚本（模拟 CLI verifier
    把结论留在 stdout 的形态），再 _judge_verifier_outcomes 裁决。
    """
    from longtask.cli.runner import AttemptRunner

    registry = ExecutorRegistry()
    for exec_id in ("exec-a", "exec-b"):
        registry.register(
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
    runner = AttemptRunner(root, conn, registry)
    ok = runner._dispatch_verifier(NOW, contract_id=cid, executor_id="exec-a")
    assert ok, "verifier dispatch failed"
    # 找到 verifier attempt，向其脚本注入 stdout
    ver_id = conn.execute(
        "SELECT attempt_id FROM attempts WHERE role='verifier' AND goal_id=?"
        " ORDER BY admitted_at DESC LIMIT 1",
        (cid,),
    ).fetchone()[0]
    adapter = runner._adapters["exec-b"]
    assert isinstance(adapter, FakeExecutor)
    adapter._scripts[ver_id] = FakeAttemptScript(outcome="succeeded", stdout=stdout)
    runner.poll_attempts(NOW + timedelta(seconds=1))


def _verdict_event(conn: Any, cid: str) -> dict:
    import json as _json

    row = conn.execute(
        "SELECT payload_json FROM events WHERE event_type IN"
        " ('attempt/succeeded','attempt/failed') AND attempt_id LIKE 'ver-%'"
        " AND goal_id=? ORDER BY event_id DESC LIMIT 1",
        (cid,),
    ).fetchone()
    return _json.loads(row[0])


def test_verdict_block_fills_undetermined_command_check(tmp_path: Path) -> None:
    """dogfood v5 场景回归：协议跑不了 python（undetermined）→ 模型
    verdict pass 填补 → verifier succeeded → 合同 complete。"""
    checks = (
        {"kind": "file-exists", "target": "result.txt"},
        # python 不在测试进程裸 PATH 的假设不成立，改用一个必 undetermined
        # 的命令（不存在的解释器）来制造协议侧 undetermined
        {"kind": "command-exit-zero", "target": "no-such-interpreter-xyz"},
    )
    root, conn, cid = _setup(tmp_path, checks=checks)
    try:
        _run_cli_verifier(
            root,
            conn,
            cid,
            VERDICT_OK.replace(
                "command-exit-zero:python test_all.py",
                "command-exit-zero:no-such-interpreter-xyz",
            ),
        )
        _judge_verifier_outcomes(root, conn, NOW + timedelta(seconds=2))
        contract = get_contract(conn, cid)
        assert contract.state == ContractState.COMPLETE
        ev = _verdict_event(conn, cid)
        assert ev["state"] == "succeeded"
        assert ev["model_verdict"]["verdict"] == "succeeded"
        by_id = {e["check_id"]: e for e in ev["evidence"]}
        assert by_id["file-exists:result.txt"]["outcome"] == "pass"
        cmd = by_id["command-exit-zero:no-such-interpreter-xyz"]
        assert cmd["outcome"] == "pass"  # 模型填补
        assert cmd["model_outcome"] == "pass"
    finally:
        conn.close()


def test_verdict_fail_keeps_contract_in_repair(tmp_path: Path) -> None:
    """模型 verdict fail → verifier failed → 合同退回 active（repair）。"""
    checks = (
        {"kind": "file-exists", "target": "result.txt"},
        {"kind": "command-exit-zero", "target": "no-such-interpreter-xyz"},
    )
    root, conn, cid = _setup(tmp_path, checks=checks)
    try:
        _run_cli_verifier(
            root,
            conn,
            cid,
            VERDICT_FAIL.replace(
                "command-exit-zero:python test_all.py",
                "command-exit-zero:no-such-interpreter-xyz",
            ),
        )
        _judge_verifier_outcomes(root, conn, NOW + timedelta(seconds=2))
        contract = get_contract(conn, cid)
        assert contract.state == ContractState.ACTIVE
        assert contract.acceptance_status.value == "failed"
        ev = _verdict_event(conn, cid)
        assert ev["state"] == "failed"
    finally:
        conn.close()


def test_no_verdict_block_still_evidence_missing(tmp_path: Path) -> None:
    """无判定块 → 无证据 fail-closed（不猜）——但确定性 file check 仍评估。"""
    checks = ({"kind": "file-exists", "target": "result.txt"},)
    root, conn, cid = _setup(tmp_path, checks=checks)
    try:
        _run_cli_verifier(root, conn, cid, NO_VERDICT)
        ev = _verdict_event(conn, cid)
        assert ev["state"] == "succeeded"  # file check 确定性 pass
        assert ev["model_verdict"] is None
        assert ev["evidence"][0]["model_outcome"] == "absent"
    finally:
        conn.close()


def test_deterministic_fail_not_overridden_by_model_pass(tmp_path: Path) -> None:
    """协议确定性 fail（文件不存在）→ 模型 pass 不得覆盖（防橡皮图章）。"""
    checks = ({"kind": "file-exists", "target": "ghost.txt"},)
    root, conn, cid = _setup(tmp_path, checks=checks)
    try:
        stdout = VERDICT_OK.replace("file-exists:result.txt", "file-exists:ghost.txt")
        _run_cli_verifier(root, conn, cid, stdout)
        _judge_verifier_outcomes(root, conn, NOW + timedelta(seconds=2))
        ev = _verdict_event(conn, cid)
        assert ev["state"] == "failed"  # 确定性 fail 优先
        assert ev["evidence"][0]["outcome"] == "fail"
        assert ev["evidence"][0]["model_outcome"] == "pass"  # 冲突可审计
        contract = get_contract(conn, cid)
        assert contract.state == ContractState.ACTIVE
    finally:
        conn.close()
