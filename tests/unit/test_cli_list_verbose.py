# ruff: noqa: I001  (import 块长 isort 假阳性，同 watch.py 情况)
"""CLI list --verbose 单元测试（DESIGN §10 可见性、§6.1 紧迫度派生）。

覆盖：compute_u（各状态/越 deadline）、format_eta（未来/过期/终态）、
render_contract_list_verbose（min_u 过滤、tier 标签、blocked_reason）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.cli.formatting import (
    compute_u,
    format_eta,
    render_contract_list_verbose,
)
from longtask.contracts.schema import (
    Acceptance,
    AcceptanceStatus,
    BlockReason,
    Budget,
    ContractDraft,
    ContractState,
    ContractView,
    DeadlineStatus,
)
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)
from longtask.cli.main import main

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _view(
    *,
    state: ContractState = ContractState.ACTIVE,
    deadline: datetime | None = None,
    workload: float = 2.0,
    blocked: BlockReason | None = None,
) -> ContractView:
    return ContractView(
        draft=ContractDraft(
            title="t",
            objective="o",
            deadline_at=deadline or NOW + timedelta(hours=4),
            hard_constraints={"file_effects": {"mode": "workspace-write"}},
            acceptance=Acceptance(standard="s", checks=("c",)),
            workload_initial_hours=workload,
            budget=Budget(
                max_dispatches=3,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=60,
                max_output_bytes=1048576,
            ),
        ),
        contract_id="lt-v01",
        goal_id="lt-v01",
        revision=1,
        state=state,
        deadline_status=DeadlineStatus.NOT_DUE,
        acceptance_status=AcceptanceStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        next_wakeup_at=None,
        next_decision_at=None,
        blocked_reason=blocked,
    )


class TestComputeU:
    def test_active_contract(self) -> None:
        # 2h 工作 / 4h 剩余 -> u = 0.5
        assert compute_u(_view(workload=2.0), NOW) == 0.5

    def test_past_deadline_returns_none(self) -> None:
        # 越 deadline -> None（§6.2 走仲裁不走阶梯）
        view = _view(deadline=NOW - timedelta(hours=1))
        assert compute_u(view, NOW) is None

    def test_terminal_states_return_none(self) -> None:
        for state in (ContractState.COMPLETE, ContractState.CANCELLED, ContractState.EXPIRED):
            assert compute_u(_view(state=state), NOW) is None


class TestFormatEta:
    def test_future_minutes(self) -> None:
        view = _view(deadline=NOW + timedelta(minutes=30))
        assert format_eta(view, NOW).startswith("in 30m")

    def test_future_hours(self) -> None:
        view = _view(deadline=NOW + timedelta(hours=2, minutes=13))
        assert "in 2h" in format_eta(view, NOW)

    def test_future_days(self) -> None:
        view = _view(deadline=NOW + timedelta(days=3, hours=5))
        assert "in 3d" in format_eta(view, NOW)

    def test_past_due(self) -> None:
        view = _view(deadline=NOW - timedelta(minutes=4))
        assert "past due" in format_eta(view, NOW)

    def test_terminal_is_dash(self) -> None:
        assert format_eta(_view(state=ContractState.COMPLETE), NOW) == "—"


class TestRenderVerbose:
    def test_includes_u_tier_eta_blocked(self) -> None:
        # u = 4.0/2.0 = 2.0 -> RESPAWN 档
        view = _view(deadline=NOW + timedelta(hours=2), workload=4.0, blocked=BlockReason.NEED_USER)
        out = render_contract_list_verbose([view], min_u=None, now=NOW)
        item = out["result"][0]
        assert item["u"] == 2.0
        assert item["tier"] is not None
        assert "in 2h" in item["eta"]
        assert item["blocked_reason"] == "need-user"

    def test_min_u_filters_out(self) -> None:
        # u = 0.5 < min_u=1.0 -> 被过滤
        view = _view(workload=2.0)  # deadline +4h -> u=0.5
        out = render_contract_list_verbose([view], min_u=1.0, now=NOW)
        assert out["result"] == []

    def test_min_u_keeps_higher(self) -> None:
        view = _view(deadline=NOW + timedelta(hours=1), workload=2.0)  # u=2.0
        out = render_contract_list_verbose([view], min_u=1.0, now=NOW)
        assert len(out["result"]) == 1

    def test_none_u_passes_filter(self) -> None:
        # 终态合同 u=None 不被 min_u 排除（仍显示供总览）
        view = _view(state=ContractState.COMPLETE)
        out = render_contract_list_verbose([view], min_u=1.0, now=NOW)
        assert len(out["result"]) == 1
        assert out["result"][0]["u"] is None


class TestListVerboseCLI:
    """走 main() 端到端：--verbose JSON 输出含 u 字段。"""

    def test_verbose_flag_outputs_u(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        save_contract(
            conn,
            ContractDraft(
                title="u 测试",
                objective="o",
                deadline_at=datetime.now(UTC) + timedelta(hours=2),
                hard_constraints={"file_effects": {"mode": "workspace-write"}},
                acceptance=Acceptance(standard="s", checks=("c",)),
                workload_initial_hours=4.0,  # u = 4/2 = 2.0
                budget=Budget(
                    max_dispatches=3,
                    max_escalations=1,
                    max_concurrent_attempts=1,
                    max_attempt_minutes=60,
                    max_output_bytes=1048576,
                ),
            ),
            contract_id="lt-20260901-lv01",
            now=datetime.now(UTC),
        )
        update_contract_state(
            conn,
            contract_id="lt-20260901-lv01",
            new_state=ContractState.ACTIVE,
            now=datetime.now(UTC),
        )
        conn.close()

        rc = main(["--data-dir", str(data_dir), "list", "--verbose"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        item = next(i for i in out["result"] if i["contract_id"] == "lt-20260901-lv01")
        assert item["u"] is not None and item["u"] >= 1.5
        assert item["tier"] is not None
        assert item["eta"] is not None
