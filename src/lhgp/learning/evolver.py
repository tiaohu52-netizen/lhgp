"""P6: auto-evolve ``templates/`` from high-quality contracts.

The user explicitly chose "auto_evolve" mode: contracts whose quality
score clears the threshold become new template files under
``templates/`` so future ``prepare`` calls can ``use`` them.

The evolver is the only module in :mod:`lhgp.learning` that writes
to disk. It targets a sibling ``templates/`` directory: the lhgp data
root's ``templates/`` subdirectory, falling back to the package
bundled templates when the data root is not configured.

Naming: ``auto-<contract_id>-r<contract_revision>.json``. We never
overwrite an existing file; on collision we append a numeric suffix.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lhgp.contracts.contract_view_entity import ContractView
from lhgp.feedback.types import EvaluationRating
from lhgp.learning.extractor import (
    extract_template_signals,
)
from lhgp.learning.types import TemplateSignal

_AUTO_PREFIX = "auto-"
_QUALITY_THRESHOLD = 0.7
_MIN_RATING = EvaluationRating.GOOD


def _templates_dir() -> Path:
    """Locate the templates directory. Prefers data-root; falls back to package."""
    from lhgp.persistence.paths import default_data_root

    try:
        return default_data_root() / "templates"
    except Exception:  # noqa: S110 — best-effort fallback to package templates
        pass
    # package-bundled fallback
    pkg = Path(__file__).resolve().parents[2] / "templates"
    return pkg if pkg.exists() else Path("templates")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return s[:48] or "contract"


def _render_template(signal: TemplateSignal, contract: ContractView) -> dict[str, Any]:
    """Render a templates/-shaped JSON document for a high-quality contract."""
    draft = contract.draft
    return {
        "id": f"{_AUTO_PREFIX}{signal.contract_id}-r{signal.contract_revision}",
        "name": signal.title_hint or signal.contract_id,
        "derived_from": {
            "contract_id": signal.contract_id,
            "contract_revision": signal.contract_revision,
            "score": signal.quality.overall,
        },
        "objective": draft.objective,
        "title": draft.title,
        "deadline_default_hours": 168.0,
        "deadline_at_isoformat": None,
        "acceptance": {
            "standard": draft.acceptance.standard,
            "checks": [
                {"kind": kind, "rationale": "auto-evolved from high-quality contract"}
                for kind in signal.acceptance_vocabulary
            ]
            or [{"kind": "structure-valid", "rationale": "default"}],
            "verifier": "cross_check",
        },
        "hard_constraints": {
            "file_effects": {"mode": "workspace-write"},
        },
        "soft_guidance": signal.notes or "auto-evolved template",
        "schema_version": 2,
        "auto_evolved_at": datetime.now(UTC).isoformat(),
    }


def _existing_names(target: Path) -> set[str]:
    if not target.exists():
        return set()
    return {p.stem for p in target.glob("auto-*.json")}


def _unique_path(target: Path, base_id: str) -> Path:
    existing = _existing_names(target)
    candidate = target / f"{base_id}.json"
    if candidate.stem not in existing:
        return candidate
    n = 2
    while (target / f"{base_id}-n{n}.json").stem in existing:
        n += 1
    return target / f"{base_id}-n{n}.json"


@dataclass(frozen=True, slots=True)
class EvolutionResult:
    """Result of one evolver run. Empty list means no new templates written."""

    written: tuple[Path, ...] = ()
    skipped_low_quality: int = 0
    skipped_existing: int = 0


def evolve_from_signals(
    signals: Iterable[TemplateSignal],
    contracts_by_id: dict[str, ContractView],
    target_dir: Path | None = None,
) -> EvolutionResult:
    """Materialise qualifying signals as new template files."""
    target = target_dir or _templates_dir()
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    skipped_low = 0
    skipped_dup = 0
    for sig in signals:
        if sig.quality.overall < _QUALITY_THRESHOLD:
            skipped_low += 1
            continue
        contract = contracts_by_id.get(sig.contract_id)
        if contract is None:
            continue
        base_id = f"{_AUTO_PREFIX}{sig.contract_id}-r{sig.contract_revision}"
        path = _unique_path(target, base_id)
        if path.exists():
            skipped_dup += 1
            continue
        doc = _render_template(sig, contract)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return EvolutionResult(
        written=tuple(written),
        skipped_low_quality=skipped_low,
        skipped_existing=skipped_dup,
    )


def auto_evolve(
    conn: sqlite3.Connection,
    *,
    min_rating: EvaluationRating = _MIN_RATING,
    limit: int = 50,
) -> EvolutionResult:
    """One-shot: mine signals, then write qualifying ones as new templates."""
    signals = extract_template_signals(conn, min_rating=min_rating, limit=limit)
    # Load contract views lazily to avoid a circular import.
    from lhgp.persistence.store import get_contract

    contracts: dict[str, ContractView] = {}
    for sig in signals:
        if sig.contract_id not in contracts:
            try:
                view = get_contract(conn, sig.contract_id)
            except Exception:  # noqa: S112 — contract deleted/garbled, skip
                continue
            if view is None:
                continue
            contracts[sig.contract_id] = view
    return evolve_from_signals(signals, contracts)


class TemplateEvolver:
    """Facade so callers (CLI / MCP) can use the auto-evolve without picking
    thresholds by hand. Holds the data root for the run.
    """

    def __init__(
        self,
        min_rating: EvaluationRating = _MIN_RATING,
        quality_threshold: float = _QUALITY_THRESHOLD,
        target_dir: Path | None = None,
    ) -> None:
        self.min_rating = min_rating
        self.quality_threshold = quality_threshold
        self.target_dir = target_dir

    def run(self, conn: sqlite3.Connection, *, limit: int = 50) -> EvolutionResult:
        signals = extract_template_signals(conn, min_rating=self.min_rating, limit=limit)
        signals = [s for s in signals if s.quality.overall >= self.quality_threshold]
        from lhgp.persistence.store import get_contract

        contracts: dict[str, ContractView] = {}
        for sig in signals:
            if sig.contract_id not in contracts:
                view = get_contract(conn, sig.contract_id)
                if view is not None:
                    contracts[sig.contract_id] = view
        return evolve_from_signals(signals, contracts, target_dir=self.target_dir)
