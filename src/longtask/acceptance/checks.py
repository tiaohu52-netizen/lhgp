"""Compatibility facade for the canonical :mod:`lhgp.acceptance.checks`."""

from lhgp.acceptance.checks import (
    CheckKind,
    CheckSpec,
    RepairBrief,
    check_identity,
    parse_check,
)

__all__ = ["CheckKind", "CheckSpec", "RepairBrief", "check_identity", "parse_check"]
