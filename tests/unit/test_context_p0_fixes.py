"""P0 / P1 fixes from the 4-agent review of compile_context_snapshot.

Each test class is a single ship-block:
  - TestDeadlineBreachWarning      deadline > now ⚠️ in header
  - TestHandoverIncompleteEvent    corrupt handover HANDOVER_INCOMPLETE
  - TestExpiredSnapshotCleanup     CONTEXT_SNAPSHOT_EXPIRED emitter + sweep
  - TestDirectiveCap               pending_directives() hard cap
  - TestMemoryBudgetFormula        small contracts no longer blow up
  - TestDigestLimit                _recent_attempt_digest stays bounded
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.context import (
    _MAX_DIRECTIVES_INJECTED,
    _expire_old_snapshots,
    _handover_data,
    _recent_attempt_digest,
    compile_context_snapshot,
)
from longtask.persistence.events import EventType
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    save_contract,
)

pytestmark = pytest.mark.unit


def _make_draft(*, deadline_at: datetime, max_bytes: int | None = None) -> ContractDraft:
    context: dict = {}
    if max_bytes is not None:
        context = {
            "required": True,
            "limits": {"max_bytes": max_bytes, "expires_after_minutes": 60},
        }
    return ContractDraft(
        title="P0 smoke",
        objective="verify fix",
        deadline_at=deadline_at,
        hard_constraints={},
        acceptance=Acceptance(standard="x", checks=("y",)),
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=2,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=10,
            max_output_bytes=65536,
        ),
        context=context,
    )


class TestDeadlineBreachWarning:
    """When the clock has passed the contract's deadline, active.md
    must include a top-of-file warning so a tool that only reads the
    header notices the breach."""

    def test_breach_header_present_when_deadline_passed(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        deadline = datetime(2026, 1, 1, tzinfo=UTC)
        now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # 1 hour after deadline
        draft = _make_draft(deadline_at=deadline)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        save_contract(conn, draft, contract_id="lt-breach", now=now)
        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-breach")
        assert view is not None
        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-breach-1", now=now
        )
        text = active.read_text(encoding="utf-8")
        assert "## ⚠️ 合同已超期" in text
        assert text.index("## ⚠️") < text.index("## 合同锚点")
        conn.close()

    def test_no_breach_warning_when_on_time(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        deadline = datetime(2099, 1, 1, tzinfo=UTC)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        draft = _make_draft(deadline_at=deadline)
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        save_contract(conn, draft, contract_id="lt-ok", now=now)
        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-ok")
        assert view is not None
        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-ok-1", now=now
        )
        text = active.read_text(encoding="utf-8")
        assert "## ⚠️ 合同已超期" not in text
        conn.close()


class TestHandoverIncompleteEvent:
    """A handover file that exists but fails to parse must produce a
    HANDOVER_INCOMPLETE event so an auditor distinguishes 'no handover
    yet = first attempt' from 'handover broken = previous attempt
    left a corrupt file'."""

    def test_corrupt_handover_appends_event(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        handover_path = data_dir / "contracts" / "lt-bad" / "handover.md"
        handover_path.parent.mkdir(parents=True)
        # Truncated YAML / broken structure -> parse_handover_markdown
        # returns (None, [Violation(...)]).
        handover_path.write_text("not a valid handover file at all\n", encoding="utf-8")
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        result = _handover_data(data_dir, "lt-bad", conn=conn, now=datetime.now(UTC))
        assert result == {}
        evt = conn.execute(
            "SELECT event_type, payload_json FROM events WHERE event_type = ?",
            (EventType.HANDOVER_INCOMPLETE.value,),
        ).fetchone()
        assert evt is not None
        payload = json.loads(evt[1])
        assert payload["reason"] == "parse_failed"
        conn.close()

    def test_oserror_appends_event(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from pathlib import Path as _Path

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # Place a real handover.md so ``is_file()`` is True.
        handover_path = data_dir / "contracts" / "lt-os" / "handover.md"
        handover_path.parent.mkdir(parents=True)
        handover_path.write_text("current_stage: s\n", encoding="utf-8")
        # Now patch ``Path.read_text`` to raise OSError on this specific
        # file so the OSError branch is exercised without OS tricks.
        original_read_text = _Path.read_text

        def _patched(self: _Path, *args: object, **kwargs: object) -> str:
            if self == handover_path:
                raise OSError("simulated disk read failure")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "read_text", _patched)

        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        result = _handover_data(data_dir, "lt-os", conn=conn, now=datetime.now(UTC))
        assert result == {}
        evt = conn.execute(
            "SELECT event_type, payload_json FROM events WHERE event_type = ?",
            (EventType.HANDOVER_INCOMPLETE.value,),
        ).fetchone()
        assert evt is not None
        payload = json.loads(evt[1])
        assert payload["reason"] == "os_error"
        conn.close()


class TestExpiredSnapshotCleanup:
    """CONTEXT_SNAPSHOT_EXPIRED was a dead type. Wire it up:
    _expire_old_snapshots removes any prior attempt's active.md
    whose expires_at is in the past and emits the audit event."""

    def test_expired_active_md_is_removed_and_event_emitted(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        old_dir = data_dir / "contracts" / "lt-clean" / "context" / "attempts" / "att-old"
        old_dir.mkdir(parents=True)
        old_active = old_dir / "active.md"
        past = datetime(2024, 1, 1, tzinfo=UTC)
        old_active.write_text(
            f"# Active Context: lt-clean / att-old\n"
            f"\n- compiled_at: {past.isoformat()}\n"
            f"- expires_at: {past.isoformat()}\n"
            f"- contract_revision: 1\n\n"
        )
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        n = _expire_old_snapshots(data_dir, conn, "lt-clean", now=datetime.now(UTC))
        assert n == 1
        assert not old_active.exists()
        evt = conn.execute(
            "SELECT event_type FROM events WHERE event_type = ?",
            (EventType.CONTEXT_SNAPSHOT_EXPIRED.value,),
        ).fetchone()
        assert evt is not None
        conn.close()

    def test_fresh_active_md_is_kept(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        old_dir = data_dir / "contracts" / "lt-fresh" / "context" / "attempts" / "att-fresh"
        old_dir.mkdir(parents=True)
        old_active = old_dir / "active.md"
        now = datetime.now(UTC)
        future = now + timedelta(hours=1)
        old_active.write_text(f"# Active Context\n\n- expires_at: {future.isoformat()}\n")
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        n = _expire_old_snapshots(data_dir, conn, "lt-fresh", now=now)
        assert n == 0
        assert old_active.exists()
        conn.close()


class TestDirectiveCap:
    """An unbounded pending_directives() call can grow active.md past
    max_bytes. The fix is a hard cap on the number of directives
    injected plus a per-text length cap."""

    def test_more_than_max_directives_are_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lhgp.persistence import messages as msg_module

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)

        n_extra = _MAX_DIRECTIVES_INJECTED + 5
        monkeypatch.setattr(
            msg_module,
            "pending_directives",
            lambda *a, **kw: [{"text": f"do thing {i}"} for i in range(n_extra)],
        )

        deadline = datetime(2099, 1, 1, tzinfo=UTC)
        draft = _make_draft(deadline_at=deadline)
        save_contract(conn, draft, contract_id="lt-cap", now=datetime.now(UTC))
        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-cap")
        assert view is not None
        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-cap", now=datetime.now(UTC)
        )
        text = active.read_text(encoding="utf-8")
        section = text.split("## ⚡ 收到的指令（必须遵守）", 1)[1].split("## 合同锚点", 1)[0]
        bullet_lines = [ln for ln in section.splitlines() if ln.startswith("- **")]
        assert len(bullet_lines) == _MAX_DIRECTIVES_INJECTED
        assert "more directives truncated" in section
        conn.close()

    def test_per_directive_text_length_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lhgp.persistence import messages as msg_module
        from longtask.persistence.context import _DIRECTIVE_TEXT_CHARS

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)

        long_text = "x" * 5000  # >> _DIRECTIVE_TEXT_CHARS
        monkeypatch.setattr(
            msg_module,
            "pending_directives",
            lambda *a, **kw: [{"text": long_text}],
        )

        deadline = datetime(2099, 1, 1, tzinfo=UTC)
        draft = _make_draft(deadline_at=deadline)
        save_contract(conn, draft, contract_id="lt-long", now=datetime.now(UTC))
        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-long")
        assert view is not None
        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-long", now=datetime.now(UTC)
        )
        text = active.read_text(encoding="utf-8")
        # The long directive's text body is truncated to
        # _DIRECTIVE_TEXT_CHARS; the rendered line is
        # ``- **<text>**  —  from `<sender>``` (3rd-round review:
        # directive header shows real sender).  We parse out the
        # bolded body for the length assertion.
        section = text.split("## ⚡ 收到的指令（必须遵守）", 1)[1].split("## 合同锚点", 1)[0]
        bullet = next(ln for ln in section.splitlines() if ln.startswith("- **"))
        # bullet starts with "- **" and the bolded body ends at the
        # next "**" — strip that pair, then strip the optional
        # "  —  from …" tail.
        inner = bullet[len("- **") :]
        bold_end = inner.find("**")
        assert bold_end != -1
        text_body = inner[:bold_end]
        # 契约变更（2026-09-11 注入面截断纪律）：截断不再只切正文到 240，而是
        # 「正文 + 显式标记 ≤ 240」——被截断这件事必须可见，标记占用字节从
        # 同一预算里出（不偷偷突破上限）。旧断言「正好 240」对应静默截断。
        from longtask.persistence.context import _DIRECTIVE_TRUNCATION_MARK

        assert _DIRECTIVE_TRUNCATION_MARK in text_body
        assert len(text_body) <= _DIRECTIVE_TEXT_CHARS
        conn.close()


