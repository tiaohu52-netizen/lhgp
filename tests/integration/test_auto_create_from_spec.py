"""Auto-create next stage contract from the stage's structured spec.

3rd-round review 2026-09-08: the daemon should synthesize a usable
contract draft from ``stage.spec`` when no inline ``stage.draft`` is
supplied. The previous stage's verifier evidence is forwarded into the
new contract's ``context`` so the next executor can reference produced
artifacts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
            "spec-only auto-advance",
            "verify spec-driven contract synthesis",
            json.dumps({"stages": stages}, ensure_ascii=False),
            "{}",
            NOW.isoformat(),
            NOW.isoformat(),
            2,
        ),
    )
    conn.commit()


def _prepare_with_inline_draft(
    conn: sqlite3.Connection,
    goal_id: str,
    stage_id: str,
    contract_id: str,
    *,
    spec: dict,
) -> None:
    draft = {
        "title": f"阶段 {stage_id}",
        "objective": "实现该阶段目标",
        "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
        "hard_constraints": {},
        "acceptance": {
            "standard": "通过",
            "checks": ["result.txt 存在"],
            "verifier": "cross_check",
            "spec": spec,
            "spec_hash": "h",
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


def _activate(conn: sqlite3.Connection, cid: str) -> None:
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)


def _record_verifier(
    conn: sqlite3.Connection,
    cid: str,
    *,
    check_results: dict[str, str] | None = None,
) -> None:
    append_event(
        conn,
        contract_id=cid,
        attempt_id="ver-1",
        event_type=EventType.ATTEMPT_SUCCEEDED,
        payload={
            "reported_by": "model",
            "role": "verifier",
            "checks": check_results or {"check1": "pass"},
            "evidence": {"produced_files": ["result.txt", "report.md"]},
        },
        now=NOW,
        actor="model",
    )


def test_next_stage_spec_drives_contract_synthesis(tmp_path: Path) -> None:
    """stage 2 has only a spec, no inline draft → daemon synthesizes a contract.

    The previous verifier's evidence is forwarded to stage 2's context.
    """
    root, conn, reg = _setup(tmp_path)
    spec_stage1 = {"all": [{"judge": "machine", "kind": "file-exists", "target": "result.txt"}]}
    spec_stage2 = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "deploy.log"},
            {"judge": "machine", "kind": "file-exists", "target": "summary.md"},
        ]
    }
    cid1 = "lt-20260901-spec-stage1"
    _insert_goal(
        conn,
        "goal-spec-auto",
        [
            {
                "id": "stage-1",
                "title": "实现",
                "spec": spec_stage1,
                "draft": {  # bootstrap draft for stage 1
                    "title": "stage-1",
                    "objective": "实现该阶段目标",
                    "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                    "hard_constraints": {},
                    "acceptance": {
                        "standard": "通过",
                        "checks": ["result.txt 存在"],
                        "spec": spec_stage1,
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
            {
                "id": "stage-2",
                "title": "部署",
                "spec": spec_stage2,  # no inline draft — daemon must synthesize
            },
        ],
    )
    _prepare_with_inline_draft(conn, "goal-spec-auto", "stage-1", cid1, spec=spec_stage1)
    _activate(conn, cid1)
    _record_verifier(conn, cid1, check_results={"file-exists:result.txt": "pass"})

    run_daemon_tick(root, conn, reg, now=NOW)

    # Stage 1 should be complete
    assert get_contract(conn, cid1).state == ContractState.COMPLETE
    # Stage 2 should have been auto-created from spec
    stage2_contracts = [
        c
        for c in (
            get_contract(conn, cid)
            for cid in (
                row[0]
                for row in conn.execute(
                    "SELECT contract_id FROM contracts WHERE goal_id = ? AND contract_id != ?",
                    ("goal-spec-auto", cid1),
                ).fetchall()
            )
        )
        if c is not None
    ]
    assert len(stage2_contracts) == 1
    stage2 = stage2_contracts[0]
    # The synthesized draft must carry the stage-2 spec through to the contract
    assert stage2.draft.acceptance.spec == spec_stage2
    assert stage2.draft.acceptance.spec_hash
    # The previous stage's evidence is forwarded into stage 2's context
    assert "previous_evidence" in stage2.draft.context
    conn.close()
