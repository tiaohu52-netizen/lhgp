"""P6+1 / memory-and-wiki Phase 3: Mermaid flowchart renderer.

Produces a ``flowchart TD`` block from a :class:`Flow`. The block is
deliberately wrapped in a fenced code block (```mermaid ... ```) so it
can be pasted directly into a wiki page or rendered by a Mermaid-aware
viewer.

Node IDs are sanitized to Mermaid's word-char subset (``[A-Za-z0-9_]``).
Anything else is replaced with ``_``. Collisions are resolved by
appending a numeric suffix; the original (human-readable) label lives
on the node line so the diagram stays legible.
"""

from __future__ import annotations

from lhgp.flow.ast_walker import Flow, FlowNode


def _safe_id(raw: str, taken: dict[str, int]) -> str:
    base = "".join(c if c.isalnum() or c == "_" else "_" for c in raw)
    if not base:
        base = "node"
    if base[0].isdigit():
        base = "n_" + base
    n = taken.get(base, 0)
    taken[base] = n + 1
    return f"{base}_{n}" if n else base


def _shape_for(node: FlowNode) -> str:
    # Mermaid shapes: [] rectangle, () rounded, {} diamond, (()) circle.
    if node.kind == "module":
        return "[/{0}/]"
    if node.kind == "class":
        return "[({0})]"
    if node.kind == "method":
        return "[({0})]"
    if node.kind == "external":
        return '{{"{0}"}}'
    return "[{0}]"


def render_mermaid(flow: Flow) -> str:
    """Render a Flow as a Mermaid ``flowchart TD`` fenced block."""
    if not flow.nodes:
        return "```mermaid\nflowchart TD\n  empty[no nodes]\n```\n"
    taken: dict[str, int] = {}
    safe: dict[str, str] = {}
    lines: list[str] = ["```mermaid", "flowchart TD"]
    for n in flow.nodes:
        sid = _safe_id(n.id, taken)
        safe[n.id] = sid
        shape = _shape_for(n)
        # Strip the placeholders from _shape_for.
        shape_filled = shape.format(n.label)
        lines.append(f"  {sid}{shape_filled}")
    for e in flow.edges:
        s = safe.get(e.src)
        d = safe.get(e.dst)
        if s is None or d is None:
            continue
        if e.label:
            lines.append(f"  {s} -->|{e.label}| {d}")
        else:
            lines.append(f"  {s} --> {d}")
    lines.append("```")
    return "\n".join(lines) + "\n"


__all__ = ["render_mermaid"]
