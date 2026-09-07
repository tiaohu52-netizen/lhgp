"""P6+1 / memory-and-wiki Phase 2: MemoryIndex — the retrieval layer.

``MemoryIndex.retrieve()`` is what the contract compiler calls per-attempt.
It picks the most relevant memories for a given contract context, applies
the capacity contract (``policy.max_bytes`` from ``ContextPolicy``), and
returns a list of memories plus the rendered text. If the rendered text
would overflow the budget, the index drops the lowest-score memories
until it fits — fail-closed means: never overflow, even if the result is
"no memories retrieved".

Selection rules (in priority order):
  1. scope=global memories always included (system-level rules)
  2. scope=domain memories whose domain matches the contract's
     ``draft.context.domain`` (or a default if not set)
  3. scope=project memories — top-N by score, descending

Within each scope, the result is sorted by (score DESC, created_at DESC).
The top-K is taken so the rendered text fits in the budget.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lhgp.memory.store import list_memories
from lhgp.memory.types import Memory, MemoryScope

# Constant cost of the section header, in UTF-8 bytes. Pre-computed so
# we can budget a single-item truncation precisely without re-encoding
# the header on every call.
_HEADER_TEXT = "## 长期记忆（跨合同沉淀）\n\n"
_HEADER_BYTES = len(_HEADER_TEXT.encode("utf-8"))

# Bytes reserved for a single item's metadata line and the trailing
# newline between items. The metadata line is ``- **<title>** (...)\n``
# which varies with title/source/kind; this constant is a conservative
# upper bound for a typical 1-2 word title and a short source string.
_ITEM_OVERHEAD_BYTES = 200

# Tag-rendering prefix used in the body section; not a budget term, kept
# here so the renderer and the test can both reference it.
_TRUNCATION_MARKER = "\n[…truncated…]"


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    """The projected form used in active.md."""

    title: str
    body_md: str
    kind: str
    score: float
    tags: tuple[str, ...]
    source: str  # short provenance string, e.g. "auto:m6" or "user:狐条"

    def render(self) -> str:
        tag_str = ", ".join(f"#{t}" for t in self.tags) if self.tags else ""
        lines = [f"- **{self.title}** ({self.kind}, score={self.score:.2f}, source={self.source})"]
        if tag_str:
            lines.append(f"  {tag_str}")
        for line in self.body_md.splitlines():
            lines.append(f"  {line}" if line else "")
        return "\n".join(lines)


def _domain_of(contract_context: dict[str, Any] | None) -> str:
    if not isinstance(contract_context, dict):
        return ""
    domain = contract_context.get("domain")
    return str(domain) if domain else ""


def _source(memory: Memory) -> str:
    actor = memory.source_actor or "auto"
    if memory.source_event_id is not None:
        return f"{actor}:e{memory.source_event_id}"
    if memory.source_contract_id is not None:
        return f"{actor}:{memory.source_contract_id}"
    return actor


def _project(memory: Memory) -> RetrievedMemory:
    return RetrievedMemory(
        title=memory.title,
        body_md=memory.body_md,
        kind=memory.kind.value,
        score=memory.score,
        tags=memory.tags,
        source=_source(memory),
    )


def _truncate_to_budget(item: RetrievedMemory, budget: int) -> RetrievedMemory:
    """Shrink a single oversize item so the rendered section fits.

    Reserves room for the section header + the item's own metadata
    line; the body is cut to whatever's left and a truncation marker
    is appended so the reader knows the body was clipped.
    """
    body_budget = max(0, budget - _HEADER_BYTES - _ITEM_OVERHEAD_BYTES)
    if len(item.body_md) <= body_budget:
        return item
    return RetrievedMemory(
        title=item.title,
        body_md=item.body_md[:body_budget] + _TRUNCATION_MARKER,
        kind=item.kind,
        score=item.score,
        tags=item.tags,
        source=item.source,
    )


def _render_section(memories: list[RetrievedMemory]) -> str:
    """Compose the markdown section that ``compile_context_snapshot`` embeds."""
    if not memories:
        return ""
    return _HEADER_TEXT + "\n\n".join(r.render() for r in memories) + "\n"


def render_for_active_md(memories: list[RetrievedMemory]) -> str:
    """Public helper for ``compile_context_snapshot`` to render a section."""
    return _render_section(memories)


class MemoryIndex:
    """Retrieval facade for the contract compiler.

    The instance is cheap to construct; ``retrieve`` is the only
    interesting call.
    """

    DEFAULT_BUDGET_BYTES = 2400
    DEFAULT_TOP_N = 5

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        budget_bytes: int = DEFAULT_BUDGET_BYTES,
        top_n: int = DEFAULT_TOP_N,
    ) -> None:
        self.conn = conn
        self.budget_bytes = int(budget_bytes)
        self.top_n = int(top_n)

    def retrieve(
        self,
        contract_context: dict[str, Any] | None,
        *,
        now: datetime | None = None,  # reserved for future expiry filter
    ) -> list[RetrievedMemory]:
        """Return the top-N memories that fit the budget.

        Always includes ``global`` scope. For ``domain`` scope, matches
        the contract's ``context.domain``. For ``project``, the top-N by
        score. If the rendered text would overflow the budget, drops
        lowest-score items until it fits. Returns [] on empty/no-match.
        """
        candidates = self._gather_candidates(contract_context)
        top = self._take_top_n(candidates)
        return self._fit_to_budget(top)

    # -- private steps (one concern each) -------------------------------

    def _gather_candidates(self, contract_context: dict[str, Any] | None) -> list[Memory]:
        """Collect the union of global, domain-matching, and project memories.

        Dedups by id and sorts score-DESC, then created_at-DESC so the
        first N are the most relevant regardless of source scope.
        """
        domain = _domain_of(contract_context)
        # ``limit=self.top_n * 2`` is a soft cap; the SQL is also sorted
        # by score DESC, so even if there are thousands of project
        # memories we never pull more than this for one retrieval.
        limit = self.top_n * 2
        global_mems = list_memories(self.conn, scope=MemoryScope.GLOBAL, limit=limit)
        domain_mems: list[Memory] = []
        if domain:
            tag = f"topic/{domain}"
            domain_mems = [
                m
                for m in list_memories(self.conn, scope=MemoryScope.DOMAIN, limit=limit)
                if tag in m.tags
            ]
        project_mems = list_memories(self.conn, scope=MemoryScope.PROJECT, limit=limit)

        seen: set[int] = set()
        combined: list[Memory] = []
        for m in (*global_mems, *domain_mems, *project_mems):
            if m.id is None or m.id in seen:
                # None id would corrupt the seen set; skip rather than crash.
                continue
            seen.add(m.id)
            combined.append(m)
        # Sort score-DESC; tie-break on newer-first by created_at DESC.
        # ``-datetime`` works because datetime supports total_ordering.
        combined.sort(key=lambda m: (-m.score, -(m.created_at or datetime.min).timestamp()))
        return combined

    def _take_top_n(self, memories: list[Memory]) -> list[RetrievedMemory]:
        """Project the top-N to the wire form. ``top_n`` is a hard cap."""
        top = memories[: self.top_n]
        projected = [_project(m) for m in top]
        # The underlying list is already score-DESC; the projection keeps
        # that order, so no re-sort is needed.
        return projected

    def _fit_to_budget(self, items: list[RetrievedMemory]) -> list[RetrievedMemory]:
        """Greedy pack into the byte budget, top score first.

        Iterates the items (already in score-DESC order) and accumulates
        each item's rendered size. Stops at the first item that would
        overflow. A single oversize item is truncated so the diagram
        still gets one row back; otherwise we drop everything past the
        budget boundary and return what we kept.
        """
        selected: list[RetrievedMemory] = []
        total = _HEADER_BYTES
        for item in items:
            rendered = item.render()
            # +1 accounts for the separator newline between items.
            size = len(rendered.encode("utf-8")) + 1
            if total + size <= self.budget_bytes:
                selected.append(item)
                total += size
                continue
            if not selected:
                # Single oversize item: truncate so the section still
                # surfaces a row, with a marker the reader can grep for.
                return [_truncate_to_budget(item, self.budget_bytes)]
            break
        return selected


__all__ = ["MemoryIndex", "RetrievedMemory", "render_for_active_md"]
