"""Legacy facade: re-exports from canonical lhgp.memory namespace."""

from lhgp.memory import *  # noqa: F403
from lhgp.memory import (  # noqa: F401
    Memory,
    MemoryIndex,
    MemoryKind,
    MemoryScope,
    MemoryStoreError,
    RetrievedMemory,
    bump_score,
    expire_due,
    get_memory,
    list_memories,
    make_pattern_memory,
    record_memory,
    render_for_active_md,
    search_memories,
)
