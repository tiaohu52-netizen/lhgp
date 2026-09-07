"""Canonical LHGP Python namespace compatibility tests (P6)."""

import os
import subprocess
import sys

import lhgp.persistence.store as canonical_store_module
import longtask
import longtask.persistence.store as legacy_store_module
from lhgp import PROTOCOL_VERSION, __version__
from lhgp.acceptance import CheckSpec as CanonicalPackageCheckSpec
from lhgp.acceptance.checks import CheckSpec as CanonicalCheckSpec
from lhgp.adapters.registry import ExecutorRegistry as CanonicalExecutorRegistry
from lhgp.admission import (
    AdmissionRefuseCode as CanonicalRefuseCode,
)
from lhgp.admission import (
    AdmissionRefusedError as CanonicalRefusedError,
)
from lhgp.admission import (
    Offer as CanonicalPackageOffer,
)
from lhgp.admission import evaluate as canonical_package_evaluate_eligibility
from lhgp.admission.eligibility import evaluate as canonical_evaluate_eligibility
from lhgp.admission.offer import Offer as CanonicalOffer
from lhgp.admission.refuse import (
    AdmissionRefuseCode as CanonicalModuleRefuseCode,
)
from lhgp.admission.refuse import (
    AdmissionRefusedError as CanonicalModuleRefusedError,
)
from lhgp.cli.formatting import format_eta as canonical_format_eta
from lhgp.contracts import Acceptance as CanonicalPackageAcceptance
from lhgp.contracts import Attention as CanonicalPackageAttention
from lhgp.contracts import Budget as CanonicalPackageBudget
from lhgp.contracts import Continuity as CanonicalPackageContinuity
from lhgp.contracts import ContractDraft as CanonicalPackageContractDraft
from lhgp.contracts.acceptance import Acceptance as CanonicalAcceptance
from lhgp.contracts.attention import Attention as CanonicalAttention
from lhgp.contracts.authority import Authority as CanonicalAuthority
from lhgp.contracts.authority import AuthorityBinding as CanonicalAuthorityBinding
from lhgp.contracts.budget import Budget as CanonicalBudget
from lhgp.contracts.continuity import Continuity as CanonicalContinuity
from lhgp.contracts.contract_draft import ContractDraft as CanonicalContractDraft
from lhgp.contracts.contract_view import (
    AcceptanceStatus as CanonicalAcceptanceStatus,
)
from lhgp.contracts.contract_view import (
    AttemptRole as CanonicalAttemptRole,
)
from lhgp.contracts.contract_view import (
    AttemptState as CanonicalAttemptState,
)
from lhgp.contracts.contract_view import (
    BlockReason as CanonicalBlockReason,
)
from lhgp.contracts.contract_view import (
    ContractState as CanonicalContractState,
)
from lhgp.contracts.contract_view import (
    DeadlineStatus as CanonicalDeadlineStatus,
)
from lhgp.contracts.contract_view import (
    Enforcement as CanonicalEnforcement,
)
from lhgp.contracts.contract_view import (
    EventActor as CanonicalEventActor,
)
from lhgp.contracts.contract_view_entity import ContractView as CanonicalContractView
from lhgp.contracts.schema import ContractDraft as CanonicalSchemaContractDraft
from lhgp.contracts.state_machine import (
    is_valid_transition as canonical_is_valid_transition,
)
from lhgp.contracts.validation import validate_draft as canonical_validate_draft
from lhgp.forecast import Forecast as CanonicalPackageForecast
from lhgp.forecast.model import Forecast as CanonicalForecast
from lhgp.persistence import (
    EventInput as CanonicalPackageEventInput,
)
from lhgp.persistence import (
    EventType as CanonicalPackageEventType,
)
from lhgp.persistence import (
    Notification as CanonicalPackageNotification,
)
from lhgp.persistence import (
    StoreConfig as CanonicalPackageStoreConfig,
)
from lhgp.persistence import (
    connect as canonical_package_connect,
)
from lhgp.persistence import (
    enqueue_notification as canonical_package_enqueue_notification,
)
from lhgp.persistence import (
    ensure_schema as canonical_package_ensure_schema,
)
from lhgp.persistence.decisions import set_next_decision_at as canonical_set_next_decision_at
from lhgp.persistence.errors import StoreError as CanonicalStoreError
from lhgp.persistence.events import EventType as CanonicalEventType
from lhgp.persistence.events_query import append_event as canonical_append_event
from lhgp.persistence.notifications import enqueue_notification as canonical_notification_enqueue
from lhgp.persistence.paths import default_data_root as canonical_default_data_root
from lhgp.persistence.store import connect as canonical_connect
from lhgp.persistence.types import StoredLease as CanonicalStoredLease
from lhgp.promoter.escalation import decide as canonical_decide_escalation
from lhgp.promoter.killswitch import is_kill_switch_active as canonical_kill_switch_active
from lhgp.promoter.lease import check_write_fence as canonical_check_write_fence
from lhgp.promoter.records import _record_attempt as canonical_record_attempt
from lhgp.promoter.urgency import classify as canonical_classify
from lhgp.rpc import Method as CanonicalPackageMethod
from lhgp.rpc.client import call_unix_socket as canonical_call_unix_socket
from lhgp.rpc.errors import ErrorCode as CanonicalErrorCode
from lhgp.rpc.executor_api import handle_control_interrupt as canonical_handle_control_interrupt
from lhgp.rpc.handlers import HANDLERS as CANONICAL_HANDLERS
from lhgp.rpc.handlers.executor import handle_executor_list as canonical_handle_executor_list
from lhgp.rpc.handlers.protocol import handle_protocol_events as canonical_handle_protocol_events
from lhgp.rpc.handlers.protocol import handle_protocol_hello as canonical_handle_protocol_hello
from lhgp.rpc.methods import Method as CanonicalMethod
from lhgp.rpc.server import parse_envelope as canonical_parse_envelope
from lhgp.rpc.transport import process_lines as canonical_process_lines
from lhgp.scheduler.ticker import run_tick as canonical_run_tick
from lhgp.scheduler.wakeup import guard_needed as canonical_guard_needed
from longtask.acceptance.checks import CheckSpec as LegacyCheckSpec
from longtask.adapters.registry import ExecutorRegistry as LegacyExecutorRegistry
from longtask.admission.eligibility import evaluate as legacy_evaluate_eligibility
from longtask.admission.offer import Offer as LegacyOffer
from longtask.admission.refuse import (
    AdmissionRefuseCode as LegacyRefuseCode,
)
from longtask.admission.refuse import (
    AdmissionRefusedError as LegacyRefusedError,
)
from longtask.cli.formatting import format_eta as legacy_format_eta
from longtask.contracts.acceptance import Acceptance as LegacyAcceptance
from longtask.contracts.attention import Attention as LegacyAttention
from longtask.contracts.authority import Authority as LegacyAuthority
from longtask.contracts.authority import AuthorityBinding as LegacyAuthorityBinding
from longtask.contracts.budget import Budget as LegacyBudget
from longtask.contracts.continuity import Continuity as LegacyContinuity
from longtask.contracts.contract_draft import ContractDraft as LegacyContractDraft
from longtask.contracts.contract_view import (
    AcceptanceStatus as LegacyAcceptanceStatus,
)
from longtask.contracts.contract_view import (
    AttemptRole as LegacyAttemptRole,
)
from longtask.contracts.contract_view import (
    AttemptState as LegacyAttemptState,
)
from longtask.contracts.contract_view import (
    BlockReason as LegacyBlockReason,
)
from longtask.contracts.contract_view import (
    ContractState as LegacyContractState,
)
from longtask.contracts.contract_view import (
    DeadlineStatus as LegacyDeadlineStatus,
)
from longtask.contracts.contract_view import (
    Enforcement as LegacyEnforcement,
)
from longtask.contracts.contract_view import (
    EventActor as LegacyEventActor,
)
from longtask.contracts.contract_view_entity import ContractView as LegacyContractView
from longtask.contracts.schema import ContractDraft as LegacySchemaContractDraft
from longtask.contracts.state_machine import is_valid_transition as legacy_is_valid_transition
from longtask.contracts.validation import validate_draft as legacy_validate_draft
from longtask.forecast.model import Forecast as LegacyForecast
from longtask.persistence.decisions import set_next_decision_at as legacy_set_next_decision_at
from longtask.persistence.errors import StoreError as LegacyStoreError
from longtask.persistence.events import EventType as LegacyEventType
from longtask.persistence.events_query import append_event as legacy_append_event
from longtask.persistence.notifications import Notification as LegacyNotification
from longtask.persistence.notifications import enqueue_notification as legacy_enqueue_notification
from longtask.persistence.paths import default_data_root as legacy_default_data_root
from longtask.persistence.store import StoreConfig as LegacyStoreConfig
from longtask.persistence.store import connect as legacy_connect
from longtask.persistence.store import ensure_schema as legacy_ensure_schema
from longtask.persistence.types import EventInput as LegacyEventInput
from longtask.persistence.types import StoredLease as LegacyStoredLease
from longtask.promoter.escalation import decide as legacy_decide_escalation
from longtask.promoter.killswitch import is_kill_switch_active as legacy_kill_switch_active
from longtask.promoter.lease import check_write_fence as legacy_check_write_fence
from longtask.promoter.records import _record_attempt as legacy_record_attempt
from longtask.promoter.urgency import classify as legacy_classify
from longtask.rpc import Method as LegacyPackageMethod
from longtask.rpc.client import call_unix_socket as legacy_call_unix_socket
from longtask.rpc.errors import ErrorCode as LegacyErrorCode
from longtask.rpc.executor_api import handle_control_interrupt as legacy_handle_control_interrupt
from longtask.rpc.handlers.executor import handle_executor_list as legacy_handle_executor_list
from longtask.rpc.handlers.protocol import handle_protocol_events as legacy_handle_protocol_events
from longtask.rpc.methods import Method as LegacyMethod
from longtask.rpc.server import parse_envelope as legacy_parse_envelope
from longtask.rpc.transport import process_lines as legacy_process_lines
from longtask.scheduler.ticker import run_tick as legacy_run_tick
from longtask.scheduler.wakeup import guard_needed as legacy_guard_needed


