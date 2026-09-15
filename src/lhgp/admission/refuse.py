"""Admission refusal reasons (SPEC §10.4), owned by ``lhgp``."""

from dataclasses import dataclass
from enum import StrEnum


class AdmissionRefuseCode(StrEnum):
    """Business-level refusal codes, distinct from RPC errors."""

    NO_ELIGIBLE_EXECUTOR = "no-eligible-executor"
    ACCEPTANCE_NOT_EXECUTABLE = "acceptance-not-executable"
    VERIFICATION_RESERVE_INSUFFICIENT = "verification-reserve-insufficient"
    FORECAST_P90_EXCEEDS_DEADLINE = "forecast-p90-exceeds-deadline"
    BUDGET_BELOW_ONE_VERIFICATION = "budget-below-one-verification"
    POLICY_DENY = "policy-deny"


@dataclass(frozen=True, slots=True)
class AdmissionRefusedError(Exception):
    """Default-deny refusal returned by ``goal/prepare``."""

    code: AdmissionRefuseCode
    reason: str
    detail: dict[str, str] | None = None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.reason}"


__all__ = ["AdmissionRefuseCode", "AdmissionRefusedError"]
