"""Canonical executor capability manifest (SPEC §12.4)."""

from __future__ import annotations

from dataclasses import dataclass, field

from lhgp.contracts.schema import Enforcement

MANIFEST_PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class SandboxCapability:
    """Sandbox capability declaration for an executor resource."""

    file_effects: str
    network: str
    process: str
    enforcement: Enforcement


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Declared executor capabilities used during admission matching."""

    spawn: bool
    observe: bool
    cancel: bool
    notify: bool
    followup: bool
    steer: bool
    interrupt: bool
    context: str
    sandbox: SandboxCapability
    acceptance_evidence: bool


@dataclass(frozen=True, slots=True)
class ExecutorManifest:
    """Versioned executor integration declaration."""

    executor_id: str
    adapter_version: str
    transport: str
    capabilities: Capabilities
    limits: dict[str, int] = field(default_factory=dict)
    protocol_version: int = MANIFEST_PROTOCOL_VERSION


__all__ = [
    "MANIFEST_PROTOCOL_VERSION",
    "Capabilities",
    "Enforcement",
    "ExecutorManifest",
    "SandboxCapability",
]