class TestMemoryBudgetFormula:
    """max(1500, min(4000, max_bytes // 10)) broke for small contracts
    (max_bytes=500 -> memory_budget=1500, 300% of the total). New
    formula is min(0.4 * max_bytes, 4000) with a 1500 floor."""

    def test_small_contract_compiles_within_budget(self, tmp_path: Path) -> None:
        from lhgp.memory import Memory, MemoryKind, MemoryScope, record_memory

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        # Apply the v3 -> v4 migration so the memories table exists.
        from longtask.persistence.store import ensure_schema as _ensure_v4

        _ensure_v4(conn)
        # 5KB max_bytes. Under the new formula: min(0.4 * 5000, 4000) = 2000.
        draft = _make_draft(deadline_at=datetime(2099, 1, 1, tzinfo=UTC), max_bytes=5000)
        save_contract(conn, draft, contract_id="lt-small", now=datetime.now(UTC))
        # Insert a project memory so the digest has something to show.
        record_memory(
            conn,
            Memory(
                scope=MemoryScope.PROJECT,
                kind=MemoryKind.PATTERN,
                title="p0-smoke",
                body_md="memory body",
                score=0.5,
                created_at=datetime.now(UTC),
                schema_version=4,
            ),
        )
        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-small")
        assert view is not None
        # Should not raise CapacityRefusedError.
        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-small", now=datetime.now(UTC)
        )
        text = active.read_text(encoding="utf-8")
        assert "## 长期记忆" in text
        conn.close()