def test_canonical_namespace_matches_legacy_runtime_identity() -> None:
    """The new namespace must not fork protocol or package version state."""

    assert PROTOCOL_VERSION == longtask.PROTOCOL_VERSION
    assert __version__ == longtask.__version__


def test_contract_namespace_reexports_single_implementation() -> None:
    """Contract facades must preserve class identity during migration."""

    assert CanonicalContractDraft is LegacyContractDraft
    assert CanonicalPackageContractDraft is CanonicalContractDraft
    assert CanonicalBudget is LegacyBudget
    assert CanonicalPackageBudget is CanonicalBudget
    assert CanonicalAcceptance is LegacyAcceptance
    assert CanonicalPackageAcceptance is CanonicalAcceptance
    assert CanonicalAttention is LegacyAttention
    assert CanonicalPackageAttention is CanonicalAttention
    assert CanonicalAuthority is LegacyAuthority
    assert CanonicalAuthorityBinding is LegacyAuthorityBinding
    assert CanonicalContinuity is LegacyContinuity
    assert CanonicalPackageContinuity is CanonicalContinuity
    assert CanonicalContractState is LegacyContractState
    assert CanonicalDeadlineStatus is LegacyDeadlineStatus
    assert CanonicalAcceptanceStatus is LegacyAcceptanceStatus
    assert CanonicalBlockReason is LegacyBlockReason
    assert CanonicalAttemptRole is LegacyAttemptRole
    assert CanonicalAttemptState is LegacyAttemptState
    assert CanonicalEnforcement is LegacyEnforcement
    assert CanonicalEventActor is LegacyEventActor
    assert CanonicalContractView is LegacyContractView
    assert canonical_is_valid_transition is legacy_is_valid_transition
    assert canonical_validate_draft is legacy_validate_draft
    assert CanonicalSchemaContractDraft is LegacySchemaContractDraft is CanonicalContractDraft


