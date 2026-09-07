"""P6+1 / memory-and-wiki Phase 3: Excalidraw JSON renderer.

Excalidraw is a hand-drawn-style whiteboard. The renderer emits a JSON
``elements`` array with a rectangle per node and an arrow per edge. The
output is the "scene" shape (no top-level envelope) so the caller can
paste it into ``.excalidraw`` files or pipe it to a viewer.

Layout is a deterministic top-down grid: nodes are placed left-to-right
in a 4-wide grid, with arrows drawn straight. This is enough for a
readable diagram and avoids pulling in a layout engine.
"""

from __future__ import annotations

from lhgp.flow.ast_walker import Flow, FlowNode

COLS = 4
COL_GAP = 240
ROW_GAP = 140
W = 200
H = 80
ROUND_RADIUS = 8


def _kind_style(kind: str) -> tuple[str, str, int]:
    """Return (stroke, fill, font_size) for a node kind."""
    if kind == "module":
        return ("#1e1e1e", "#fef3c7", 20)
    if kind == "class":
        return ("#1e1e1e", "#dbeafe", 20)
    if kind == "method":
        return ("#1e1e1e", "#e0e7ff", 18)
    if kind == "external":
        return ("#1e1e1e", "#f3f4f6", 16)
    return ("#1e1e1e", "#ffffff", 18)


def _node_position(idx: int) -> tuple[float, float]:
    col = idx % COLS
    row = idx // COLS
    return float(col * (W + COL_GAP)), float(row * (H + ROW_GAP))


def _make_rect(node: FlowNode, idx: int) -> dict[str, object]:
    x, y = _node_position(idx)
    stroke, fill, _font = _kind_style(node.kind)
    eid = f"node_{idx}"
    return {
        "id": eid,
        "type": "rectangle",
        "x": x,
        "y": y,
        "width": W,
        "height": H,
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": fill,
        "fillStyle": "solid",
        "roundness": {"type": 3, "value": ROUND_RADIUS},
        "boundElements": [{"id": f"text_{idx}", "type": "text"}],
        "groupIds": [],
        "seed": idx + 1,
        "version": 1,
        "versionNonce": idx + 1,
        "isDeleted": False,
        "link": None,
        "locked": False,
        "updated": 1,
    }


def _make_text(node: FlowNode, idx: int) -> dict[str, object]:
    x, y = _node_position(idx)
    stroke, _, font = _kind_style(node.kind)
    return {
        "id": f"text_{idx}",
        "type": "text",
        "x": x,
        "y": y + (H - font) / 2,
        "width": W,
        "height": float(font + 4),
        "angle": 0,
        "strokeColor": stroke,
        "fillStyle": "solid",
        "boundElements": None,
        "groupIds": [],
        "seed": idx + 1000,
        "version": 1,
        "versionNonce": idx + 1000,
        "isDeleted": False,
        "link": None,
        "locked": False,
        "updated": 1,
        "text": {"fontSize": font, "fontFamily": 1, "text": node.label, "textAlign": "center"},
    }


def _make_arrow(src_idx: int, dst_idx: int, eidx: int) -> dict[str, object]:
    sx, sy = _node_position(src_idx)
    dx, dy = _node_position(dst_idx)
    return {
        "id": f"arrow_{eidx}",
        "type": "arrow",
        "x": sx + W,
        "y": sy + H / 2,
        "width": max(0.0, dx - (sx + W)),
        "height": dy + H / 2 - (sy + H / 2),
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "fillStyle": "solid",
        "boundElements": None,
        "groupIds": [],
        "seed": eidx + 5000,
        "version": 1,
        "versionNonce": eidx + 5000,
        "isDeleted": False,
        "link": None,
        "locked": False,
        "updated": 1,
        "points": [[0, 0], [max(0.0, dx - (sx + W)), dy + H / 2 - (sy + H / 2)]],
        "lastCommittedPoint": None,
        "startBinding": {"elementId": f"node_{src_idx}", "focus": 0, "gap": 1},
        "endBinding": {"elementId": f"node_{dst_idx}", "focus": 0, "gap": 1},
        "startArrowhead": None,
        "endArrowhead": "arrow",
    }


def render_excalidraw(flow: Flow) -> dict[str, object]:
    """Render a Flow as a dict of ``{"type": "excalidraw", "elements": [...]}``.

    The envelope makes it clear which schema version emitted the scene.
    """
    if not flow.nodes:
        return {"type": "excalidraw", "version": 1, "elements": []}
    index_by_id = {n.id: i for i, n in enumerate(flow.nodes)}
    elements: list[dict[str, object]] = []
    for i, n in enumerate(flow.nodes):
        elements.append(_make_rect(n, i))
        elements.append(_make_text(n, i))
    for j, e in enumerate(flow.edges):
        si = index_by_id.get(e.src)
        di = index_by_id.get(e.dst)
        if si is None or di is None:
            continue
        elements.append(_make_arrow(si, di, j))
    return {
        "type": "excalidraw",
        "version": 1,
        "source": flow.source,
        "elements": elements,
    }


__all__ = ["render_excalidraw"]