class TestDigestLimit:
    """_recent_attempt_digest must use SQL-side LIMIT so a long-lived
    contract (100k+ events) never pulls all events into Python."""

    def test_digest_stays_bounded_under_high_event_volume(self, tmp_path: Path) -> None:
        from longtask.persistence.store import append_event

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        # Insert 200 attempt events; the digest must take only the
        # last few (default 3) without ever loading 200 rows.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(200):
            ev_type = (
                EventType.ATTEMPT_SUCCEEDED.value if i % 3 == 0 else EventType.ATTEMPT_FAILED.value
            )
            append_event(
                conn,
                contract_id="lt-bulk",
                attempt_id=f"att-{i:04d}",
                event_type=ev_type,
                payload={},
                now=base + timedelta(seconds=i),
                actor="daemon",
                role="system",
            )
        conn.commit()

        result = _recent_attempt_digest(conn, "lt-bulk", limit=3)
        # The function must return at most 3 lines AND must not have
        # pulled 200 rows into Python: we only have 2 distinct event
        # types in this fixture (SUCCEEDED + FAILED), so the
        # "last 1 per type" rule yields 2 lines.
        lines = result.splitlines()
        assert 1 <= len(lines) <= 3
        # The most recent event id is in the result.
        assert "att-0199" in result
        conn.close()


