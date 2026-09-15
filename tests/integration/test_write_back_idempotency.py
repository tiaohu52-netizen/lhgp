"""write_back 幂等锚点回归（安全审查 持久化-C3）。

`events=()` 且带 `contract_state` 的合法 write-back 形态此前没有任何
request_id 落点：幂等早退靠 `get_events_by_request_id`，探测面为空导致
重放同一 request_id 会二次执行状态迁移（revision 二次膨胀、重复终态）。
修复：空 events 时追加一条 `attempt/write-back` 簿记事件携带 request_id。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft, ContractState
from lhgp.persistence.schema import connect, ensure_schema
from lhgp.persistence.store import (
    acquire_lease,
    get_events_by_request_id,
    save_contract,
    write_back,
)
from longtask.persistence.schema import StoreConfig

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
CID = "lt-wbidem01"
pytestmark = pytest.mark.integration


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn,
        ContractDraft(
            title="幂等锚点",
            objective="验证 write_back 重放",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=3,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=60,
                max_output_bytes=1048576,
            ),
        ),
        contract_id=CID,
        now=NOW,
        # 状态机（审计 B1）：写回通道带 `contract_state` 时目标态必须是合同当前状态
        # 的合法后继。本用例要写成 paused，而 DRAFTED 没有到 paused 的边——按真实
        # 生命周期把合同建为 active（只有已派工的合同才有 attempt 写回）。
        state=ContractState.ACTIVE,
    )
    acquire_lease(
        conn,
        contract_id=CID,
        holder_attempt_id="att-1",
        expected_generation=0,
        heartbeat_at=NOW,
        timeout=timedelta(minutes=30),
    )
    try:
        yield conn
    finally:
        conn.close()


def test_empty_events_write_back_is_replay_idempotent(store: Any) -> None:
    from lhgp.contracts.contract_view import ContractState

    args = dict(
        contract_id=CID,
        attempt_id="att-1",
        write_generation=1,
        request_id="req-idem-1",
        now=NOW,
        contract_state=ContractState.PAUSED,
    )
    first = write_back(store, **args)
    row1 = store.execute(
        "SELECT revision, state FROM contracts WHERE contract_id=?", (CID,)
    ).fetchone()
    second = write_back(store, **args)  # 重放同一 request_id
    row2 = store.execute(
        "SELECT revision, state FROM contracts WHERE contract_id=?", (CID,)
    ).fetchone()

    assert row1 == row2, f"replay mutated contract: {row1} -> {row2}"
    events = get_events_by_request_id(store, "req-idem-1")
    assert events, "no idempotency anchor event written for request_id"
    assert second.revision == first.revision
