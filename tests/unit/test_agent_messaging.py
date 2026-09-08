"""Agent messaging layer regression (branch feature/agent-messaging)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.persistence.messages import (
    get_messages,
    pending_directives,
    send_message,
)
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import save_contract

NOW = datetime(2026, 9, 6, 14, 0, 0, tzinfo=UTC)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    ensure_schema(conn)
    return conn


def _contract(conn: sqlite3.Connection, cid: str = "lt-msg01") -> None:
    save_contract(
        conn,
        contract_id=cid,
        draft=ContractDraft(
            title="msg",
            objective="messaging test",
            deadline_at=NOW + timedelta(hours=4),
            hard_constraints={},
            acceptance=Acceptance(standard="s", checks=("c1",)),
            workload_initial_hours=1.0,
            budget=Budget(
                max_dispatches=5,
                max_escalations=1,
                max_concurrent_attempts=1,
                max_attempt_minutes=30,
                max_output_bytes=1048576,
            ),
        ),
        now=NOW,
        actor="user",
    )


class TestSendMessage:
    def test_directive_roundtrip(self, tmp_path: Path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            event_id = send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="user",
                kind="directive",
                text="skip check c2, use approach B",
                now=NOW,
            )
            assert event_id > 0
            msgs = get_messages(conn, contract_id="lt-msg01")
            assert len(msgs) == 1
            assert msgs[0]["kind"] == "directive"
            assert msgs[0]["text"] == "skip check c2, use approach B"
        finally:
            conn.close()

    def test_invalid_kind_rejected(self, tmp_path: Path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            with pytest.raises(ValueError, match="invalid message kind"):
                send_message(
                    conn,
                    contract_id="lt-msg01",
                    from_actor="model",
                    kind="gossip",
                    text="hey",
                    now=NOW,
                )
        finally:
            conn.close()

    def test_multiple_kinds_filterable(self, tmp_path: Path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="user",
                kind="directive",
                text="do X",
                now=NOW,
            )
            send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="model",
                kind="question",
                text="which X?",
                now=NOW,
            )
            send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="model",
                kind="context",
                text="did Y",
                now=NOW,
            )

            directives = pending_directives(conn, contract_id="lt-msg01")
            assert len(directives) == 1
            assert directives[0]["text"] == "do X"

            all_msgs = get_messages(conn, contract_id="lt-msg01")
            assert len(all_msgs) == 3
        finally:
            conn.close()


class TestContextInjection:
    def test_directives_appear_in_context(self, tmp_path: Path) -> None:
        """用户的 directive 必须出现在下个 attempt 的上下文里。"""
        from lhgp.persistence.store import get_contract
        from longtask.persistence.context import compile_context_snapshot

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            # 模拟用户发 directive
            send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="user",
                kind="directive",
                text="use approach B instead of A",
                now=NOW,
            )
            # 编译上下文
            contract = get_contract(conn, "lt-msg01")
            assert contract is not None
            _, scratch, _consumed = compile_context_snapshot(
                tmp_path, conn, contract, "att-test", NOW
            )
            # active.md 应包含 directive
            active_path = scratch.parent / "active.md"
            content = active_path.read_text(encoding="utf-8")
            assert "use approach B instead of A" in content
            assert "收到的指令" in content
        finally:
            conn.close()

    def test_no_directives_no_section(self, tmp_path: Path) -> None:
        conn = _conn(tmp_path)
        try:
            _contract(conn)
            from lhgp.persistence.store import get_contract
            from longtask.persistence.context import compile_context_snapshot

            contract = get_contract(conn, "lt-msg01")
            assert contract is not None
            _, scratch, _consumed = compile_context_snapshot(
                tmp_path, conn, contract, "att-test", NOW
            )
            active_path = scratch.parent / "active.md"
            content = active_path.read_text(encoding="utf-8")
            assert "收到的指令" not in content
        finally:
            conn.close()


class TestA2AScoping:
    """3rd-round review (2026-09-08): the message layer's ``to_agent``
    field was being ignored.  Two agents working the same contract
    both saw each other's targeted directives, and the contract-
    level broadcast cursor advanced even when only one agent
    consumed a directive.  Fix: per-agent cursor + per-agent
    filter in pending_directives."""

    def test_targeted_directive_only_seen_by_named_agent(self, tmp_path: Path) -> None:
        from lhgp.persistence.store import get_contract
        from longtask.persistence.context import compile_context_snapshot

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="agent:a",
                to_agent="agent:b",
                kind="directive",
                text="handoff the bugfix branch to B",
                now=NOW,
            )
            contract = get_contract(conn, "lt-msg01")
            assert contract is not None

            # Agent A asks: should NOT see the message addressed to B.
            _, scratch_a, consumed_a = compile_context_snapshot(
                tmp_path, conn, contract, "att-a", NOW, to_agent="agent:a"
            )
            (scratch_a.parent / "active.md").read_text(encoding="utf-8")
            assert consumed_a == 0  # nothing consumed

            # Agent B asks: must see the message.
            _, scratch_b, consumed_b = compile_context_snapshot(
                tmp_path, conn, contract, "att-b", NOW, to_agent="agent:b"
            )
            text_b = (scratch_b.parent / "active.md").read_text(encoding="utf-8")
            assert "handoff the bugfix branch to B" in text_b
            assert consumed_b > 0
        finally:
            conn.close()

    def test_broadcast_directive_seen_by_every_agent(self, tmp_path: Path) -> None:
        from lhgp.persistence.store import get_contract
        from longtask.persistence.context import compile_context_snapshot

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="user",
                kind="directive",
                text="general steering for all agents",
                now=NOW,
            )
            contract = get_contract(conn, "lt-msg01")
            assert contract is not None
            for who in ("agent:a", "agent:b", None):
                # attempt_id must be a plain identifier — colon would
                # make the path creation fail on Windows.
                att_id = f"att-{who.replace(':', '_') if who else 'any'}"
                _, scratch, _consumed = compile_context_snapshot(
                    tmp_path, conn, contract, att_id, NOW, to_agent=who
                )
                text = (scratch.parent / "active.md").read_text(encoding="utf-8")
                assert "general steering for all agents" in text
        finally:
            conn.close()

    def test_directive_section_preserves_sender(self, tmp_path: Path) -> None:
        from lhgp.persistence.store import get_contract
        from longtask.persistence.context import compile_context_snapshot

        conn = _conn(tmp_path)
        try:
            _contract(conn)
            send_message(
                conn,
                contract_id="lt-msg01",
                from_actor="agent:reviewer",
                to_agent="agent:dev",
                kind="directive",
                text="please add a regression test",
                now=NOW,
            )
            contract = get_contract(conn, "lt-msg01")
            assert contract is not None
            _, scratch, _consumed = compile_context_snapshot(
                tmp_path, conn, contract, "att-dev", NOW, to_agent="agent:dev"
            )
            text = (scratch.parent / "active.md").read_text(encoding="utf-8")
            # Real sender preserved, not collapsed to "user"
            assert "from `agent:reviewer`" in text
            assert "to `agent:dev`" in text
        finally:
            conn.close()
