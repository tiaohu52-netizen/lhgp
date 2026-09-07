"""Canonical LHGP contract namespace.

These modules are compatibility facades over the single ``longtask.contracts``
implementation.  Keeping one implementation prevents version or state forks
while callers migrate imports incrementally.
"""

from lhgp.contracts.acceptance import VALID_VERIFIER_KINDS, Acceptance
from lhgp.contracts.attention import VALID_NOTIFY_ON, Attention, QuietHours
from lhgp.contracts.authority import ALLOWED_CONTROLS, Authority, AuthorityBinding
from lhgp.contracts.budget import DEFAULT_VERIFICATION_RESERVED, Budget
from lhgp.contracts.continuity import Continuity
from lhgp.contracts.contract_draft import SCHEMA_VERSION, ContractDraft
from lhgp.contracts.contract_view import (
    FROZEN_FIELDS,
    AcceptanceStatus,
    AttemptRole,
    AttemptState,
    BlockReason,
    ContractState,
    DeadlineStatus,
    Enforcement,
    EventActor,
    from_state_dict,
    to_state_dict,
)
from lhgp.contracts.plan import ALLOWED_ACTIONS, Plan, PlanStep, PlanValidation
from lhgp.contracts.resume import ResumeBrief, build_resume_brief
from lhgp.contracts.validation import validate_draft, validate_raw

__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_CONTROLS",
    "DEFAULT_VERIFICATION_RESERVED",
    "FROZEN_FIELDS",
    "SCHEMA_VERSION",
    "VALID_NOTIFY_ON",
    "VALID_VERIFIER_KINDS",
    "Acceptance",
    "AcceptanceStatus",
    "AttemptRole",
    "AttemptState",
    "Attention",
    "Authority",
    "AuthorityBinding",
    "BlockReason",
    "Budget",
    "Continuity",
    "ContractDraft",
    "ContractState",
    "DeadlineStatus",
    "Enforcement",
    "EventActor",
    "Plan",
    "PlanStep",
    "PlanValidation",
    "QuietHours",
    "ResumeBrief",
    "build_resume_brief",
    "from_state_dict",
    "to_state_dict",
    "validate_draft",
    "validate_raw",
]
