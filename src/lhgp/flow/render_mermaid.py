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

# Characters that have grammar meaning inside a Mermaid shape / edge
# label. We escape them with HTML entities (``#NN;``) so a label like
# ``list[0]`` or ``#hash`` does not break the shape parser. The set is
# kept narrow on purpose: over-escaping makes diagrams unreadable.
_LABEL_ESCAPE_CHARS = frozenset('[]{}()|"<>#')


def _escape_label(label: str) -> str:
    """Neutralize Mermaid-reserved characters in a label.

    Mermaid treats ``[ ] { } ( ) | " < > #`` as shape / edge grammar.
    HTML-entity escape each occurrence so the label can contain any
    string a Python source file might produce (e.g. ``list[0]``,
    ``#hash``, ``a|b``) without breaking the diagram.
    """
    out: list[str] = []
    for ch in label:
        if ch in _LABEL_ESCAPE_CHARS:
            out.append(f"#{ord(ch)};")
        else:
            out.append(ch)
    return "".join(out)


def _safe_id(raw: str, taken: dict[str, int]) -> str:
    # Mermaid node IDs are restricted to ``[A-Za-z][A-Za-z0-9_]*`` by the
    # grammar; some renderers (older mermaid-cli, custom preprocessors)
    # reject non-ASCII. Force ASCII so the diagram renders portably;
    # the original label is preserved on the node line for legibility.
    base = "".join(c if (c.isascii() and c.isalnum()) or c == "_" else "_" for c in raw)
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
        # Escape reserved Mermaid chars in the label so a Python
        # identifier like ``list[0]`` or ``#hash`` cannot break the
        # shape grammar.
        shape_filled = shape.format(_escape_label(n.label))
        lines.append(f"  {sid}{shape_filled}")
    for e in flow.edges:
        s = safe.get(e.src)
        d = safe.get(e.dst)
        if s is None or d is None:
            continue
        if e.label:
            lines.append(f"  {s} -->|{_escape_label(e.label)}| {d}")
        else:
            lines.append(f"  {s} --> {d}")
    lines.append("```")
    return "\n".join(lines) + "\n"


__all__ = ["render_mermaid"]
