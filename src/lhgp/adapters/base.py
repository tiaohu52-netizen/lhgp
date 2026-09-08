"""Canonical executor-adapter protocol (SPEC §11.3, §12.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from lhgp.adapters.handles import ExternalRunHandle
    from lhgp.adapters.manifest import ExecutorManifest
    from lhgp.contracts.schema import AttemptRole, Enforcement


@dataclass(frozen=True, slots=True)
class AttemptInput:
    """Structured prepare/spawn input derived from a frozen contract revision."""

    attempt_id: str
    contract_id: str
    revision: int
    lease_generation: int
    role: AttemptRole
    contract_snapshot: dict[str, Any]
    handover_path: str
    workspace_root: str
    budget_remaining: dict[str, int]
    partition_id: str | None = None
    context_snapshot_path: str | None = None
    task_prompt: str | None = None
    session_token: str | None = None
    # A2A scoping: the registry executor_id of the agent that will
    # receive the snapshot.  Used to filter directed directives (those
    # with ``to_agent`` set) so a message addressed to B does not
    # leak into A's snapshot.  Defaults to the attempt_id for
    # backward compat with adapters that do not propagate it.
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    """Structured launch declaration; adapters must never accept shell text."""

    argv: tuple[str, ...]
    cwd: str | None
    env_allowlist: tuple[str, ...]
    enforcement: Enforcement
    context_snapshot_path: str | None = None


class PrepareRefusedError(Exception):
    """Constraint compilation failed; fail closed instead of silently degrading."""


class ExecutorAdapter(Protocol):
    """Minimal language-neutral adapter surface for an executor resource."""

    @property
    def id(self) -> str: ...

    def describe(self) -> ExecutorManifest: ...

    def health(self) -> bool: ...

    def prepare(self, input_: AttemptInput) -> PreparedLaunch: ...

    def spawn(self, input_: AttemptInput, launch: PreparedLaunch) -> str: ...

    def run_handle(self, attempt_id: str) -> ExternalRunHandle | None: ...

    def reattach(self, handle: ExternalRunHandle) -> bool: ...

    def observe(self, attempt_id: str) -> dict[str, Any]: ...

    def cancel(self, attempt_id: str, reason: str) -> None: ...

    def collect(self, attempt_id: str) -> dict[str, Any]: ...


__all__ = [
    "AttemptInput",
    "ExecutorAdapter",
    "PrepareRefusedError",
    "PreparedLaunch",
]
