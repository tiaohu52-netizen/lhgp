"""P6: extract signals from past contracts.

The extractor reads :class:`UserEvaluation` rows (joined with the latest
:class:`AcceptanceDiff` for the same contract) and surfaces
:class:`TemplateSignal` records. It is read-only with respect to
contracts and events; the only write path is :mod:`lhgp.learning.evolver`,
which writes new template files under ``templates/``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from lhgp.contracts.contract_view import AcceptanceStatus, ContractState
from lhgp.feedback.diff import compute_acceptance_diff
from lhgp.feedback.store import get_latest_diff, list_evaluations
from lhgp.feedback.types import EvaluationRating, EvaluationVerdict
from lhgp.learning.types import DraftSuggestion, QualityScore, TemplateSignal
from lhgp.persistence.events import EventType

_HIGH_QUALITY_THRESHOLD = 0.7  # overall score >= threshold → template candidate


def _acceptance_pass_rate(events: Iterable[sqlite3.Row]) -> float:
    """Heuristic: count accept/reject events vs total; capped at 1.0.

    Accepts both sqlite3.Row and plain tuples; for the latter, the
    event_type column must be at index 0 (this is the only SELECT shape
    we run against the events table here).

    Positive signals (one of):
      - event_type contains "passed" or "satisfied"
        (covers synthetic event types from older callers and
        ``contract/satisfied`` from the live event vocabulary)
      - event_type is ``acceptance/status-changed`` with a payload
        ``status`` of ``passed`` (read by callers that pass Rows with
        both columns; not exercised by the current cursor shape, kept
        for forward compatibility)
    """
    accepted = 0
    total = 0
    for r in events:
        try:
            et = r["event_type"]
        except (KeyError, TypeError, IndexError):
            try:
                et = r[0]
            except (KeyError, TypeError, IndexError):
                continue
        if "acceptance" in et:
            total += 1
            if "passed" in et or "satisfied" in et:
                accepted += 1
        elif et == EventType.CONTRACT_SATISFIED.value:
            total += 1
            accepted += 1
    if total == 0:
        return 0.0
    return min(1.0, accepted / total)


def score_contract_quality(
    conn: sqlite3.Connection,
    contract_id: str,
    contract_revision: int,
    workspace_root: Any = None,
) -> QualityScore | None:
    """Compute :class:`QualityScore` for one contract.

    Returns ``None`` if the contract has no user evaluation yet — quality
    is the human-anchored signal, machine pass-rate alone is not enough.
    """
    evaluations = list_evaluations(conn, contract_id=contract_id, limit=10)
    if not evaluations:
        return None
    # Use the most recent evaluation as the authoritative rating.
    ev = evaluations[0]
    user_rating_norm = (int(ev.rating) - 1) / 4.0  # 1..5 -> 0..1
    pass_rate = _acceptance_pass_rate(
        conn.execute(
            "SELECT event_type FROM events WHERE contract_id = ? AND contract_revision = ?",
            (contract_id, contract_revision),
        )
    )
    diff_eff = 1.0
    diff = get_latest_diff(conn, contract_id, contract_revision)
    if diff and workspace_root is not None:
        try:
            from pathlib import Path

            latest = compute_acceptance_diff(
                contract_id,
                contract_revision,
                Path(workspace_root),
                before=diff.snapshot_before,
            )
            before_count = len(latest.snapshot_before.get("files", []))
            after_count = len(latest.snapshot_after.get("files", []))
            if after_count > 0:
                diff_eff = max(0.0, 1.0 - abs(after_count - before_count) / max(after_count, 1))
        except (OSError, ValueError, TypeError):
            # Workspace I/O or shape errors → neutral score, do not poison
            # the rest of the function. Programming bugs (AttributeError,
            # KeyError) still surface.
            diff_eff = 0.5  # neutral
    return QualityScore.from_components(
        contract_id=contract_id,
        contract_revision=contract_revision,
        user_rating=user_rating_norm,
        acceptance_pass_rate=pass_rate,
        diff_efficiency=diff_eff,
    )


def extract_template_signals(
    conn: sqlite3.Connection,
    *,
    min_rating: EvaluationRating = EvaluationRating.GOOD,
    limit: int = 50,
) -> list[TemplateSignal]:
    """Mine user evaluations for high-quality contracts worth templating.

    A contract is a template candidate when:
      - The user rated it >= min_rating (default 4)
      - The verdict was ACCEPT or PARTIAL (not REJECT)
      - The overall quality score clears the high-quality threshold
    """
    candidates = list_evaluations(conn, limit=limit * 4)
    signals: list[TemplateSignal] = []
    for ev in candidates:
        if ev.rating < min_rating:
            continue
        if ev.verdict == EvaluationVerdict.REJECT:
            continue
        score = score_contract_quality(conn, ev.contract_id, ev.contract_revision)
        if score is None or score.overall < _HIGH_QUALITY_THRESHOLD:
            continue
        # Acceptance vocabulary: pull from events (acceptance/check kinds).
        vocab: set[str] = set()
        for r in conn.execute(
            "SELECT payload_json FROM events "
            "WHERE contract_id = ? AND contract_revision = ? "
            "AND event_type = ?",
            (
                ev.contract_id,
                ev.contract_revision,
                EventType.ACCEPTANCE_STATUS_CHANGED.value,
            ),
        ):
            try:
                # sqlite3.Row by column name; plain tuple at index 0.
                payload_text = r["payload_json"]
            except (KeyError, TypeError):
                try:
                    payload_text = r[0]
                except (KeyError, TypeError, IndexError):
                    continue
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for kind in payload.get("check_kinds", []):
                vocab.add(str(kind))
        # Objective keywords: take a tiny TF slice from comments + objective text.
        kw = _extract_keywords(ev.comments)
        signals.append(
            TemplateSignal(
                pattern_id=f"sig-{ev.contract_id}-r{ev.contract_revision}",
                contract_id=ev.contract_id,
                contract_revision=ev.contract_revision,
                quality=score,
                acceptance_vocabulary=tuple(sorted(vocab)),
                objective_keywords=kw,
                title_hint=ev.comments[:80],
                notes=ev.comments,
            )
        )
    return signals


def suggest_draft_improvements(
    conn: sqlite3.Connection,
    *,
    objective: str,
    min_rating: EvaluationRating = EvaluationRating.GOOD,
) -> list[DraftSuggestion]:
    """Return non-destructive advisory suggestions for an in-flight draft."""
    signals = extract_template_signals(conn, min_rating=min_rating, limit=20)
    suggestions: list[DraftSuggestion] = []
    seen_kinds: set[str] = set()
    for sig in signals:
        new_kinds = tuple(k for k in sig.acceptance_vocabulary if k not in seen_kinds)
        seen_kinds.update(sig.acceptance_vocabulary)
        if not new_kinds and not sig.objective_keywords:
            continue
        suggestions.append(
            DraftSuggestion(
                pattern_id=sig.pattern_id,
                add_acceptance_kinds=new_kinds,
                tighten_deadline_hours=None,
                suggest_objective_keywords=sig.objective_keywords,
                rationale=(
                    f"based on contract {sig.contract_id} r{sig.contract_revision} "
                    f"(score {sig.quality.overall:.2f})"
                ),
                evidence={
                    "contract_id": sig.contract_id,
                    "overall_score": sig.quality.overall,
                    "rating": int(min_rating),
                },
            )
        )
    return suggestions


def _extract_keywords(text: str, *, max_tokens: int = 8) -> tuple[str, ...]:
    """Cheap keyword extractor: split, lowercase, drop stopwords.

    Adequate for advisory hints; the evolved templates store the full
    objective text so consumers can re-extract if needed.
    """
    if not text:
        return ()
    stop = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "to",
        "of",
        "and",
        "or",
        "in",
        "on",
        "for",
        "with",
        "as",
        "by",
        "this",
        "that",
        "it",
        "be",
        "i",
        "we",
    }
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in text.lower().split():
        token = raw.strip(".,;:()[]{}\"'`?!")
        if not token or token in stop or len(token) < 3 or not token.isascii():
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= max_tokens:
            break
    return tuple(tokens)


def is_terminal_for_learning(state: str) -> bool:
    """Whether a contract is in a state from which we can learn.

    Mirrors the :class:`ContractState` enum but kept as a string helper
    to avoid the import cycle (this module is read by the evolver).
    """
    return state in {ContractState.SATISFIED.value, ContractState.CANCELLED.value}


def is_accepted_terminal(state: str, acceptance: str) -> bool:
    return state == ContractState.SATISFIED.value and acceptance in {
        AcceptanceStatus.PASSED.value,
        AcceptanceStatus.CANDIDATE.value,
    }
