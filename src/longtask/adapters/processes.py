"""Compatibility facade for :mod:`lhgp.adapters.processes`."""

from lhgp.adapters.processes import (
    IDENTITY_TOLERANCE_SECONDS,
    TREE_KILL_TIMEOUT_SECONDS,
    identity_matches,
    process_alive,
    process_start_time,
    safe_process_group,
    taskkill_argv,
    terminate_pid,
    terminate_tree,
)

__all__ = [
    "IDENTITY_TOLERANCE_SECONDS",
    "TREE_KILL_TIMEOUT_SECONDS",
    "identity_matches",
    "process_alive",
    "process_start_time",
    "safe_process_group",
    "taskkill_argv",
    "terminate_pid",
    "terminate_tree",
]
