"""P6+1 / memory-and-wiki Phase 2: protocol memory subsystem.

Re-exports for the canonical public surface. Long-term knowledge that
survives contract archival and template pruning — distinct from per-contract
context and per-attempt handover.

Architecture:
  - ``Memory`` / ``MemoryKind`` / ``MemoryScope`` in :mod:`lhgp.memory.types`
  - SQLite CRUD in :mod:`lhgp.memory.store`
  - Retrieval in :mod:`lhgp.memory.index` (the only consumer-facing call is
    :func:`MemoryIndex.retrieve`, used by the contract compiler)
  - Auto-write hooks live in the callers (not in this module) — see
    ``longtask/persistence/evaluations.py`` for the user_evaluation hook
"""

from lhgp.memory.index import MemoryIndex, RetrievedMemory, render_for_active_md
from lhgp.memory.store import (
    MemoryStoreError,
    bump_score,
    expire_due,
    get_memory,
    list_memories,
    make_memory,
    make_pattern_memory,  # back-compat alias of make_memory
    record_memory,
    search_memories,
)
from lhgp.memory.types import Memory, MemoryKind, MemoryScope

__all__ = [
    "Memory",
    "MemoryIndex",
    "MemoryKind",
    "MemoryScope",
    "MemoryStoreError",
    "RetrievedMemory",
    "bump_score",
    "expire_due",
    "get_memory",
    "list_memories",
    "make_memory",
    "make_pattern_memory",  # back-compat alias of make_memory
    "record_memory",
    "render_for_active_md",
    "search_memories",
]
