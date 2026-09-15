"""Canonical LHGP adapter namespace.

The package exports only the stable adapter surface.  Individual legacy
modules remain available during migration, but are not re-exported through a
wildcard that could accidentally widen this public API.
"""

from lhgp.adapters.base import (
    AttemptInput,
    ExecutorAdapter,
    PreparedLaunch,
    PrepareRefusedError,
)
from lhgp.adapters.handles import (
    EXTERNAL_STATE_UNKNOWN,
    RECOVERY_NONRECOVERABLE,
    RECOVERY_POLL,
    RECOVERY_REATTACH,
    RECOVERY_STRATEGIES,
    ExternalRunHandle,
    parse_legacy_session_ref,
)
from lhgp.adapters.manifest import (
    MANIFEST_PROTOCOL_VERSION,
    Capabilities,
    ExecutorManifest,
    SandboxCapability,
)
from lhgp.adapters.registry import CostHint, ExecutorRegistry, LaunchSpec, RegistryEntry

__all__ = [
    "EXTERNAL_STATE_UNKNOWN",
    "MANIFEST_PROTOCOL_VERSION",
    "RECOVERY_NONRECOVERABLE",
    "RECOVERY_POLL",
    "RECOVERY_REATTACH",
    "RECOVERY_STRATEGIES",
    "AttemptInput",
    "Capabilities",
    "CostHint",
    "ExecutorAdapter",
    "ExecutorManifest",
    "ExecutorRegistry",
    "ExternalRunHandle",
    "LaunchSpec",
    "PrepareRefusedError",
    "PreparedLaunch",
    "RegistryEntry",
    "SandboxCapability",
    "parse_legacy_session_ref",
]
