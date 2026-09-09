"""真实 daemon 进程重启时的 subprocess reattach 场景。"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.processes import terminate_pid
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.cli.daemon import halt_daemon
from longtask.cli.main import main
from longtask.contracts.schema import Acceptance, Budget, ContractDraft, ContractState, Enforcement
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    get_events,
    save_contract,
    update_contract_state,
)
from tests.wait_budget import budget

pytestmark = pytest.mark.integration


def _registry() -> ExecutorRegistry:
    registry = ExecutorRegistry()
    code = (
        "import pathlib,time; time.sleep(30); "
        "pathlib.Path('restart-survived.txt').write_text('ok', encoding='utf-8')"
    )
    registry.register(
        RegistryEntry(
            id="restart-exec",
            kind="subprocess",
            launch=LaunchSpec(
                argv=(sys.executable, "-c", code),
                env_allowlist=("PATH", "SYSTEMROOT", "TEMP", "TMP"),
            ),
            capabilities=Capabilities(
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
            ),
            limits={"max_concurrent_attempts": 1},
            cost_hint=CostHint.LOW,
            enabled=True,
        )
    )
    return registry


def _setup(root: Path, now: datetime) -> str:
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    conn = connect(StoreConfig(db_path=root / "state.db"))
    try:
        ensure_schema(conn)
        contract_id = "lt-daemon-restart"
        save_contract(
            conn,
            ContractDraft(
                title="daemon restart reattach",
                objective="外部进程跨 daemon 重启持续运行",
                deadline_at=now + timedelta(hours=2),
                hard_constraints={
                    "file_effects": {
                        "mode": "workspace-write",
                        "workspace_root": str(workspace),
                    }
                },
                acceptance=Acceptance(
                    standard="产物存在", checks=("file-exists:restart-survived.txt",)
                ),
                workload_initial_hours=4.0,
                budget=Budget(
                    max_dispatches=3,
                    max_escalations=2,
                    max_concurrent_attempts=1,
                    max_attempt_minutes=2,
                    max_output_bytes=1048576,
                ),
            ),
            contract_id=contract_id,
            now=now,
        )
        update_contract_state(
            conn, contract_id=contract_id, new_state=ContractState.ACTIVE, now=now
        )
        return contract_id
    finally:
        conn.close()


def _events(root: Path, contract_id: str) -> list[str]:
    conn = connect(StoreConfig(db_path=root / "state.db"))
    try:
        return [str(event.event_type) for event in get_events(conn, contract_id=contract_id)]
    finally:
        conn.close()


def _wait_for(predicate: object, timeout: float = 45.0) -> bool:
    """Poll ``predicate`` until it is true or the budget runs out.

    Everything waited on here involves a real daemon/subprocess, so the
    default is a ceiling for a loaded machine rather than an expected delay.
    """
    end = time.monotonic() + budget(timeout)
    while time.monotonic() < end:
        if callable(predicate) and predicate():
            return True
        time.sleep(0.1)
    return False


def test_real_daemon_restart_reattaches_live_subprocess(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """start → 外部 run → stop → 新 daemon reattach，期间不重复派工。"""
    root = tmp_path / "data"
    root.mkdir()
    now = datetime.now(UTC)
    contract_id = _setup(root, now)
    _registry().save_to_file(root / "registry.json")

    assert main(["--data-dir", str(root), "start", "--interval", "0.1"]) == 0
    capsys.readouterr()
    first_attempt_id: str | None = None
    external_pid: int | None = None
    try:

        def first_started() -> bool:
            nonlocal first_attempt_id, external_pid
            conn = connect(StoreConfig(db_path=root / "state.db"))
            try:
                row = conn.execute(
                    "SELECT attempt_id, external_run_id FROM attempts "
                    "WHERE goal_id=? AND role='executor' ORDER BY admitted_at LIMIT 1",
                    (contract_id,),
                ).fetchone()
                if row is None or row[1] is None:
                    return False
                first_attempt_id = str(row[0])
                external_pid = int(row[1])
                return True
            finally:
                conn.close()

        assert _wait_for(first_started), "first daemon did not spawn an external process"
        assert external_pid is not None
        assert main(["--data-dir", str(root), "stop"]) == 0
        capsys.readouterr()

        # The daemon stop is a control-plane restart, not an instruction to
        # terminate the external attempt.  It must still be alive briefly.
        assert not (root / "ws" / "restart-survived.txt").exists()

        assert main(["--data-dir", str(root), "start", "--interval", "0.1"]) == 0
        capsys.readouterr()

        def reattached() -> bool:
            return EventType.RECONCILE_REATTACHED.value in _events(root, contract_id)

        assert _wait_for(reattached), "second daemon did not reattach the live subprocess"
        assert first_attempt_id is not None
        conn = connect(StoreConfig(db_path=root / "state.db"))
        try:
            executor_count = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE goal_id=? AND role='executor'",
                (contract_id,),
            ).fetchone()[0]
            lease = conn.execute(
                "SELECT holder_attempt_id FROM leases WHERE contract_id=?", (contract_id,)
            ).fetchone()
            assert executor_count == 1
            assert lease is not None and lease[0] == first_attempt_id
            assert get_contract(conn, contract_id).state == ContractState.ACTIVE
        finally:
            conn.close()

        # The second daemon must have adopted the reattached run into its
        # Runner table, not merely renewed it in reconcile.  Cancelling the
        # contract now should terminate the still-sleeping child before it can
        # create its output file.
        assert main(["--data-dir", str(root), "cancel", contract_id]) == 0
        capsys.readouterr()

        def cancelled() -> bool:
            conn = connect(StoreConfig(db_path=root / "state.db"))
            try:
                row = conn.execute(
                    "SELECT state FROM attempts WHERE attempt_id=?", (first_attempt_id,)
                ).fetchone()
                return row is not None and row[0] == "cancelled"
            finally:
                conn.close()

        assert _wait_for(cancelled, timeout=30.0), (
            "reattached attempt was not cancelled by new daemon"
        )
        assert not (root / "ws" / "restart-survived.txt").exists()
    finally:
        halt_daemon(root, grace_seconds=3.0)
        if external_pid is not None:
            terminate_pid(external_pid)
