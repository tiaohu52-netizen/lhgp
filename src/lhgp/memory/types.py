"""P6+1 / memory-and-wiki Phase 2: protocol memory data types.

Memory is the protocol's *long-term* knowledge store. It is distinct from
every other layer:

  - per-contract knowledge: contract context, soft_guidance, etc. (in the
    contract draft)
  - per-attempt knowledge: context snapshot, scratch, handover (in
    longtask/persistence/context.py)
  - per-pattern knowledge: templates/auto-*.json (auto-evolved)
  - per-evaluation knowledge: user_evaluations + acceptance_diffs (P6)
  - per-vault human knowledge: docs/wiki/ (Phase 1)

Memory is *cross-contract, cross-project* knowledge. It survives contract
archival and template pruning. It is the right place for things like:

  - "When `cur.fetchall()` returns tuples, `r["col"]` TypeError — always
    handle both Row and tuple" (a pattern that bit P6 three times; not
    contract-specific, not attempt-specific, not a wiki page)
  - "When a memory's score drops below 0.3 for 30 days, the next memory
    add for the same title supersedes it" (a heuristic about the memory
    system itself; not for any specific contract)
  - "DSH 4 cordis patches all use the same failOnStartupError=false as a
    safety net" (a deployment fact, not a contract fact)

Scope defines *visibility*:
  - ``project`` — visible to any contract in this protocol (default)
  - ``domain`` — visible to contracts whose ``draft.context.domain`` matches
  - ``global`` — never expires, low score, system-level only

Kind defines *shape*:
  - ``pattern`` — a recurring shape ("X always needs Y")
  - ``rule`` — a hard rule ("X must not Y")
  - ``reference`` — pointer to a doc/wiki page or code location
  - ``heuristic`` — rule of thumb ("when X, prefer Y")
  - ``gotcha`` — anti-pattern ("don't do X because Z")
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class MemoryScope(StrEnum):
    PROJECT = "project"
    DOMAIN = "domain"
    GLOBAL = "global"


class MemoryKind(StrEnum):
    PATTERN = "pattern"
    RULE = "rule"
    REFERENCE = "reference"
    HEURISTIC = "heuristic"
    GOTCHA = "gotcha"


@dataclass(frozen=True, slots=True)
class Memory:
    """One long-term memory record.

    ``id`` is None before insert; the store sets it. ``body_md`` is the
    canonical content (the AI-readable, git-diff-friendly representation).
    """

    scope: MemoryScope
    kind: MemoryKind
    title: str
    body_md: str
    tags: tuple[str, ...] = ()
    source_contract_id: str | None = None
    source_event_id: int | None = None
    source_actor: str | None = None
    score: float = 0.0
    id: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    schema_version: int = 4

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope.value,
            "kind": self.kind.value,
            "title": self.title,
            "body_md": self.body_md,
            "tags_json": json.dumps(list(self.tags), ensure_ascii=False),
            "source_contract_id": self.source_contract_id,
            "source_event_id": self.source_event_id,
            "source_actor": self.source_actor,
            "score": float(self.score),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_db_row(cls, row: Any) -> Memory:
        # Cursor may return sqlite3.Row OR plain tuple depending on call
        # shape. sqlite3.Row supports [] by column name; plain tuple
        # does not. Use the same Row-or-tuple pattern P6 uses elsewhere.
        def _col(name: str, idx: int) -> Any:
            try:
                return row[name]
            except (KeyError, TypeError):
                try:
                    return row[idx]
                except (KeyError, TypeError, IndexError):
                    return None

        tags_raw = _col("tags_json", -1) or "[]"
        try:
            tags_list = json.loads(tags_raw) if tags_raw else []
        except (TypeError, ValueError, json.JSONDecodeError):
            tags_list = []

        def _opt_dt(s: Any) -> datetime | None:
            if not s:
                return None
            try:
                return datetime.fromisoformat(s)
            except (TypeError, ValueError):
                return None

        return cls(
            id=int(_col("id", 0)) if _col("id", 0) is not None else None,
            scope=MemoryScope(_col("scope", 1)),
            kind=MemoryKind(_col("kind", 2)),
            title=str(_col("title", 3) or ""),
            body_md=str(_col("body_md", 4) or ""),
            tags=tuple(tags_list),
            source_contract_id=_col("source_contract_id", 6),
            source_event_id=int(_col("source_event_id", 7))
            if _col("source_event_id", 7) is not None
            else None,
            source_actor=_col("source_actor", 8),
            score=float(_col("score", 9)) if _col("score", 9) is not None else 0.0,
            created_at=_opt_dt(_col("created_at", 10)),
            expires_at=_opt_dt(_col("expires_at", 11)),
            schema_version=int(_col("schema_version", 12))
            if _col("schema_version", 12) is not None
            else 4,
        )


__all__ = ["Memory", "MemoryKind", "MemoryScope"]