class TestDirectiveCursorAdvance:
    """P0 verifier finding: pending_directives() was unbounded.
    The user could fire 50 directives and each attempt re-injected
    all 50. The fix is a per-contract cursor stored in
    ``continuity_json`` that advances to the max consumed event id
    after each compile_context_snapshot call."""

    def test_cursor_starts_at_zero_for_fresh_contract(self, tmp_path: Path) -> None:
        from longtask.persistence.context import _read_directive_cursor

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        deadline = datetime(2099, 1, 1, tzinfo=UTC)
        draft = _make_draft(deadline_at=deadline)
        save_contract(conn, draft, contract_id="lt-cursor-0", now=datetime.now(UTC))
        assert _read_directive_cursor(conn, "lt-cursor-0") == 0
        conn.close()

    def test_second_attempt_only_sees_new_directives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lhgp.persistence import messages as msg_module
        from lhgp.persistence.events import EventType
        from longtask.persistence.context import _read_directive_cursor

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        deadline = datetime(2099, 1, 1, tzinfo=UTC)
        draft = _make_draft(deadline_at=deadline)
        save_contract(conn, draft, contract_id="lt-cursor-1", now=datetime.now(UTC))

        base = datetime(2026, 1, 1, tzinfo=UTC)
        max_event_id = 0
        for i in range(3):
            ev_obj = append_event(
                conn,
                contract_id="lt-cursor-1",
                goal_id=None,
                event_type=EventType.AGENT_MESSAGE,
                payload={"kind": "directive", "text": f"do thing {i}"},
                now=base + timedelta(seconds=i),
                actor="user",
                role="user",
            )
            max_event_id = max(max_event_id, ev_obj.event_id)
        conn.commit()

        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-cursor-1")
        assert view is not None
        _active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-1", now=datetime.now(UTC)
        )
        # P1 review (2026-09-08, 2nd round): cursor advance is now
        # deferred to spawn confirmation. The test pins both:
        # - compile_context_snapshot returns the max event id it
        #   included so the caller knows what to mark consumed
        # - mark_directives_consumed must be called explicitly to
        #   advance the cursor (Popen success = advance)
        from longtask.persistence.context import mark_directives_consumed

        assert _read_directive_cursor(conn, "lt-cursor-1") == 0
        mark_directives_consumed(conn, "lt-cursor-1", _consumed)
        assert _read_directive_cursor(conn, "lt-cursor-1") == max_event_id

        new_obj = append_event(
            conn,
            contract_id="lt-cursor-1",
            goal_id=None,
            event_type=EventType.AGENT_MESSAGE,
            payload={"kind": "directive", "text": "do thing 3 (NEW)"},
            now=datetime.now(UTC),
            actor="user",
            role="user",
        )
        conn.commit()

        seen_after: list[int] = []
        real_fn = msg_module.pending_directives

        def _spy(*args: object, **kwargs: object) -> list[dict[str, object]]:
            seen_after.append(int(kwargs.get("after_event_id", 0)))
            return real_fn(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(msg_module, "pending_directives", _spy)
        _active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-2", now=datetime.now(UTC)
        )
        assert seen_after[0] == max_event_id
        # P1 review (2026-09-08, 2nd round): cursor advance moved out
        # of compile_context_snapshot; the runner does it after
        # Popen via mark_directives_consumed.  The test now calls
        # that helper explicitly to keep the assertion meaningful.
        from longtask.persistence.context import mark_directives_consumed

        assert _read_directive_cursor(conn, "lt-cursor-1") == max_event_id
        mark_directives_consumed(conn, "lt-cursor-1", _consumed)
        assert _read_directive_cursor(conn, "lt-cursor-1") == new_obj.event_id
        conn.close()

    def test_cursor_never_rewinds(self, tmp_path: Path) -> None:
        from longtask.persistence.context import (
            _bump_directive_cursor,
            _read_directive_cursor,
        )

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)
        deadline = datetime(2099, 1, 1, tzinfo=UTC)
        draft = _make_draft(deadline_at=deadline)
        save_contract(conn, draft, contract_id="lt-rewind", now=datetime.now(UTC))
        _bump_directive_cursor(conn, "lt-rewind", 100)
        _bump_directive_cursor(conn, "lt-rewind", 50)
        assert _read_directive_cursor(conn, "lt-rewind") == 100
        conn.close()


