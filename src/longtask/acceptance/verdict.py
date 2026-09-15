"""Compatibility facade for :mod:`lhgp.acceptance.verdict`."""

from lhgp.acceptance.verdict import (
    VERDICT_MARKER,
    ModelVerdict,
    VerdictSourceLossError,
    merge_evidence,
    parse_verdict_block,
    verdict_from_output,
)

__all__ = [
    "VERDICT_MARKER",
    "ModelVerdict",
    "VerdictSourceLossError",
    "merge_evidence",
    "parse_verdict_block",
    "verdict_from_output",
]