def test_persistence_namespace_reexports_single_implementation() -> None:
    """Persistence facades must preserve callable identity during migration."""

    assert canonical_connect is legacy_connect
    assert CanonicalStoredLease is LegacyStoredLease
    assert CanonicalEventType is LegacyEventType
    assert canonical_append_event is legacy_append_event
    assert CanonicalStoreError is LegacyStoreError
    assert canonical_default_data_root is legacy_default_data_root
    assert canonical_set_next_decision_at is legacy_set_next_decision_at
    assert CanonicalPackageEventInput is LegacyEventInput
    assert CanonicalPackageEventType is LegacyEventType
    assert CanonicalPackageStoreConfig is LegacyStoreConfig
    assert canonical_package_connect is legacy_connect
    assert canonical_package_ensure_schema is legacy_ensure_schema
    assert CanonicalPackageNotification is LegacyNotification
    assert canonical_package_enqueue_notification is legacy_enqueue_notification
    assert canonical_notification_enqueue is legacy_enqueue_notification


def test_canonical_store_exports_match_legacy_contract() -> None:
    """Explicit canonical exports must stay complete during migration."""
    assert set(canonical_store_module.__all__) == set(legacy_store_module.__all__)
    for name in canonical_store_module.__all__:
        assert getattr(canonical_store_module, name) is getattr(legacy_store_module, name)


