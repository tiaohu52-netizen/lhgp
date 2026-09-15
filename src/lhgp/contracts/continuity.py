"""Continuity configuration (SPEC §6.1 and §11), owned by ``lhgp``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Continuity:
    """Checkpoint, recovery grace, and capsule capacity policy."""

    checkpoint_max_age_minutes: int = 20
    checkpoint_on_material_change: bool = True
    recovery_grace_minutes: int = 5
    capsule_max_tokens: int = 12000

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.checkpoint_max_age_minutes <= 0:
            errors.append(
                f"continuity.checkpoint_max_age_minutes must be positive, "
                f"got {self.checkpoint_max_age_minutes}"
            )
        if self.recovery_grace_minutes < 0:
            errors.append(
                f"continuity.recovery_grace_minutes must be non-negative, "
                f"got {self.recovery_grace_minutes}"
            )
        if self.capsule_max_tokens <= 0:
            errors.append(
                f"continuity.capsule_max_tokens must be positive, got {self.capsule_max_tokens}"
            )
        return errors


def to_dict(continuity: Continuity) -> dict[str, Any]:
    return {
        "checkpoint_max_age_minutes": continuity.checkpoint_max_age_minutes,
        "checkpoint_on_material_change": continuity.checkpoint_on_material_change,
        "recovery_grace_minutes": continuity.recovery_grace_minutes,
        "capsule_max_tokens": continuity.capsule_max_tokens,
    }


def from_dict(data: dict[str, Any] | None) -> Continuity:
    if not isinstance(data, dict) or not data:
        return Continuity()
    return Continuity(
        checkpoint_max_age_minutes=_strict_int(
            data.get("checkpoint_max_age_minutes", 20),
            "checkpoint_max_age_minutes",
        ),
        checkpoint_on_material_change=_strict_bool(
            data.get("checkpoint_on_material_change", True),
            "checkpoint_on_material_change",
        ),
        recovery_grace_minutes=_strict_int(
            data.get("recovery_grace_minutes", 5), "recovery_grace_minutes"
        ),
        capsule_max_tokens=_strict_int(data.get("capsule_max_tokens", 12000), "capsule_max_tokens"),
    )


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{field} must be an integer")
    return int(value)


__all__ = ["Continuity", "from_dict", "to_dict"]
