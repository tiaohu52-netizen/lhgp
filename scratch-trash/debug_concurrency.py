"""Debug script for concurrency test."""

import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft, ContractState, Enforcement
from longtask.adapters.manifest import Capabilities, SandboxCapability
from longtask.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry
from longtask.cli.daemon import run_daemon_tick
from longtask.cli.runner import AttemptRunner
from longtask.persistence.store import (
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from longtask.persistence.types import StoreConfig

with tempfile.TemporaryDirectory() as d:
    root = Path(d) / "data"
    root.mkdir()
    ws1 = root / "ws1"
    ws2 = root / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    NOW = datetime(2026, 9, 8, 9, 0, 0, tzinfo=UTC)
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    for ws, cid in [(ws1, "lt-dbg-c1"), (ws2, "lt-dbg-c2")]:
        (ws / "verify_gate.py").write_text("from pathlib import Path; raise SystemExit(0)")
        draft = ContractDraft(
            title="e2e",
            objective="write ok to result.txt and stop",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={
                "file_effects": {"mode": "workspace-write", "workspace_root": str(ws)}
            },
            acceptance=Acceptance(standard="ok", checks=("file-exists:result.txt",)),
            workload_initial_hours=4.0,
            budget=Budget(
                max_dispatches=1,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=10,
                max_output_bytes=1048576,
            ),
        )
        save_contract(conn, draft, contract_id=cid, now=NOW)
        update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)

    registry = ExecutorRegistry()
    exec_code = (
        "import os, sys, json, time; "
        "from pathlib import Path; "
        "snap = os.environ.get('LHGP_CONTEXT_SNAPSHOT_PATH'); "
        "Path('executor_seen.txt').write_text(json.dumps({'argv': sys.argv[1:], 'snapshot_path': snap, 'snapshot_exists': Path(snap).is_file() if snap else False, 'cwd': os.getcwd()})); "
        "time.sleep(0.05); "
        "Path('result.txt').write_text('ok')"
    )
    registry.register(
        RegistryEntry(
            id="exec-dbg",
            kind="subprocess",
            launch=LaunchSpec(
                argv=(sys.executable, "-c", exec_code),
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
                context="required",
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
    runner = AttemptRunner(root, conn, registry)

    first = run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=1))
    print("first dispatched:", first["dispatched"])
    fa = first["attempts_started"][0]
    print("first contract:", fa["contract_id"])
    runner.start_attempt(
        NOW + timedelta(seconds=1),
        contract_id=fa["contract_id"],
        attempt_id=fa["attempt_id"],
        executor_id=fa["executor_id"],
    )
    print("runner running after start:", list(runner._running.keys()))

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and runner._running:
        time.sleep(0.05)
        runner.poll_attempts(NOW + timedelta(seconds=1, milliseconds=200))
    print("after wait runner running:", list(runner._running.keys()))
    print("ws1 files:", [p.name for p in ws1.iterdir()])
    print("ws2 files:", [p.name for p in ws2.iterdir()])

    second = run_daemon_tick(root, conn, registry, now=NOW + timedelta(seconds=2))
    print("second dispatched:", second["dispatched"])
    print("second events:", second.get("events", []))
    sa = second["attempts_started"][0] if second["attempts_started"] else None
    print("second contract:", sa["contract_id"] if sa else None)
    for cid in ("lt-dbg-c1", "lt-dbg-c2"):
        v = get_contract(conn, cid)
        if v:
            print(f"  {cid}: state={v.state} deadline_status={v.deadline_status}")
    # dump all events
    rows = conn.execute(
        "SELECT contract_id, event_type, created_at FROM events ORDER BY event_id"
    ).fetchall()
    for r in rows:
        print("event:", r)
    if sa:
        runner.start_attempt(
            NOW + timedelta(seconds=2),
            contract_id=sa["contract_id"],
            attempt_id=sa["attempt_id"],
            executor_id=sa["executor_id"],
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and runner._running:
            time.sleep(0.05)
            runner.poll_attempts(NOW + timedelta(seconds=2, milliseconds=200))
        print("after wait runner running:", list(runner._running.keys()))
        print("ws1 files:", [p.name for p in ws1.iterdir()])
        print("ws2 files:", [p.name for p in ws2.iterdir()])
    conn.close()
