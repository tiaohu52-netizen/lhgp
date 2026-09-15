"""Canonical LHGP admission namespace."""

from lhgp.admission.eligibility import CandidateFacts, EligibilityVerdict, evaluate
from lhgp.admission.offer import ExecutorCandidateView, Offer
from lhgp.admission.refuse import AdmissionRefuseCode, AdmissionRefusedError

__all__ = [
    "AdmissionRefuseCode",
    "AdmissionRefusedError",
    "CandidateFacts",
    "EligibilityVerdict",
    "ExecutorCandidateView",
    "Offer",
    "evaluate",
]
