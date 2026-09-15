"""Canonical, UI-independent persistent data directory resolution."""

from __future__ import annotations

from pathlib import Path

LEGACY_DIR_NAME = ".longtask"
NEW_DIR_NAME = ".lhgp"


def default_data_root() -> Path:
    """Prefer ``~/.lhgp`` and fall back to an existing legacy directory."""

    new = Path.home() / NEW_DIR_NAME
    legacy = Path.home() / LEGACY_DIR_NAME
    return new if new.exists() or not legacy.exists() else legacy


__all__ = ["default_data_root"]
