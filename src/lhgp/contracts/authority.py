"""Contract authority and executor/model/role allowlists (SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ALLOWED_CONTROLS: frozenset[str] = frozenset({"notify", "followup", "steer", "spawn"})


@dataclass(frozen=True, slots=True)
class AuthorityBinding:
    """One explicit executor, model, and role binding."""

    executor_id: str
    models: tuple[str, ...]
    roles: tuple[str, ...]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.executor_id.strip():
            errors.append("authority.executors[].executor_id must not be empty")
        if not self.models:
            errors.append("authority.executors[].models must not be empty")
        if not self.roles:
            errors.append("authority.executors[].roles must not be empty")
        return errors


@dataclass(frozen=True, slots=True)
class Authority:
    """Default-deny contract authorization policy."""

    executor_policy: str = "closed"
    executors: tuple[AuthorityBinding, ...] = field(default_factory=tuple)
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    allowed_controls: tuple[str, ...] = field(default_factory=tuple)
    allow_parallel: bool = False

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.executor_policy not in ("closed", "explicit_allow"):
            errors.append(
                f"authority.executor_policy must be 'closed' or 'explicit_allow', "
                f"got {self.executor_policy!r}"
            )
        for idx, binding in enumerate(self.executors):
            for err in binding.validate():
                errors.append(f"authority.executors[{idx}].{err}")
        unknown = set(self.allowed_controls) - ALLOWED_CONTROLS
        if unknown:
            errors.append(f"authority.allowed_controls has unknown entries: {sorted(unknown)}")
        return errors


def models_allow(authority: Authority, *, binding: AuthorityBinding, model: str) -> bool:
    del authority
    return bool(binding.models) and ("*" in binding.models or model in binding.models)


def roles_allow(authority: Authority, *, binding: AuthorityBinding, role: str) -> bool:
    del authority
    return role in binding.roles


def binding_for_executor(authority: Authority, executor_id: str) -> AuthorityBinding | None:
    for binding in authority.executors:
        if binding.executor_id == executor_id:
            return binding
    return None


def to_dict(authority: Authority) -> dict[str, Any]:
    return {
        "executor_policy": authority.executor_policy,
        "executors": [
            {
                "executor_id": binding.executor_id,
                "models": list(binding.models),
                "roles": list(binding.roles),
            }
            for binding in authority.executors
        ],
        "required_capabilities": list(authority.required_capabilities),
        "allowed_controls": list(authority.allowed_controls),
        "allow_parallel": authority.allow_parallel,
    }


def from_dict(data: dict[str, Any] | None) -> Authority:
    if not isinstance(data, dict) or not data:
        return Authority()
    executors_raw = data.get("executors") or []
    executors = tuple(
        AuthorityBinding(
            executor_id=str(item["executor_id"]),
            models=tuple(str(model) for model in item.get("models") or ()),
            roles=tuple(str(role) for role in item.get("roles") or ()),
        )
        for item in executors_raw
        if isinstance(item, dict)
    )
    return Authority(
        executor_policy=str(data.get("executor_policy") or "closed"),
        executors=executors,
        required_capabilities=tuple(str(cap) for cap in data.get("required_capabilities") or ()),
        allowed_controls=tuple(str(control) for control in data.get("allowed_controls") or ()),
        allow_parallel=_strict_bool(data.get("allow_parallel", False), "allow_parallel"),
    )


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


__all__ = [
    "ALLOWED_CONTROLS",
    "Authority",
    "AuthorityBinding",
    "binding_for_executor",
    "from_dict",
    "models_allow",
    "roles_allow",
    "to_dict",
]
