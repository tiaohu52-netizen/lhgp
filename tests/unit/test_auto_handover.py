"""Auto-handover detector: ``check_handover_due`` in
``src/longtask/persistence/context.py``.

When an attempt's active.md grows toward ``policy.max_bytes``, the
next attempt risks running out of context window mid-execution. The
detector returns True so the daemon loop can write a ``handover.md``
with the most recent submitted evaluation's next_action and emit a
HANDOVER_DUE event before the next attempt starts.

Branches covered:
- missing active.md → False (no snapshot to judge)
- small file → False
- >90% of max_bytes → True (overdue, no debounce)
- >60% of max_bytes with no recent HANDOVER_DUE → True (warm warning,
  also writes the event so a re-call within the debounce window
  returns False)
- >60% of max_bytes WITH a recent HANDOVER_DUE event → False (the
  previous iteration already triggered)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longtask.contracts.schema import Acceptance, Budget, ContractDraft
from longtask.persistence.context import (
    HANDOVER_DUE_DEBOUNCE_SECONDS,
    HANDOVER_DUE_EVENT_TYPE,
    check_handover_due,
)
from longtask.persistence.store import (
    StoreConfig,
    append_event,
    connect,
    ensure_schema,
    save_contract,
)

pytestmark = pytest.mark.unit


def _make_draft(*, max_bytes: int) -> ContractDraft:
    return ContractDraft(
        title="auto-handover smoke",
        objective="verify size-driven handover",
        deadline_at=datetime(2099, 1, 1, tzinfo=UTC),
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
        context={
            "required": True,
            "limits": {"max_bytes": max_bytes, "expires_after_minutes": 60},
        },
    )


def _open_store(tmp_path: Path, contract_id: str, max_bytes: int):
    """Open a file-backed store + save a contract. Returns ``(conn, root)``."""
    root = tmp_path / "data"
    root.mkdir()
    conn = connect(StoreConfig(db_path=root / "state.db"))
    ensure_schema(conn)
    save_contract(
        conn, _make_draft(max_bytes=max_bytes), contract_id=contract_id, now=datetime.now(UTC)
    )
    return conn, root


def _write_active_md(root: Path, contract_id: str, attempt_id: str, size: int) -> Path:
    """Create ``attempts/<attempt_id>/active.md`` with the given byte size."""
    attempt_dir = root / "contracts" / contract_id / "context" / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    active_path = attempt_dir / "active.md"
    active_path.write_bytes(b"a" * size)
    return active_path


class TestCheckHandoverDue:
    def test_missing_active_md_returns_false(self, tmp_path: Path) -> None:
        conn, _root = _open_store(tmp_path, "lt-no-active", max_bytes=1000)
        # No file is written under contracts/lt-no-active/context/...
        assert check_handover_due(conn, "lt-no-active", "att-missing") is False
        conn.close()

    def test_small_file_returns_false(self, tmp_path: Path) -> None:
        conn, root = _open_store(tmp_path, "lt-small", max_bytes=10_000)
        _write_active_md(root, "lt-small", "att-1", size=100)  # 1% — well below low_water
        assert check_handover_due(conn, "lt-small", "att-1") is False
        conn.close()

    def test_over_90_percent_returns_true_and_skips_debounce(self, tmp_path: Path) -> None:
        conn, root = _open_store(tmp_path, "lt-overdue", max_bytes=1000)
        # 95% — past high_water (0.9). A previous HANDOVER_DUE event must NOT
        # suppress the overdue branch.
        _write_active_md(root, "lt-overdue", "att-1", size=950)
        append_event(
            conn,
            contract_id="lt-overdue",
            attempt_id="att-1",
            event_type=HANDOVER_DUE_EVENT_TYPE,
            payload={"note": "stale"},
            now=datetime.now(UTC),
            actor="daemon",
            role="system",
        )
        assert check_handover_due(conn, "lt-overdue", "att-1") is True
        conn.close()

    def test_warm_warning_without_recent_event_fires(self, tmp_path: Path) -> None:
        conn, root = _open_store(tmp_path, "lt-warm", max_bytes=1000)
        # 70% — between low_water (0.6) and high_water (0.9).
        _write_active_md(root, "lt-warm", "att-1", size=700)
        # No prior HANDOVER_DUE for this attempt → must fire.
        assert check_handover_due(conn, "lt-warm", "att-1") is True
        # The function wrote a HANDOVER_DUE event; verify it landed.
        rows = conn.execute(
            "SELECT event_type FROM events "
            "WHERE contract_id = ? AND attempt_id = ? AND event_type = ?",
            ("lt-warm", "att-1", HANDOVER_DUE_EVENT_TYPE),
        ).fetchall()
        assert len(rows) == 1
        # And a re-check within the debounce window now returns False.
        assert check_handover_due(conn, "lt-warm", "att-1") is False
        conn.close()

    def test_warm_warning_with_recent_event_does_not_fire(self, tmp_path: Path) -> None:
        conn, root = _open_store(tmp_path, "lt-recent", max_bytes=1000)
        # 70% — warm band.
        _write_active_md(root, "lt-recent", "att-1", size=700)
        # Inject a HANDOVER_DUE event dated ``now - 5s`` — well inside the
        # 60-second debounce window.
        now = datetime.now(UTC)
        append_event(
            conn,
            contract_id="lt-recent",
            attempt_id="att-1",
            event_type=HANDOVER_DUE_EVENT_TYPE,
            payload={"note": "already handed over"},
            now=now - timedelta(seconds=5),
            actor="daemon",
            role="system",
        )
        assert check_handover_due(conn, "lt-recent", "att-1") is False
        # The detector must not write a second HANDOVER_DUE — we only had
        # one to begin with.
        rows = conn.execute(
            "SELECT event_type FROM events "
            "WHERE contract_id = ? AND attempt_id = ? AND event_type = ?",
            ("lt-recent", "att-1", HANDOVER_DUE_EVENT_TYPE),
        ).fetchall()
        assert len(rows) == 1
        conn.close()

    def test_stale_recent_event_fires_again(self, tmp_path: Path) -> None:
        """A HANDOVER_DUE older than the debounce window does not suppress a
        re-fire. Without this, a snapshot that crosses 60% and then keeps
        growing would never see a second warning."""
        conn, root = _open_store(tmp_path, "lt-stale", max_bytes=1000)
        _write_active_md(root, "lt-stale", "att-1", size=700)
        # Stale HANDOVER_DUE: older than HANDOVER_DUE_DEBOUNCE_SECONDS.
        stale_at = datetime.now(UTC) - timedelta(seconds=HANDOVER_DUE_DEBOUNCE_SECONDS + 30)
        append_event(
            conn,
            contract_id="lt-stale",
            attempt_id="att-1",
            event_type=HANDOVER_DUE_EVENT_TYPE,
            payload={"note": "old"},
            now=stale_at,
            actor="daemon",
            role="system",
        )
        assert check_handover_due(conn, "lt-stale", "att-1") is True
        conn.close()

    def test_unknown_contract_returns_false(self, tmp_path: Path) -> None:
        conn, _root = _open_store(tmp_path, "lt-exists", max_bytes=1000)
        # Contract ``lt-ghost`` is never saved.
        assert check_handover_due(conn, "lt-ghost", "att-1") is False
        conn.close()