def test_adapter_namespace_reexports_single_implementation() -> None:
    """Adapter facades must preserve registry class identity during migration."""

    assert CanonicalExecutorRegistry is LegacyExecutorRegistry


def test_rpc_namespace_reexports_protocol_types() -> None:
    """RPC facades must preserve enum identity during migration."""

    assert canonical_call_unix_socket is legacy_call_unix_socket
    assert CanonicalPackageMethod is LegacyPackageMethod is CanonicalMethod
    assert canonical_process_lines is legacy_process_lines
    assert canonical_handle_protocol_events is legacy_handle_protocol_events
    assert canonical_handle_executor_list is legacy_handle_executor_list
    assert canonical_handle_control_interrupt is legacy_handle_control_interrupt
    assert CANONICAL_HANDLERS[CanonicalMethod.PROTOCOL_HELLO] is canonical_handle_protocol_hello
    assert CanonicalMethod is LegacyMethod
    assert canonical_parse_envelope is legacy_parse_envelope
    assert CanonicalErrorCode is LegacyErrorCode


def test_scheduler_and_promoter_namespaces_reexport_functions() -> None:
    """Scheduling facades must preserve pure-function identity."""

    assert canonical_guard_needed is legacy_guard_needed
    assert canonical_classify is legacy_classify
    assert canonical_kill_switch_active is legacy_kill_switch_active
    assert canonical_check_write_fence is legacy_check_write_fence
    assert canonical_record_attempt is legacy_record_attempt
    assert canonical_decide_escalation is legacy_decide_escalation
    assert canonical_run_tick is legacy_run_tick


def test_cli_namespace_reexports_pure_helpers() -> None:
    """CLI facades must preserve helper identity without changing entrypoints."""

    assert canonical_format_eta is legacy_format_eta


def test_supporting_namespaces_reexport_single_implementation() -> None:
    """Acceptance, admission and forecast facades preserve class identity."""

    assert CanonicalCheckSpec is LegacyCheckSpec
    assert CanonicalPackageCheckSpec is CanonicalCheckSpec
    assert CanonicalOffer is LegacyOffer
    assert canonical_evaluate_eligibility is legacy_evaluate_eligibility
    assert canonical_package_evaluate_eligibility is canonical_evaluate_eligibility
    assert CanonicalPackageOffer is CanonicalOffer
    assert CanonicalRefuseCode is CanonicalModuleRefuseCode is LegacyRefuseCode
    assert CanonicalRefusedError is CanonicalModuleRefusedError is LegacyRefusedError
    assert CanonicalRefuseCode.POLICY_DENY.value == "policy-deny"
    assert CanonicalForecast is LegacyForecast
    assert CanonicalPackageForecast is CanonicalForecast


def test_canonical_modules_preserve_module_execution_entrypoints() -> None:
    """Canonical module paths must behave like their installed console scripts."""

    cli = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "lhgp.cli.main", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert cli.stdout.strip().startswith("lhgp ")

    mcp = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "lhgp.mcp_server", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "LHGP" in mcp.stdout


def test_p6_feedback_namespace_reexports_single_implementation() -> None:
    """P6 feedback facade: longtask.feedback must be the same module objects
    as lhgp.feedback so daemon code and external callers see the same
    classes (no duplicate dataclass identity)."""
    from lhgp.feedback import (
        AcceptanceDiff as CanonicalAcceptanceDiff,
    )
    from lhgp.feedback import (
        EvaluationRating as CanonicalEvaluationRating,
    )
    from lhgp.feedback import (
        EvaluationVerdict as CanonicalEvaluationVerdict,
    )
    from lhgp.feedback import (
        UserEvaluation as CanonicalUserEvaluation,
    )
    from lhgp.feedback import (
        compute_acceptance_diff as canonical_compute_acceptance_diff,
    )
    from lhgp.feedback import (
        get_latest_diff as canonical_get_latest_diff,
    )
    from lhgp.feedback import (
        list_diffs as canonical_list_diffs,
    )
    from lhgp.feedback import (
        list_evaluations as canonical_list_evaluations,
    )
    from lhgp.feedback import (
        record_diff as canonical_record_diff,
    )
    from lhgp.feedback import (
        record_evaluation as canonical_record_evaluation,
    )
    from longtask import feedback as legacy_feedback

    assert CanonicalUserEvaluation is legacy_feedback.UserEvaluation
    assert CanonicalAcceptanceDiff is legacy_feedback.AcceptanceDiff
    assert CanonicalEvaluationRating is legacy_feedback.EvaluationRating
    assert CanonicalEvaluationVerdict is legacy_feedback.EvaluationVerdict
    assert canonical_record_evaluation is legacy_feedback.record_evaluation
    assert canonical_record_diff is legacy_feedback.record_diff
    assert canonical_get_latest_diff is legacy_feedback.get_latest_diff
    assert canonical_list_evaluations is legacy_feedback.list_evaluations
    assert canonical_list_diffs is legacy_feedback.list_diffs
    assert canonical_compute_acceptance_diff is legacy_feedback.compute_acceptance_diff


