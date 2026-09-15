"""Canonical global kill-switch marker file operations."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

KILL_SWITCH_FILE = "KILL_SWITCH"


def is_kill_switch_active(root: Path) -> bool:
    return (root / KILL_SWITCH_FILE).is_file()


def set_kill_switch(root: Path, active: bool) -> None:
    path = root / KILL_SWITCH_FILE
    if active:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"kill switch engaged at {datetime.now(UTC).isoformat()}\n", encoding="utf-8"
        )
    else:
        path.unlink(missing_ok=True)


__all__ = ["KILL_SWITCH_FILE", "is_kill_switch_active", "set_kill_switch"]
