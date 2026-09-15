"""Compatibility facade for :mod:`lhgp.adapters.base`."""

from lhgp.adapters.base import (
    AttemptInput,
    ExecutorAdapter,
    PreparedLaunch,
    PrepareRefusedError,
)

__all__ = [
    "AttemptInput",
    "ExecutorAdapter",
    "PrepareRefusedError",
    "PreparedLaunch",
]