def test_p6_learning_namespace_reexports_single_implementation() -> None:
    """P6 learning facade: longtask.learning must mirror lhgp.learning
    so the auto-evolve pipeline has one canonical class identity."""
    from lhgp.learning import (
        DraftSuggestion as CanonicalDraftSuggestion,
    )
    from lhgp.learning import (
        QualityScore as CanonicalQualityScore,
    )
    from lhgp.learning import (
        TemplateEvolver as CanonicalTemplateEvolver,
    )
    from lhgp.learning import (
        TemplateSignal as CanonicalTemplateSignal,
    )
    from lhgp.learning import (
        auto_evolve as canonical_auto_evolve,
    )
    from lhgp.learning import (
        extract_template_signals as canonical_extract_template_signals,
    )
    from lhgp.learning import (
        score_contract_quality as canonical_score_contract_quality,
    )
    from lhgp.learning import (
        suggest_draft_improvements as canonical_suggest_draft_improvements,
    )
    from longtask import learning as legacy_learning

    assert CanonicalQualityScore is legacy_learning.QualityScore
    assert CanonicalTemplateSignal is legacy_learning.TemplateSignal
    assert CanonicalDraftSuggestion is legacy_learning.DraftSuggestion
    assert CanonicalTemplateEvolver is legacy_learning.TemplateEvolver
    assert canonical_auto_evolve is legacy_learning.auto_evolve
    assert canonical_extract_template_signals is (legacy_learning.extract_template_signals)
    assert canonical_score_contract_quality is (legacy_learning.score_contract_quality)
    assert canonical_suggest_draft_improvements is (legacy_learning.suggest_draft_improvements)


def test_p6_portfolio_namespace_reexports_single_implementation() -> None:
    """P6 portfolio facade: longtask.portfolio must mirror lhgp.portfolio
    so MCP / CLI tools return values match the canonical module identity."""
    from lhgp.portfolio import (
        ContractSummary as CanonicalContractSummary,
    )
    from lhgp.portfolio import (
        PortfolioSnapshot as CanonicalPortfolioSnapshot,
    )
    from lhgp.portfolio import (
        portfolio_summary as canonical_portfolio_summary,
    )
    from lhgp.portfolio import (
        trace_contract as canonical_trace_contract,
    )
    from longtask import portfolio as legacy_portfolio

    assert CanonicalContractSummary is legacy_portfolio.ContractSummary
    assert CanonicalPortfolioSnapshot is legacy_portfolio.PortfolioSnapshot
    assert canonical_portfolio_summary is legacy_portfolio.portfolio_summary
    assert canonical_trace_contract is legacy_portfolio.trace_contract


