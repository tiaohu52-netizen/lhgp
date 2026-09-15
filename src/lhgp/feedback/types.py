"""P6 feedback loop data types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EvaluationVerdict(StrEnum):
    """User's verdict on a finished contract.

    Distinct from machine :class:`AcceptanceStatus` because the user can
    accept a contract whose machine checks all passed (satisfied=true)
    and still rate it 2/5 with comments; the two axes are independent.
    """

    ACCEPT = "accept"
    PARTIAL = "partial"
    REJECT = "reject"


class EvaluationRating(StrEnum):
    """Five-point scale. Stored as string for SQL portability and human
    readability; ordered via :attr:`_RATING_ORDER` when a numeric weight
    is needed (e.g. for quality scoring).
    """

    UNSATISFACTORY = "1"
    POOR = "2"
    NEUTRAL = "3"
    GOOD = "4"
    EXCELLENT = "5"


_RATING_ORDER: dict[str, int] = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
}


def rating_int(rating: EvaluationRating) -> int:
    return _RATING_ORDER[rating.value]


@dataclass(frozen=True, slots=True)
class UserEvaluation:
    contract_id: str
    contract_revision: int
    evaluator: str
    rating: EvaluationRating
    verdict: EvaluationVerdict
    comments: str = ""
    attempt_id: str | None = None
    evaluation_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_db_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "contract_id": self.contract_id,
            "contract_revision": self.contract_revision,
            "attempt_id": self.attempt_id,
            "evaluator": self.evaluator,
            "rating": self.rating.value,
            "verdict": self.verdict.value,
            "comments": self.comments,
            "created_at": self.created_at.isoformat(),
        }
        if self.evaluation_id is not None:
            row["evaluation_id"] = self.evaluation_id
        return row

    @classmethod
    def from_db_row(cls, row: Any) -> UserEvaluation:
        return _evaluation_from_row(row, _USER_EVAL_COL_ORDER, cls)


@dataclass(frozen=True, slots=True)
class AcceptanceDiff:
    """Snapshot diff between attempt and final state for one contract.

    ``snapshot_before_json`` is the attempt's reported artifact set (paths,
    sizes, hashes if available). ``snapshot_after_json`` is the final
    workspace state at the time the contract closed. ``files_changed_json``
    is a JSON array of {path, action: created|modified|deleted, size_bytes}.
    """

    contract_id: str
    contract_revision: int
    snapshot_before: dict[str, Any] = field(default_factory=dict)
    snapshot_after: dict[str, Any] = field(default_factory=dict)
    files_changed: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    attempt_id: str | None = None
    diff_id: int | None = None
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_db_row(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "contract_id": self.contract_id,
            "contract_revision": self.contract_revision,
            "attempt_id": self.attempt_id,
            "snapshot_before_json": _json(self.snapshot_before),
            "snapshot_after_json": _json(self.snapshot_after),
            "files_changed_json": _json(self.files_changed),
            "summary": self.summary,
            "computed_at": self.computed_at.isoformat(),
        }

    @classmethod
    def from_db_row(cls, row: Any) -> AcceptanceDiff:
        return _diff_from_row(row, _DIFF_COL_ORDER, cls)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _column_value(row: Any, col_order: dict[str, int], keymap: dict[str, str], key: str) -> Any:
    """Single source of truth for the Row/tuple dual-mode row access.

    Accepts both sqlite3.Row (has .keys()) and plain tuple; looks up by
    name when possible, falling back to positional index. This is the
    only place that needs the isinstance-style branching; callers just
    pass the row and key.
    """
    if key in keymap:
        return row[keymap[key]]
    return row[col_order[key]]


def _build_keymap(row: Any) -> dict[str, str]:
    """Map lowercased column names to actual column names for sqlite3.Row.

    Returns an empty dict for plain tuples (callers fall back to the
    positional ``col_order``).
    """
    if hasattr(row, "keys"):
        return {k.lower(): k for k in row}
    return {}


def _evaluation_from_row(
    row: Any, col_order: dict[str, int], cls: type[UserEvaluation]
) -> UserEvaluation:
    keymap = _build_keymap(row)

    def get(key: str) -> Any:
        return _column_value(row, col_order, keymap, key)

    rating_str = get("rating")
    return cls(
        evaluation_id=int(get("evaluation_id")) if get("evaluation_id") is not None else None,
        contract_id=str(get("contract_id")),
        contract_revision=int(get("contract_revision")),
        attempt_id=get("attempt_id"),
        evaluator=str(get("evaluator")),
        rating=EvaluationRating(str(rating_str)),
        verdict=EvaluationVerdict(str(get("verdict"))),
        comments=str(get("comments") or ""),
        created_at=datetime.fromisoformat(str(get("created_at"))),
    )


def _diff_from_row(
    row: Any, col_order: dict[str, int], cls: type[AcceptanceDiff]
) -> AcceptanceDiff:
    keymap = _build_keymap(row)

    def get(key: str) -> Any:
        return _column_value(row, col_order, keymap, key)

    return cls(
        diff_id=int(get("diff_id")) if get("diff_id") is not None else None,
        contract_id=str(get("contract_id")),
        contract_revision=int(get("contract_revision")),
        attempt_id=get("attempt_id"),
        snapshot_before=json.loads(str(get("snapshot_before_json") or "{}")),
        snapshot_after=json.loads(str(get("snapshot_after_json") or "{}")),
        files_changed=json.loads(str(get("files_changed_json") or "[]")),
        summary=str(get("summary") or ""),
        computed_at=datetime.fromisoformat(str(get("computed_at"))),
    )


# Positional column orderings for tuple-mode row access (when sqlite3.Row
# is unavailable, e.g. unit tests that open a raw connection).
_USER_EVAL_COL_ORDER = {
    "evaluation_id": 0,
    "contract_id": 1,
    "contract_revision": 2,
    "attempt_id": 3,
    "evaluator": 4,
    "rating": 5,
    "verdict": 6,
    "comments": 7,
    "created_at": 8,
}
_DIFF_COL_ORDER = {
    "diff_id": 0,
    "contract_id": 1,
    "contract_revision": 2,
    "attempt_id": 3,
    "snapshot_before_json": 4,
    "snapshot_after_json": 5,
    "files_changed_json": 6,
    "summary": 7,
    "computed_at": 8,
}