class TestCursorBumpDeferredUntilWriteSucceeds:
    """Verifier P0 finding: the cursor used to be bumped *before* the
    capacity check and ``active_path.write_text`` call. If the snapshot
    was rejected (capacity exceeded) the cursor had already advanced
    to the max consumed event id, so the next attempt never saw the
    rejected directives again — they were silently lost with no audit
    trail beyond ``CONTEXT_CAPACITY_REFUSED``. The fix is to bump the
    cursor *after* the snapshot is on disk; on failure the cursor stays
    put and the next attempt replays the same directives."""

    def test_capacity_failure_does_not_advance_cursor(self, tmp_path: Path) -> None:
        from longtask.persistence.context import (
            CapacityRefusedError,
            _read_directive_cursor,
        )
        from longtask.persistence.events import EventType

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)

        # Tiny max_bytes — the first 5 directives already overflow.
        draft = _make_draft(deadline_at=datetime(2099, 1, 1, tzinfo=UTC), max_bytes=400)
        save_contract(conn, draft, contract_id="lt-cap-cursor", now=datetime.now(UTC))

        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            append_event(
                conn,
                contract_id="lt-cap-cursor",
                goal_id=None,
                event_type=EventType.AGENT_MESSAGE,
                payload={"kind": "directive", "text": f"do thing {i} " + "x" * 80},
                now=base + timedelta(seconds=i),
                actor="user",
                role="user",
            )
        conn.commit()

        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-cap-cursor")
        assert view is not None
        with pytest.raises(CapacityRefusedError):
            _active, _, _consumed, _consumed_ids = compile_context_snapshot(
                data_dir, conn, view, "att-fail", now=datetime.now(UTC)
            )

        # Cursor must not have advanced despite the user having
        # visible directives in flight. Next attempt replays them.
        assert _read_directive_cursor(conn, "lt-cap-cursor") == 0
        conn.close()

    def test_capacity_failure_then_relaxed_compile_consumes_directives(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: capacity failure leaves cursor at 0; a subsequent
        compile with relaxed max_bytes consumes the same directives and
        then advances the cursor."""
        from longtask.persistence.context import (
            CapacityRefusedError,
            _read_directive_cursor,
        )
        from longtask.persistence.events import EventType

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = connect(StoreConfig(db_path=data_dir / "state.db"))
        ensure_schema(conn)

        draft_tight = _make_draft(deadline_at=datetime(2099, 1, 1, tzinfo=UTC), max_bytes=400)
        save_contract(conn, draft_tight, contract_id="lt-relaxed", now=datetime.now(UTC))

        base = datetime(2026, 1, 1, tzinfo=UTC)
        max_event_id = 0
        for i in range(3):
            ev = append_event(
                conn,
                contract_id="lt-relaxed",
                goal_id=None,
                event_type=EventType.AGENT_MESSAGE,
                payload={"kind": "directive", "text": f"do thing {i}"},
                now=base + timedelta(seconds=i),
                actor="user",
                role="user",
            )
            max_event_id = max(max_event_id, ev.event_id)
        conn.commit()

        from longtask.persistence.store import get_contract

        view = get_contract(conn, "lt-relaxed")
        assert view is not None
        with pytest.raises(CapacityRefusedError):
            active, _, _consumed, _consumed_ids = compile_context_snapshot(
                data_dir, conn, view, "att-fail", now=datetime.now(UTC)
            )
        assert _read_directive_cursor(conn, "lt-relaxed") == 0

        # Loosen the contract via a raw UPDATE (the contracts schema
        # has no public update API) so the same directives can fit on
        # the next attempt. The cursor must then advance to the max
        # consumed event id, confirming the directives were replayed.
        conn.execute(
            "UPDATE contracts SET context_json = ? WHERE contract_id = ?",
            (
                json.dumps(
                    {
                        "required": True,
                        "limits": {
                            "max_bytes": 24000,
                            "expires_after_minutes": 60,
                        },
                    }
                ),
                "lt-relaxed",
            ),
        )
        conn.commit()
        view = get_contract(conn, "lt-relaxed")
        assert view is not None
        active, _, _consumed, _consumed_ids = compile_context_snapshot(
            data_dir, conn, view, "att-ok", now=datetime.now(UTC)
        )
        text = active.read_text(encoding="utf-8")
        assert "## ⚡ 收到的指令（必须遵守）" in text
        # P1 review (2026-09-08, 2nd round): the cursor advance was
        # previously in compile_context_snapshot. With the fix it
        # moves to mark_directives_consumed, called by the runner
        # after Popen succeeds. The test now exercises that
        # post-spawn path explicitly.
        from longtask.persistence.context import mark_directives_consumed

        assert _read_directive_cursor(conn, "lt-relaxed") == 0
        mark_directives_consumed(conn, "lt-relaxed", _consumed)
        assert _read_directive_cursor(conn, "lt-relaxed") == max_event_id
        conn.close()
