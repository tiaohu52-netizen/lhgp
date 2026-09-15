"""Compatibility facade for :mod:`lhgp.adapters.handles`."""

from lhgp.adapters.handles import (
    EXTERNAL_STATE_UNKNOWN,
    RECOVERY_NONRECOVERABLE,
    RECOVERY_POLL,
    RECOVERY_REATTACH,
    RECOVERY_STRATEGIES,
    ExternalRunHandle,
    parse_legacy_session_ref,
)

__all__ = [
    "EXTERNAL_STATE_UNKNOWN",
    "RECOVERY_NONRECOVERABLE",
    "RECOVERY_POLL",
    "RECOVERY_REATTACH",
    "RECOVERY_STRATEGIES",
    "ExternalRunHandle",
    "parse_legacy_session_ref",
]
