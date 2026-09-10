"""Acceptance contract fields (SPEC §4 and §5.2), owned by ``lhgp``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lhgp.acceptance.checks import CheckSpec, parse_check

VALID_VERIFIER_KINDS: frozenset[str] = frozenset({"cross_check", "none"})

#: Payload keys that carry the content identity of the acceptance a verifier
#: actually checked.  Producers stamp them, ``user_confirm`` reads them, and
#: the match is only as strong as the agreement between the two -- so the
#: names live next to the fingerprint they carry, not at each call site.
EVIDENCE_FINGERPRINT_KEY = "acceptance_fingerprint"
EVIDENCE_SPEC_HASH_KEY = "spec_hash"


def evidence_binding(acceptance: Acceptance) -> dict[str, Any]:
    """The payload fragment that makes verifier evidence re-usable.

    Every producer of a verifier success event must merge this into the
    event payload: the runner that collects a finished attempt, and the
    reconciler that settles one after a daemon restart.  A verifier event
    without it cannot be proved to describe the current acceptance, so
    ``user_confirm`` treats it as unbound and demands a fresh run.

    ``spec_hash`` is included for continuity with the 6th-round binding and
    is still honoured as a veto; it is the caller's own label and the
    runtime cannot vouch for it, which is why ``content_fingerprint`` --
    not ``spec_hash`` -- is what the binding rests on.
    """
    return {
        EVIDENCE_FINGERPRINT_KEY: acceptance.content_fingerprint,
        EVIDENCE_SPEC_HASH_KEY: acceptance.spec_hash,
    }


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
    # Hash of the spec content, supplied by whoever prepared the contract.
    # Advisory only: the runtime never recomputes it, so it cannot prove
    # anything about the acceptance content -- an editor may leave the
    # requirement text untouched and keep the hash, or edit the text and
    # keep the hash.  Use ``content_fingerprint`` for binding; this field
    # survives because callers store it and compare it against their own
    # earlier submission.
    spec_hash: str | None = field(default=None)

    @property
    def content_fingerprint(self) -> str:
        """Stable hash over every substantive acceptance field.

        8th-round review (2026-09-10): verifier evidence used to be bound
        to ``spec_hash``, which is optional and caller-supplied -- on the
        common path where nobody filled it in, the binding compared two
        ``None`` values, found them equal, and let an edited acceptance
        complete on evidence gathered against the previous requirement.
        A caller who supplied a hash and then forgot to re-supply it while
        editing was worse off: the edit turned the current hash into
        ``None``, which also read as "equal".

        The runtime computes this fingerprint instead, over ``standard``,
        ``checks`` (typed, in order), ``verifier`` and ``spec``, so the
        identity of what was promised cannot be kept while its content
        changes.  Deterministic: the same acceptance yields the same value
        in-process and after any number of store round-trips, because
        checks are normalised back through :class:`CheckSpec` and mapping
        keys are sorted.
        """
        import hashlib
        import json

        payload = {
            "standard": self.standard,
            "verifier": self.verifier,
            "checks": [_canonical_check(check) for check in self.checks],
            "spec": self.spec,
        }
        blob = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

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


def _canonical_check(check: str | CheckSpec | dict[str, Any]) -> object:
    """Return a JSON-safe form that is identical before and after a store
    round-trip.

    A hand-built ``Acceptance`` may hold a raw mapping that never went
    through :func:`parse_check`; hashing it as-is would give a different
    value than hashing the ``CheckSpec`` it loads back into (defaulted keys
    like ``note`` would be missing), so normalise through the value type.
    Anything unparseable is left alone rather than guessed at.
    """
    if isinstance(check, CheckSpec):
        return check.to_dict()
    if isinstance(check, dict):
        try:
            return CheckSpec.from_dict(check).to_dict()
        except (TypeError, KeyError, ValueError):
            return check
    return check


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


__all__ = [
    "EVIDENCE_FINGERPRINT_KEY",
    "EVIDENCE_SPEC_HASH_KEY",
    "VALID_VERIFIER_KINDS",
    "Acceptance",
    "evidence_binding",
]
