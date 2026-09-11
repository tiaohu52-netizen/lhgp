"""Budget value object (SPEC §4, §6.2), owned by the canonical namespace."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_VERIFICATION_RESERVED = 2


@dataclass(frozen=True, slots=True)
class Budget:
    """Execution, escalation, output, verification, and cost limits.

    ``max_cost`` 是唯一按**钱**画的预算线（货币单位由部署约定，协议不解释）：
    台账口径见 SPEC §12.3.1（执行者自报、下界语义、按最后一次写回为准）。
    未声明（None）= 不按成本设限，与既有合同零差异。
    """

    max_dispatches: int
    max_escalations: int
    max_concurrent_attempts: int
    max_attempt_minutes: int
    max_output_bytes: int
    verification_attempts_reserved: int = DEFAULT_VERIFICATION_RESERVED
    max_cost: float | None = None

    def validate(self) -> list[str]:
        """Return violations; an empty list means the budget is valid."""
        errors: list[str] = []
        for name, value in (
            ("max_dispatches", self.max_dispatches),
            ("max_escalations", self.max_escalations),
            ("max_concurrent_attempts", self.max_concurrent_attempts),
            ("max_attempt_minutes", self.max_attempt_minutes),
            ("max_output_bytes", self.max_output_bytes),
            ("verification_attempts_reserved", self.verification_attempts_reserved),
        ):
            if value < 0 or (name != "verification_attempts_reserved" and value == 0):
                errors.append(f"budget.{name} must be positive, got {value}")
        if self.max_cost is not None and (isinstance(self.max_cost, bool) or self.max_cost <= 0):
            errors.append(f"budget.max_cost must be a positive number, got {self.max_cost!r}")
        return errors


__all__ = ["DEFAULT_VERIFICATION_RESERVED", "Budget"]
