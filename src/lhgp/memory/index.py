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
        now: datetime | None = None,
    ) -> list[RetrievedMemory]:
        """Return the top-N memories that fit the budget.

        Always includes ``global`` scope. For ``domain`` scope, matches
        the contract's ``context.domain``. For ``project``, the top-N by
        score. If the rendered text would overflow the budget, drops
        lowest-score items until it fits. Returns [] on empty/no-match.
        """
        domain = _domain_of(contract_context)
        # 1. Global: always include
        globals_ = list_memories(self.conn, scope=MemoryScope.GLOBAL, limit=self.top_n * 2)
        # 2. Domain: contract's domain
        domains: list[Memory] = []
        if domain:
            domains = list_memories(self.conn, scope=MemoryScope.DOMAIN, limit=self.top_n * 2)
            # Filter by domain tag (we store it as a tag, not a column)
            domains = [m for m in domains if domain in m.tags]
        # 3. Project: top by score
        projects = list_memories(self.conn, scope=MemoryScope.PROJECT, limit=self.top_n * 2)

        # Combine and de-dup by id, then sort by score desc
        seen: set[int] = set()
        combined: list[Memory] = []
        for m in globals_ + domains + projects:
            if m.id is None or m.id in seen:
                # None id would corrupt the seen set; skip rather than crash.
                continue
            seen.add(m.id)
            combined.append(m)
        combined.sort(key=lambda m: (-m.score, m.created_at or datetime.min), reverse=False)
        combined.reverse()  # score DESC

        # Project + render-fit; lowest-score first to drop
        projected = [_project(m) for m in combined[: self.top_n * 3]]
        projected.sort(key=lambda r: -r.score)
        selected: list[RetrievedMemory] = []
        for r in projected:
            tentative = [*selected, r]
            text = _render_section(tentative)
            if len(text.encode("utf-8")) <= self.budget_bytes:
                selected = tentative
            elif not selected:
                # Single item exceeds budget. Truncate body and accept.
                truncated = RetrievedMemory(
                    title=r.title,
                    body_md=r.body_md[: max(0, self.budget_bytes - 200)] + "\n[…truncated…]",
                    kind=r.kind,
                    score=r.score,
                    tags=r.tags,
                    source=r.source,
                )
                selected = [truncated]
                break
        return selected


def _render_section(memories: list[RetrievedMemory]) -> str:
    if not memories:
        return ""
    lines = ["## 长期记忆（跨合同沉淀）", ""]
    for r in memories:
        lines.append(r.render())
        lines.append("")
    return "\n".join(lines)


def render_for_active_md(memories: list[RetrievedMemory]) -> str:
    """Public helper for ``compile_context_snapshot`` to render a section."""
    return _render_section(memories)


__all__ = ["MemoryIndex", "RetrievedMemory", "render_for_active_md"]
