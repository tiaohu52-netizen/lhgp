"""真实 subprocess adapter 的 fail → repair → reverify 闭环。

该场景不依赖任何外部 CLI 或凭据：执行器和 verifier 都是由
``SubprocessAdapter`` 拉起的独立 Python 进程，因此验证的是协议的真实
进程边界、stdout 判定块、租约回收和新 verifier 派生，而不是 FakeExecutor
的脚本注入。
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.cli.daemon import run_daemon_tick
from longtask.cli.runner import AttemptRunner
from longtask.cli.tick import _judge_verifier_outcomes
from longtask.contracts.authority import Authority, AuthorityBinding
from longtask.contracts.schema import Acceptance, Budget, ContractDraft, ContractState, Enforcement
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from tests.wait_budget import budget

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _caps() -> Capabilities:
    return Capabilities(
        spawn=True,
        observe=True,
        cancel=True,
        notify=False,
        followup=False,
        steer=False,
        interrupt=True,
        context="optional",
        sandbox=SandboxCapability(
            file_effects="workspace-write",
            network="unsupported",
            process="unsupported",
            enforcement=Enforcement.PARTIAL,
        ),
        acceptance_evidence=True,
    )


def _registry() -> ExecutorRegistry:
    registry = ExecutorRegistry()
    # The executor increments a persistent counter: first run is intentionally
    # red, subsequent runs are the repair attempt and write a green artifact.
    executor_code = (
        "from pathlib import Path; "
        "p=Path('runs.txt'); n=int(p.read_text()) if p.exists() else 0; "
        "p.write_text(str(n+1)); Path('result.txt').write_text('good' if n else 'bad')"
    )
    verifier_code = (
        "import json; from pathlib import Path; "
        "ok=Path('result.txt').read_text() == 'good'; "
        "print('```lhgp-verdict\\n' + json.dumps({'verdict': 'succeeded' if ok else 'failed'}) "
        "+ '\\n```')"
    )
    repair_executor_code = "from pathlib import Path; Path('result.txt').write_text('good')"
    for entry_id, code in (
        ("exec-real", executor_code),
        ("exec-repair", repair_executor_code),
        ("ver-real", verifier_code),
    ):
        registry.register(
            RegistryEntry(
                id=entry_id,
                kind="subprocess",
                launch=LaunchSpec(
                    argv=(sys.executable, "-c", code),
                    env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
                ),
                capabilities=_caps(),
                limits={"max_concurrent_attempts": 1},
                cost_hint=(CostHint.HIGH if entry_id == "exec-repair" else CostHint.LOW),
                enabled=True,
            )
        )
    return registry


def _setup(root: Path) -> tuple[Any, str]:
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    # The deterministic check is deliberately an absolute interpreter path so
    # it is executable in the daemon environment as well as this test process.
    (workspace / "verify_gate.py").write_text(
        "from pathlib import Path; "
        "raise SystemExit(0 if Path('result.txt').read_text() == 'good' else 1)",
        encoding="utf-8",
    )
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="真实 subprocess 修复闭环",
            objective="执行器修复产物后由独立 verifier 重新验收",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {"mode": "workspace-write", "workspace_root": str(workspace)}
            },
            acceptance=Acceptance(
                standard="验证脚本通过",
                checks=(
                    {
                        "kind": "command-exit-zero",
                        "target": sys.executable,
                        "args": {"argv": ("verify_gate.py",)},
                    },
                ),
            ),
            # Keep enough remaining work that the first decision is actionable
            # at NOW (the scheduler intentionally does not spin during a
            # low-urgency idle window).
            workload_initial_hours=4.0,
            budget=Budget(
                max_dispatches=4,
                max_escalations=2,
                max_concurrent_attempts=1,
                max_attempt_minutes=10,
                max_output_bytes=1048576,
                verification_attempts_reserved=3,
            ),
            authority=Authority(
                executor_policy="explicit_allow",
                executors=(
                    AuthorityBinding("exec-real", ("*",), ("executor",)),
                    AuthorityBinding("exec-repair", ("*",), ("executor",)),
                    AuthorityBinding("ver-real", ("*",), ("verifier",)),
                ),
            ),
        ),
        contract_id="lt-real-subprocess-loop",
        now=NOW,
    )
    update_contract_state(
        conn, contract_id="lt-real-subprocess-loop", new_state=ContractState.ACTIVE, now=NOW
    )
    return conn, "lt-real-subprocess-loop"


def _wait_and_poll(runner: AttemptRunner, now: datetime) -> None:
    deadline = time.monotonic() + budget(60.0)
    while time.monotonic() < deadline and runner._running:
        time.sleep(0.05)
        runner.poll_attempts(now)
    assert not runner._running, "real subprocess attempt did not reach a terminal state"


def test_real_subprocess_fail_repair_reverify(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    conn, contract_id = _setup(root)
    registry = _registry()
    try:
        runner = AttemptRunner(root, conn, registry)

        # Round 1: real executor writes bad output; real verifier must fail.
        first_tick = run_daemon_tick(root, conn, registry, now=NOW)
        first = first_tick["attempts_started"][0]
        assert runner.start_attempt(
            NOW,
            contract_id=first["contract_id"],
            attempt_id=first["attempt_id"],
            executor_id=first["executor_id"],
        )
        _wait_and_poll(runner, NOW + timedelta(seconds=1))
        _wait_and_poll(runner, NOW + timedelta(seconds=2))
        _judge_verifier_outcomes(root, conn, NOW + timedelta(seconds=3))
        assert get_contract(conn, contract_id).state == ContractState.ACTIVE

        # Round 2: disable the first executor and let a different authorized
        # CLI take over.  This is the actual multi-agent handoff, not merely a
        # second attempt in the same adapter/session.
        assert registry.set_enabled("exec-real", False)
        runner.replace_registry(registry)
        second_tick = run_daemon_tick(root, conn, registry, now=NOW)
        second = second_tick["attempts_started"][0]
        assert second["executor_id"] == "exec-repair"
        assert runner.start_attempt(
            NOW,
            contract_id=second["contract_id"],
            attempt_id=second["attempt_id"],
            executor_id=second["executor_id"],
        )
        _wait_and_poll(runner, NOW + timedelta(seconds=1))
        _wait_and_poll(runner, NOW + timedelta(seconds=2))
        _judge_verifier_outcomes(root, conn, NOW + timedelta(seconds=3))

        assert get_contract(conn, contract_id).state == ContractState.COMPLETE
        assert (root / "ws" / "result.txt").read_text(encoding="utf-8") == "good"
        verifier_rows = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE goal_id=? AND role='verifier'", (contract_id,)
        ).fetchone()[0]
        assert verifier_rows == 2
    finally:
        conn.close()
