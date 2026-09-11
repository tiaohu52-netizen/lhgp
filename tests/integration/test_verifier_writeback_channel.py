"""SPEC §12.4 通道 1：会话型 verifier 经 `attempt/write-back` 上报裁决（审计 B9b）。

SPEC §12.4 明写通道有两条：

1. **RPC `attempt/write-back`**（会话型 harness）：verifier 在会话内调用协议 RPC，
   终态必须携带 evidence 列表；
2. stdout 判定块（一次性 CLI harness）。

`handle_attempt_write_back` 早就为通道 1 写好了 verifier 专用逻辑——只有
`attempt_role == "verifier"` 时才由运行时补上证据的内容指纹绑定
（`attempt_evidence_binding`，防篡改）。但构造 `EventInput` 时 role 只进了 payload，
**没落到 `events.role` 列**：`store.write_back` 的 `role=(inp.role or role or actor)`
于是兜底到 `actor="model"`，而读取方（`tick._judge_verifier_outcomes`、
`contract.py` 的验收证据绑定）判定 verifier 事件的条件是

    event.role == "verifier"  或（event.role is None 且 payload 里能找到 role）

——"model" 两条都不满足，**通道 1 的裁决对读取方完全不可见**：合同永远完不成、
证据读不到。且 "model" 不在 `events.role` 的文档取值域内（schema.py：
executor / verifier / daemon / user / promoter / scheduler / system）。

实测（探针，修复前）：`attempt/succeeded` 列.role='model'，
tick 能看见的 verifier 成功事件数 = 0；修复后 = 1。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import get_events
from lhgp.persistence.store import (
    StoreConfig,
    acquire_lease,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)
from lhgp.rpc.methods import Method
from longtask.cli.tick import _judge_verifier_outcomes
from longtask.contracts.schema import (
    Acceptance,
    AcceptanceStatus,
    Budget,
    ContractDraft,
    ContractState,
)
from longtask.rpc.executor_api import handle_attempt_write_back
from longtask.rpc.server import RequestEnvelope

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
TOKEN = "session-token-for-channel-one-probe"  # noqa: S105 - test fixture
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
# schema.py 里 events.role 的文档取值域
DOCUMENTED_ROLES = frozenset(
    {"executor", "verifier", "daemon", "user", "promoter", "scheduler", "system"}
)


def _contract(tmp_path: Path, cid: str, *, root: Path) -> Any:
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="通道 1 裁决",
            objective="verifier 经 write-back 上报必须被读取方看见",
            deadline_at=NOW + timedelta(hours=2),
            hard_constraints={},
            acceptance=Acceptance(standard="全部通过", checks=("m1",)),
            workload_initial_hours=1.0,
            budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        ),
        contract_id=cid,
        now=NOW,
        actor="user",
    )
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)
    return conn


def _attempt_with_lease(conn: Any, cid: str, attempt_id: str, role: str) -> int:
    conn.execute(
        "INSERT INTO attempts (attempt_id, contract_id, goal_id, role, state, admitted_at,"
        " contract_revision, updated_at, session_token_hash)"
        " VALUES (?, ?, ?, ?, 'running', ?, 1, ?, ?)",
        (attempt_id, cid, cid, role, NOW.isoformat(), NOW.isoformat(), TOKEN_HASH),
    )
    lease = acquire_lease(
        conn,
        contract_id=cid,
        holder_attempt_id=attempt_id,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=30),
        actor="daemon",
        payload={},
        role=role,
        contract_revision=1,
        expected_generation=0,
    )
    conn.commit()
    return lease.generation


def _write_back(conn: Any, cid: str, attempt_id: str, generation: int, *, state: str) -> None:
    handle_attempt_write_back(
        RequestEnvelope(
            method=Method.ATTEMPT_WRITE_BACK,
            request_id=f"wb-{attempt_id}",
            client_id="executor",
            protocol_version=2,
            params={
                "contract_id": cid,
                "attempt_id": attempt_id,
                "write_generation": generation,
                "session_token": TOKEN,
                "attempt_state": state,
                "evidence": [{"check_id": "m1", "outcome": "pass", "source": "verifier-run"}],
            },
        ),
        conn=conn,
        now=NOW,
    )


def _terminal_event(conn: Any, cid: str) -> Any:
    events = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.ATTEMPT_SUCCEEDED.value
    ]
    assert events, "write-back 必须落下终态事件"
    return events[-1]


def test_verifier_write_back_event_carries_the_verifier_role(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir(parents=True)
    cid = "lt-channel-one-verifier"
    conn = _contract(tmp_path, cid, root=root)
    generation = _attempt_with_lease(conn, cid, "ver-1", "verifier")
    _write_back(conn, cid, "ver-1", generation, state="succeeded")

    event = _terminal_event(conn, cid)
    assert event.role == "verifier", (
        f"通道 1 的裁决事件 role={event.role!r}，读取方会当成非 verifier 事件"
    )
    assert event.role in DOCUMENTED_ROLES, "events.role 必须落在 schema 文档取值域内"
    conn.close()


def test_verifier_write_back_completes_the_contract(tmp_path: Path) -> None:
    """行为级后果：通道 1 上报的裁决必须真的能被采信（SPEC §12.4 + §5.2）。"""
    root = tmp_path / "data"
    root.mkdir(parents=True)
    cid = "lt-channel-one-complete"
    conn = _contract(tmp_path, cid, root=root)
    generation = _attempt_with_lease(conn, cid, "ver-2", "verifier")
    _write_back(conn, cid, "ver-2", generation, state="succeeded")

    _judge_verifier_outcomes(root, conn, NOW)

    contract = get_contract(conn, cid)
    assert contract is not None
    assert contract.state == ContractState.COMPLETE, (
        f"通道 1 的 verifier 成功未能完成合同，state={contract.state}"
    )
    assert contract.acceptance_status == AcceptanceStatus.PASSED
    completed = [
        e
        for e in get_events(conn, contract_id=cid)
        if str(e.event_type) == EventType.CONTRACT_COMPLETED.value
    ]
    assert completed, "必须记 contract/completed"
    conn.close()


def test_executor_write_back_is_not_mistaken_for_a_verifier(tmp_path: Path) -> None:
    """对照组：执行者回写要落 executor，且不得被读取方当成 verifier。"""
    root = tmp_path / "data"
    root.mkdir(parents=True)
    cid = "lt-channel-one-executor"
    conn = _contract(tmp_path, cid, root=root)
    generation = _attempt_with_lease(conn, cid, "att-1", "executor")
    _write_back(conn, cid, "att-1", generation, state="succeeded")

    event = _terminal_event(conn, cid)
    assert event.role == "executor"
    assert event.role in DOCUMENTED_ROLES
    _judge_verifier_outcomes(root, conn, NOW)
    contract = get_contract(conn, cid)
    assert contract is not None
    assert contract.state == ContractState.ACTIVE, "执行者回写不得直接完成合同"
    conn.close()
