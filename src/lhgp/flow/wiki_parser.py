"""P6+1 / memory-and-wiki Phase 3: wiki ``## flow`` section reader.

The wiki supports a hand-maintained flow section in any page. The
syntax is just a Markdown subsection with a fenced ``mermaid`` block:

    ## flow

    ```mermaid
    flowchart TD
      A[Start] --> B{Decision}
      B -->|yes| C[Do thing]
      B -->|no| D[Stop]
    ```

This module extracts that block as a raw Mermaid string. The output is
deliberately a string, not a :class:`Flow`, because hand-written
diagrams can use Mermaid features the AST walker never produces (e.g.
subgraphs, custom icons). Rendering back to a Mermaid block is a
no-op; rendering to Excalidraw would require a full Mermaid parser,
which is out of scope for v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FLOW_HEADING = re.compile(r"^##\s+flow\s*$", re.MULTILINE)
_MERMAID_FENCE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True, slots=True)
class FlowSection:
    page: str  # path relative to docs/wiki/
    mermaid: str
    line: int  # 1-based line where the ``## flow`` heading appears


def extract_flow_section(page_text: str, *, page: str = "<inline>") -> FlowSection | None:
    """Return the first ``## flow`` section of a wiki page, or None."""
    match = _FLOW_HEADING.search(page_text)
    if not match:
        return None
    line = page_text.count("\n", 0, match.start()) + 1
    after = page_text[match.end() :]
    fence = _MERMAID_FENCE.search(after)
    if not fence:
        return None
    return FlowSection(page=page, mermaid=fence.group(1).rstrip() + "\n", line=line)


def list_flow_pages(wiki_root: Path) -> list[str]:
    """List all wiki pages that contain a ``## flow`` section.

    Returns the page paths (relative to ``wiki_root``) as POSIX strings.
    """
    if not wiki_root.exists():
        return []
    found: list[str] = []
    for path in sorted(wiki_root.rglob("*.md")):
        # Skip hidden directories (.index.json, dotfiles).
        rel = path.relative_to(wiki_root).as_posix()
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if extract_flow_section(text, page=rel) is not None:
            found.append(rel)
    return found


__all__ = ["FlowSection", "extract_flow_section", "list_flow_pages"]
