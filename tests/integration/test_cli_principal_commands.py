"""CLI Principal 命令：``contract user-confirm`` 与 ``proposal-apply``。

两个命令都是「MCP 工具描述已经承诺、命令行却不存在」的出口：

- ``lhgp contract user-confirm`` 是 CANDIDATE → PASSED 的**唯一**用户签字路径
  （``lhgp_user_confirm_spec_verdict`` 的 AUTH_FAILED 提示指的就是它）；
- ``lhgp proposal-apply`` 是 goal 提案的官方落地通道，此前只有分支没有解析器。

两者都直调 handler（``client_id="longtask-cli"`` → 服务端派生 ``actor=user``，
命令行不传也不能传 actor），因此必须在打开库后自己 ``ensure_schema``——
否则空 data-dir 上会撞 ``sqlite3.OperationalError: no such table: events``。
第二个测试专钉这一条：命令可达不等于命令能用。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.acceptance import Acceptance
from lhgp.contracts.auto_approve import AutoApprove
from lhgp.contracts.budget import Budget
from lhgp.contracts.contract_draft import ContractDraft
from lhgp.contracts.contract_view import AcceptanceStatus
from longtask.cli.main import main
from longtask.contracts.schema import ContractState
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    get_contract,
    save_contract,
    update_contract_state,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_entry]

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
CID = "lt-cli-uc-1"


def _seed_candidate(tmp_path: Path) -> None:
    """带 user 判据的合同，停在 CANDIDATE（与 tests/unit/test_user_confirm.py 同形）。"""
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    ensure_schema(conn)
    spec = {
        "all": [
            {"judge": "machine", "kind": "file-exists", "target": "summary.md"},
            {"judge": "user", "question": "summary 满意吗？"},
        ],
    }
    draft = ContractDraft(
        title="t",
        objective="objective",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=Acceptance(standard="s", checks=("c1",), spec=spec, spec_hash="h-cli-uc"),
        workload_initial_hours=1.0,
        budget=Budget(5, 1, 1, 30, 1_048_576, 2),
        auto_approve=AutoApprove(),
    )
    save_contract(conn, draft=draft, contract_id=CID, now=NOW, actor="user")
    update_contract_state(
        conn,
        contract_id=CID,
        new_state=ContractState.ACTIVE,
        now=NOW,
        acceptance_status=AcceptanceStatus.CANDIDATE,
    )
    conn.close()


def test_contract_user_confirm_moves_candidate_to_passed(tmp_path: Path) -> None:
    """真实入口：CLI 走通用户签字，合同从 CANDIDATE 落到 PASSED。"""
    _seed_candidate(tmp_path)
    rc = main(["--data-dir", str(tmp_path), "contract", "user-confirm", CID, "--note", "ok"])
    assert rc == 0
    conn = connect(StoreConfig(db_path=tmp_path / "state.db"))
    try:
        view = get_contract(conn, CID)
    finally:
        conn.close()
    assert view is not None
    assert view.acceptance_status == AcceptanceStatus.PASSED


def test_principal_commands_survive_a_fresh_data_dir(tmp_path: Path) -> None:
    """空 data-dir（无 schema）必须给干净错误，而不是 no such table 崩溃。

    回归：两个分支都直调 handler，handler 会在做 CAS 之前先按 request_id
    查幂等重放，那条查询是裸 SQL——不先 ``ensure_schema`` 就会抛
    ``sqlite3.OperationalError``（未捕获，直接变成 traceback）。
    """
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert main(["--data-dir", str(fresh), "contract", "user-confirm", "lt-missing"]) == 1
    assert main(["--data-dir", str(fresh), "proposal-apply", "g-missing", "1"]) == 1


def test_contract_group_without_subcommand_is_a_usage_error(tmp_path: Path) -> None:
    """``lhgp contract`` 本身不是动作：给用法并退 2，不静默成功。"""
    assert main(["--data-dir", str(tmp_path), "contract"]) == 2
