"""Compatibility facade for :mod:`lhgp.adapters.manifest`."""

from lhgp.adapters.manifest import (
    MANIFEST_PROTOCOL_VERSION,
    Capabilities,
    Enforcement,
    ExecutorManifest,
    SandboxCapability,
)

__all__ = [
    "MANIFEST_PROTOCOL_VERSION",
    "Capabilities",
    "Enforcement",
    "ExecutorManifest",
    "SandboxCapability",
]
