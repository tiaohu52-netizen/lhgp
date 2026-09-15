"""模板库 / CLI 扩展 / 提案事件回归（分支 feature/plugin-expansion）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp import templates
from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import save_contract

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


class TestTemplates:
    def test_builtins_available(self) -> None:
        names = templates.available()
        assert {"code-change", "research", "release-check"} <= set(names)

    def test_load_returns_valid_json_with_acceptance(self) -> None:
        raw = templates.load("code-change")
        payload = json.loads(raw)
        assert payload["acceptance"]["checks"]
        # 占位符模板：deadline 要么已是带时区的样例，要么是显式 <占位符>
        assert payload["deadline_at"].endswith("+00:00") or "<" in payload["deadline_at"]

    def test_load_unknown_template(self) -> None:
        with pytest.raises(FileNotFoundError):
            templates.load("no-such-template")


class TestProposalEvent:
    def _conn(self, tmp_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "state.db")
        ensure_schema(conn)
        save_contract(
            conn,
            contract_id="lt-prop01",
            draft=ContractDraft(
                title="p",
                objective="o",
                deadline_at=NOW + timedelta(hours=4),
                hard_constraints={},
                acceptance=Acceptance(standard="s", checks=("c1",)),
                workload_initial_hours=1.0,
                budget=Budget(
                    max_dispatches=3,
                    max_escalations=1,
                    max_concurrent_attempts=1,
                    max_attempt_minutes=30,
                    max_output_bytes=1048576,
                ),
            ),
            now=NOW,
            actor="user",
        )
        return conn

    def test_proposal_event_is_non_authoritative(self, tmp_path: Path) -> None:
        """提案只落 goal/proposed 事件：Goal 权威状态不得被改变。"""
        from lhgp.persistence.events_query import get_events
        from longtask.persistence.events import EventType
        from longtask.persistence.store import append_event, get_goal

        conn = self._conn(tmp_path)
        try:
            before = get_goal(conn, "lt-prop01")
            assert before is not None
            revision_before = before["revision"]

            append_event(
                conn,
                contract_id="lt-prop01",
                goal_id="lt-prop01",
                event_type=EventType.GOAL_PROPOSED,
                payload={
                    "goal_id": "lt-prop01",
                    "proposed_by": "model",
                    "reason": "scope split",
                    "plan": {"stages": [{"id": "s1"}, {"id": "s2"}]},
                    "status": "pending",
                },
                now=NOW,
                actor="model",
            )
            after = get_goal(conn, "lt-prop01")
            assert after is not None
            assert after["revision"] == revision_before, "proposal mutated authoritative goal state"
            proposed = [
                e
                for e in get_events(conn, contract_id="lt-prop01")
                if e.event_type == EventType.GOAL_PROPOSED.value
            ]
            assert len(proposed) == 1
        finally:
            conn.close()