def test_p6_enforcement_namespace_reexports_single_implementation() -> None:
    """P6 enforcement facade: longtask.enforcement must mirror
    lhgp.enforcement so deadline-level actions emitted by the daemon
    are the same class instance the operator / MCP tools read."""
    from lhgp.enforcement import (
        DeadlineEnforcer as CanonicalDeadlineEnforcer,
    )
    from lhgp.enforcement import (
        DeadlineLevel as CanonicalDeadlineLevel,
    )
    from lhgp.enforcement import (
        EnforcementAction as CanonicalEnforcementAction,
    )
    from lhgp.enforcement import (
        compute_deadline_level as canonical_compute_deadline_level,
    )
    from lhgp.enforcement import (
        format_deadline_report as canonical_format_deadline_report,
    )
    from lhgp.enforcement import (
        render_text as canonical_render_text,
    )
    from longtask import enforcement as legacy_enforcement

    assert CanonicalDeadlineEnforcer is legacy_enforcement.DeadlineEnforcer
    assert CanonicalDeadlineLevel is legacy_enforcement.DeadlineLevel
    assert CanonicalEnforcementAction is legacy_enforcement.EnforcementAction
    assert canonical_compute_deadline_level is (legacy_enforcement.compute_deadline_level)
    assert canonical_format_deadline_report is (legacy_enforcement.format_deadline_report)
    assert canonical_render_text is legacy_enforcement.render_text
    # Pin the enum values: a re-import that accidentally changed the
    # spelling would silently break MCP clients that read level.value.
    assert CanonicalDeadlineLevel.NORMAL.value == "normal"
    assert CanonicalDeadlineLevel.WARNING.value == "warning"
    assert CanonicalDeadlineLevel.URGENT.value == "urgent"
    assert CanonicalDeadlineLevel.BREACHED.value == "breached"


def test_p2_memory_and_p3_flow_namespace_reexport_single_implementation() -> None:
    """P2/P3 facade: ``longtask.memory`` / ``longtask.flow`` must mirror
    their ``lhgp.*`` canonicals so the same class instance is visible
    to the daemon, the CLI, and any external caller (e.g. an MCP
    client) that imports from either path."""

    from lhgp.flow import (
        Flow as CanonicalFlow,
    )
    from lhgp.flow import (
        render_excalidraw as canonical_render_excalidraw,
    )
    from lhgp.flow import (
        render_mermaid as canonical_render_mermaid,
    )
    from lhgp.flow import (
        walk_source as canonical_walk_source,
    )
    from lhgp.memory import (
        Memory as CanonicalMemory,
    )
    from lhgp.memory import (
        MemoryIndex as CanonicalMemoryIndex,
    )
    from lhgp.memory import (
        MemoryKind as CanonicalMemoryKind,
    )
    from lhgp.memory import (
        MemoryScope as CanonicalMemoryScope,
    )
    from lhgp.memory import (
        make_pattern_memory as canonical_make_pattern_memory,
    )
    from lhgp.memory import (
        record_memory as canonical_record_memory,
    )
    from longtask.flow import (
        Flow as LegacyFlow,
    )
    from longtask.flow import (
        render_excalidraw as legacy_render_excalidraw,
    )
    from longtask.flow import (
        render_mermaid as legacy_render_mermaid,
    )
    from longtask.flow import (
        walk_source as legacy_walk_source,
    )
    from longtask.memory import (
        Memory as LegacyMemory,
    )
    from longtask.memory import (
        MemoryIndex as LegacyMemoryIndex,
    )
    from longtask.memory import (
        MemoryKind as LegacyMemoryKind,
    )
    from longtask.memory import (
        MemoryScope as LegacyMemoryScope,
    )
    from longtask.memory import (
        make_pattern_memory as legacy_make_pattern_memory,
    )
    from longtask.memory import (
        record_memory as legacy_record_memory,
    )

    assert CanonicalMemory is LegacyMemory
    assert CanonicalMemoryIndex is LegacyMemoryIndex
    assert CanonicalMemoryKind is LegacyMemoryKind
    assert CanonicalMemoryScope is LegacyMemoryScope
    assert canonical_record_memory is legacy_record_memory
    assert canonical_make_pattern_memory is legacy_make_pattern_memory
    assert CanonicalFlow is LegacyFlow
    assert canonical_walk_source is legacy_walk_source
    assert canonical_render_mermaid is legacy_render_mermaid
    assert canonical_render_excalidraw is legacy_render_excalidraw


def test_entry_help_survives_non_utf8_console() -> None:
    """--help must not crash on consoles that cannot encode CJK (cp1252).

    Reproduces the GitHub windows-latest failure: argparse help containing
    Chinese descriptions raised UnicodeEncodeError under a cp1252 stdout.
    """

    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    for module in ("lhgp.cli.main", "lhgp.mcp_server"):
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", module, "--help"],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
        )
        assert "usage:" in proc.stdout.lower()


def test_legacy_cli_alias_emits_deprecation_warning(monkeypatch, capsys) -> None:
    """Legacy executable names remain usable but visibly announce migration."""

    from longtask.cli.main import main

    monkeypatch.setattr(sys, "argv", ["longtask"])
    assert main(["--version"]) == 0
    assert "deprecated" in capsys.readouterr().err
