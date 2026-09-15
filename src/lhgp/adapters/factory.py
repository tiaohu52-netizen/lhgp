"""Canonical adapter factory for registry dispatch (SPEC §8.1, §12)."""

from __future__ import annotations

from lhgp.adapters.base import ExecutorAdapter
from lhgp.adapters.fake_executor import FakeExecutor
from lhgp.adapters.registry import RegistryEntry
from lhgp.adapters.subprocess_adapter import SubprocessAdapter

__all__ = ["build_adapter"]


def build_adapter(entry: RegistryEntry) -> ExecutorAdapter | None:
    """Construct a registered adapter; unknown kinds fail closed."""
    if entry.kind == "subprocess":
        return SubprocessAdapter(manifest=entry.to_manifest(), launch=entry.launch)
    if entry.kind == "fake":
        return FakeExecutor()
    return None
