"""P3: Mermaid and Excalidraw renderers.

The renderers consume a Flow and produce a string / dict. They are
deterministic (no random IDs, no clock) so tests can compare exactly.
"""

from __future__ import annotations

import pytest

from lhgp.flow.ast_walker import Flow, FlowEdge, FlowNode, walk_source
from lhgp.flow.render_excalidraw import render_excalidraw
from lhgp.flow.render_mermaid import render_mermaid

pytestmark = pytest.mark.unit


def _trivial_flow() -> Flow:
    return Flow(
        title="demo",
        nodes=(
            FlowNode(id="demo", label="demo", kind="module"),
            FlowNode(id="demo.alpha", label="alpha", kind="function"),
            FlowNode(id="demo.beta", label="beta", kind="function"),
            FlowNode(id="ext:json.dumps", label="json.dumps", kind="external"),
        ),
        edges=(
            FlowEdge(src="demo", dst="demo.alpha"),
            FlowEdge(src="demo.alpha", dst="demo.beta"),
            FlowEdge(src="demo.alpha", dst="ext:json.dumps"),
        ),
        source="ast:demo",
    )


class TestMermaid:
    def test_starts_with_fenced_block(self) -> None:
        out = render_mermaid(_trivial_flow())
        assert out.startswith("```mermaid\n")
        assert out.rstrip().endswith("```")

    def test_contains_flowchart_header(self) -> None:
        out = render_mermaid(_trivial_flow())
        assert "flowchart TD" in out

    def test_contains_all_nodes(self) -> None:
        out = render_mermaid(_trivial_flow())
        # Each label appears at least once.
        for label in ("demo", "alpha", "beta", "json.dumps"):
            assert label in out

    def test_contains_all_edges(self) -> None:
        out = render_mermaid(_trivial_flow())
        # Edges are emitted as `A --> B` between safe ids. We don't pin
        # the safe ids (the renderer chooses), so we just check the
        # count of arrows.
        assert out.count("-->") == 3

    def test_safe_id_collisions_handled(self) -> None:
        # Two nodes with the same label but different ids must not
        # collide into a single Mermaid node.
        flow = Flow(
            title="t",
            nodes=(
                FlowNode(id="a.b", label="b", kind="function"),
                FlowNode(id="a-b", label="b", kind="function"),
            ),
            edges=(),
            source="ast:t",
        )
        out = render_mermaid(flow)
        # Both labels appear; safe ids differ.
        assert out.count("[b]") == 2
        assert "b_1[" in out or "b_0[" in out  # collision-suffixed

    def test_empty_flow(self) -> None:
        out = render_mermaid(Flow(title="t", nodes=(), edges=(), source="ast:t"))
        assert "no nodes" in out

    def test_ast_source_roundtrip(self) -> None:
        # An AST-derived flow renders without errors and includes
        # function and class nodes.
        src = (
            "def alpha():\n"
            "    return beta()\n"
            "def beta():\n"
            "    return 1\n"
            "class Foo:\n"
            "    def bar(self):\n"
            "        return 1\n"
        )
        flow = walk_source(src, "demo")
        out = render_mermaid(flow)
        assert "alpha" in out
        assert "beta" in out
        assert "Foo" in out
        assert "Foo.bar" in out

    def test_non_ascii_label_yields_ascii_node_id(self) -> None:
        # Mermaid IDs are ASCII-only in the grammar. The label keeps the
        # original Unicode; the node id must be sanitized.
        flow = Flow(
            title="t",
            nodes=(FlowNode(id="mod.héllo", label="héllo", kind="function"),),
            edges=(),
            source="ast:t",
        )
        out = render_mermaid(flow)
        # Node id is sanitized: every non-ASCII char becomes ``_``.
        assert "mod_h" in out
        # The original accent must not leak into the id.
        assert "héllo[" not in out
        # Label is preserved verbatim.
        assert "héllo" in out


class TestExcalidraw:
    def test_envelope_shape(self) -> None:
        scene = render_excalidraw(_trivial_flow())
        assert scene["type"] == "excalidraw"
        assert scene["version"] == 1
        assert scene["source"] == "ast:demo"
        assert isinstance(scene["elements"], list)

    def test_pair_rect_text_per_node(self) -> None:
        scene = render_excalidraw(_trivial_flow())
        rects = [e for e in scene["elements"] if e["type"] == "rectangle"]
        texts = [e for e in scene["elements"] if e["type"] == "text"]
        assert len(rects) == 4
        assert len(texts) == 4

    def test_arrow_per_edge(self) -> None:
        scene = render_excalidraw(_trivial_flow())
        arrows = [e for e in scene["elements"] if e["type"] == "arrow"]
        assert len(arrows) == 3

    def test_node_ids_are_stable(self) -> None:
        a = render_excalidraw(_trivial_flow())
        b = render_excalidraw(_trivial_flow())
        assert a["elements"][0]["id"] == b["elements"][0]["id"]

    def test_empty_flow(self) -> None:
        scene = render_excalidraw(Flow(title="t", nodes=(), edges=(), source="ast:t"))
        assert scene["elements"] == []

    def test_back_edge_has_nonnegative_bbox(self) -> None:
        # An edge that points back to an earlier node produces a leftward
        # arrow. The points array must carry the signed delta; width/
        # height are the bounding box (non-negative) so JSON consumers
        # don't see width=0 / height<0 for back-edges.
        nodes = tuple(FlowNode(id=f"n{i}", label=str(i), kind="function") for i in range(4))
        edges = (FlowEdge(src="n0", dst="n3"),)  # left-to-right
        back = (FlowEdge(src="n3", dst="n0"),)  # back-edge
        flow = Flow(title="t", nodes=nodes, edges=edges + back, source="ast:t")
        scene = render_excalidraw(flow)
        arrows = [e for e in scene["elements"] if e["type"] == "arrow"]
        for a in arrows:
            assert a["width"] >= 0
            assert a["height"] >= 0
