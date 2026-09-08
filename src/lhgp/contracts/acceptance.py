"""Acceptance contract fields (SPEC §4 and §5.2), owned by ``lhgp``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lhgp.acceptance.checks import CheckSpec, parse_check

VALID_VERIFIER_KINDS: frozenset[str] = frozenset({"cross_check", "none"})


@dataclass(frozen=True, slots=True)
class Acceptance:
    """Acceptance standard, checks, and verifier policy."""

    standard: str
    checks: tuple[str | CheckSpec, ...]
    verifier: str = "cross_check"
    # Structured acceptance spec (acceptance/spec.py). When present, the
    # dispatch loop composes typed-check outcomes against the spec's boolean
    # structure instead of treating verifier success as automatic pass.
    # Stays ``None`` on contracts that do not declare a staged spec; legacy
    # behavior (verifier success == pass) is preserved for those.
    spec: dict[str, Any] | None = None
    # Hash of the spec content captured when the contract was prepared. The
    # gate enforcer rejects any later spec edit that does not bump the
    # contract revision, so the spec binding cannot be silently swapped.
    spec_hash: str | None = field(default=None)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.standard.strip():
            errors.append("acceptance.standard must not be empty")
        if not self.checks:
            errors.append("acceptance.checks must have at least one item")
        for index, check in enumerate(self.checks):
            if isinstance(check, CheckSpec):
                for error in _validate_check(check):
                    errors.append(f"acceptance.checks[{index}].{error}")
            elif not isinstance(check, str) or not check.strip():
                errors.append(f"acceptance.checks[{index}] must be a non-empty string or object")
        if self.verifier not in VALID_VERIFIER_KINDS:
            errors.append(f"acceptance.verifier unknown: {self.verifier}")
        if self.spec is not None:
            from lhgp.acceptance.spec import validate_spec

            errors.extend(f"acceptance.spec.{e}" for e in validate_spec(self.spec))
        return errors

    @classmethod
    def from_values(
        cls,
        standard: str,
        checks: tuple[str | dict[str, Any], ...],
        verifier: str,
        spec: dict[str, Any] | None = None,
        spec_hash: str | None = None,
    ) -> Acceptance:
        return cls(
            standard=standard,
            checks=tuple(parse_check(item) for item in checks),
            verifier=verifier,
            spec=spec,
            spec_hash=spec_hash,
        )


def _validate_check(check: CheckSpec) -> list[str]:
    errors: list[str] = []
    if not check.target.strip():
        errors.append("target must not be empty")
    if check.kind.value == "command-exit-zero" and "argv" in check.args:
        argv = check.args["argv"]
        if not isinstance(argv, list) or any(
            not isinstance(item, str) or not item for item in argv
        ):
            errors.append("args.argv must be a list of non-empty strings")
    return errors


__all__ = ["VALID_VERIFIER_KINDS", "Acceptance"]
