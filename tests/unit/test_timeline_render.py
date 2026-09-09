"""Focused tests for ``lhgp.persistence.timeline.build_timeline_html``.

The renderer is the human-facing escape hatch for "what happened to this
contract"; it ships a single self-contained HTML file with the event rows
embedded as JSON.  Two properties are load-bearing and were previously
untested:

1. every string that reaches the page is HTML-escaped, because event
   payloads carry operator- and agent-controlled text;
2. malformed ``payload_json`` degrades to an empty summary instead of
   raising, since the timeline is exactly the tool you reach for when a
   run already went wrong.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.persistence.events import EventType
from lhgp.persistence.events_query import append_event
from lhgp.persistence.timeline import _event_kind, build_timeline_html
from longtask.contracts.contract_view import ContractState
from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.store import (
    StoreConfig,
    connect,
    ensure_schema,
    save_contract,
    update_contract_state,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    (tmp_path / "data").mkdir()
    c = connect(StoreConfig(db_path=tmp_path / "data" / "state.db"))
    ensure_schema(c)
    yield c
    c.close()


def _seed_contract(conn: sqlite3.Connection, cid: str, *, title: str = "timeline probe") -> None:
    draft = ContractDraft(
        title=title,
        objective="render the event timeline",
        deadline_at=NOW + timedelta(hours=3),
        hard_constraints={
            "file_effects": {"mode": "workspace-write", "workspace_root": "timeline-ws"}
        },
        acceptance=Acceptance(standard="ok", checks=("ok",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=3,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=65536,
        ),
    )
    save_contract(conn, draft, contract_id=cid, now=NOW)
    update_contract_state(conn, contract_id=cid, new_state=ContractState.ACTIVE, now=NOW)


def _data_rows(page: str) -> list[dict[str, object]]:
    """Pull the embedded ``const DATA = [...]`` array back out of the page."""
    marker = "const DATA = "
    start = page.index(marker) + len(marker)
    end = page.index("\n", start)
    return json.loads(page[start:end].rstrip().rstrip(";"))


def _stored_event_count(conn: sqlite3.Connection, cid: str) -> int:
    # ``save_contract`` / ``update_contract_state`` write their own lifecycle
    # events, so a "seeded" contract never has a zero-length history. Tests
    # compare against the row count instead of hard-coding a baseline.
    row = conn.execute("SELECT COUNT(*) FROM events WHERE contract_id = ?", (cid,)).fetchone()
    return int(row[0])


class TestMissingContract:
    def test_unknown_contract_returns_error_not_traceback(self, conn: sqlite3.Connection) -> None:
        page, error = build_timeline_html(conn, contract_id="c-nope", now=NOW)
        assert page == ""
        assert error is not None
        assert "c-nope" in error
        assert "not found" in error


class TestRendering:
    def test_lifecycle_only_contract_still_renders_a_page(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-empty")
        page, error = build_timeline_html(conn, contract_id="c-empty", now=NOW)
        assert error is None
        assert page.startswith("<!DOCTYPE html>")
        rows = _data_rows(page)
        assert rows
        assert f"{len(rows)} events" in page
        assert len(rows) == _stored_event_count(conn, "c-empty")

    def test_events_appear_in_order_with_mapped_kinds(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-full", title="release cut")
        approved_event = append_event(
            conn,
            contract_id="c-full",
            event_type=EventType.CONTRACT_APPROVED,
            payload={"note": "user signed off"},
            now=NOW + timedelta(seconds=1),
            actor="user",
        )
        succeeded_event = append_event(
            conn,
            contract_id="c-full",
            event_type=EventType.ATTEMPT_SUCCEEDED,
            payload={"reason": "all checks green"},
            now=NOW + timedelta(seconds=2),
            attempt_id="a-1",
        )
        page, error = build_timeline_html(conn, contract_id="c-full", now=NOW)
        assert error is None
        rows = _data_rows(page)
        by_id = {int(str(row["event_id"])): row for row in rows}
        event_ids = [int(str(row["event_id"])) for row in rows]
        assert event_ids == sorted(event_ids), "timeline must read oldest → newest"
        types = [str(row["type"]) for row in rows]
        assert types[-2:] == ["contract/approved", "attempt/succeeded"]
        assert "contract/prepared" in types
        # Look rows up by event id: ``contract/approved`` is also written by the
        # seeding path, so a type lookup would silently read the wrong row.
        succeeded = by_id[succeeded_event.event_id]
        assert succeeded["kind"] == "success"
        assert succeeded["color"] == "#16a34a"
        assert succeeded["summary"] == "all checks green"
        assert succeeded["attempt_id"] == "a-1"
        approved = by_id[approved_event.event_id]
        assert approved["actor"] == "user"
        assert approved["summary"] == "user signed off"
        assert f"{len(rows)} events" in page

    def test_header_carries_contract_identity(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-head", title="季度复盘")
        page, _ = build_timeline_html(conn, contract_id="c-head", now=NOW)
        assert "c-head — 季度复盘" in page
        assert ContractState.ACTIVE.value in page
        assert (NOW + timedelta(hours=3)).isoformat() in page
        assert NOW.isoformat() in page

    def test_state_and_headline_are_escaped(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-xss", title="<script>alert(1)</script>")
        append_event(
            conn,
            contract_id="c-xss",
            event_type=EventType.ATTEMPT_FAILED,
            payload={"reason": "<img src=x onerror=alert(1)>"},
            now=NOW + timedelta(seconds=1),
        )
        page, _ = build_timeline_html(conn, contract_id="c-xss", now=NOW)
        assert "<script>alert(1)</script>" not in page
        assert "<img src=x" not in page
        assert "&lt;script&gt;" in page
        assert "&lt;img src=x onerror=alert(1)&gt;" in page

    def test_summary_is_truncated_to_a_visible_width(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-long")
        long_reason = "为" * 400
        append_event(
            conn,
            contract_id="c-long",
            event_type=EventType.CONTRACT_BLOCKED,
            payload={"reason": long_reason},
            now=NOW + timedelta(seconds=1),
        )
        page, _ = build_timeline_html(conn, contract_id="c-long", now=NOW)
        row = _data_rows(page)[-1]
        assert row["type"] == "contract/blocked"
        summary = str(row["summary"])
        assert len(summary) == 160
        assert summary == long_reason[:160]

    def test_structured_reason_is_serialised_not_dropped(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-dict")
        append_event(
            conn,
            contract_id="c-dict",
            event_type=EventType.ATTEMPT_FAILED,
            payload={"reason": {"code": "EXIT_NONZERO", "exit": 2}},
            now=NOW + timedelta(seconds=1),
        )
        page, _ = build_timeline_html(conn, contract_id="c-dict", now=NOW)
        row = _data_rows(page)[-1]
        assert row["type"] == "attempt/failed"
        # The summary is JSON first and HTML-escaped second — unescape before
        # parsing, otherwise the quotes come back as ``&quot;``.
        decoded = json.loads(html.unescape(str(row["summary"])))
        assert decoded == {"code": "EXIT_NONZERO", "exit": 2}

    def test_corrupt_payload_degrades_to_empty_summary(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-corrupt")
        corrupt_event = append_event(
            conn,
            contract_id="c-corrupt",
            event_type=EventType.ATTEMPT_FAILED,
            payload={"reason": "recoverable"},
            now=NOW + timedelta(seconds=1),
        )
        # Storage-level corruption: hand-write garbage into the payload column.
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE contract_id = ?",
            ("{not json at all", "c-corrupt"),
        )
        conn.commit()
        page, error = build_timeline_html(conn, contract_id="c-corrupt", now=NOW)
        assert error is None
        row = _data_rows(page)[-1]
        assert row["event_id"] == corrupt_event.event_id
        assert row["summary"] == ""
        assert row["type"] == "attempt/failed"

    def test_events_from_other_contracts_are_not_leaked(self, conn: sqlite3.Connection) -> None:
        _seed_contract(conn, "c-mine")
        _seed_contract(conn, "c-theirs")
        foreign = append_event(
            conn,
            contract_id="c-theirs",
            event_type=EventType.ATTEMPT_STARTED,
            payload={},
            now=NOW + timedelta(seconds=1),
        )
        page, _ = build_timeline_html(conn, contract_id="c-mine", now=NOW)
        rows = _data_rows(page)
        assert foreign.event_id not in {int(str(row["event_id"])) for row in rows}
        assert len(rows) == _stored_event_count(conn, "c-mine")


class TestEventKindTable:
    def test_known_types_have_a_colour(self) -> None:
        assert _event_kind("attempt/succeeded") == ("success", "#16a34a")
        assert _event_kind("escalation/handed-to-user") == ("escalation", "#dc2626")

    def test_unknown_types_fall_back_to_other(self) -> None:
        kind, color = _event_kind("brand/new-event")
        assert kind == "other"
        assert color == "#6b7280"

    def test_every_mapped_kind_is_renderable(self) -> None:
        # The filter buttons are generated from the kind field, so a kind with
        # an empty or space-bearing name would produce broken JS.
        from lhgp.persistence.timeline import _EVENT_KIND

        assert _EVENT_KIND
        for event_type, (kind, color) in _EVENT_KIND.items():
            assert kind.isidentifier(), event_type
            assert color.startswith("#") and len(color) == 7, event_type
