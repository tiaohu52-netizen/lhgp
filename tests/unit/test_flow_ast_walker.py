"""P3: ast_walker tests — extract defs/classes/calls from a Python source.

The walker must:
  - register top-level functions and classes as nodes
  - register one method node per class-body function
  - resolve ``self.x()`` to the local method (not external)
  - resolve ``Foo()`` to the class node (instantiation, not external)
  - treat unresolved calls (subscripts, lambdas) as external so the
    diagram at least shows the dependency
  - drop self-loops and exact duplicate edges
"""

from __future__ import annotations

import pytest

from lhgp.flow.ast_walker import walk_source

pytestmark = pytest.mark.unit


class TestNodeExtraction:
    def test_collects_module_node(self) -> None:
        flow = walk_source("x = 1", "demo")
        assert any(n.id == "demo" and n.kind == "module" for n in flow.nodes)

    def test_collects_top_level_function(self) -> None:
        flow = walk_source("def alpha():\n    pass", "demo")
        ids = {n.id for n in flow.nodes}
        assert "demo.alpha" in ids

    def test_collects_top_level_async_function(self) -> None:
        flow = walk_source("async def alpha():\n    pass", "demo")
        ids = {n.id for n in flow.nodes}
        assert "demo.alpha" in ids

    def test_collects_class_and_methods(self) -> None:
        src = "class Foo:\n    def bar(self): pass\n    def baz(self): pass\n"
        flow = walk_source(src, "demo")
        ids = {n.id for n in flow.nodes}
        assert "demo.Foo" in ids
        assert "demo.Foo.bar" in ids
        assert "demo.Foo.baz" in ids
        kinds = {n.id: n.kind for n in flow.nodes}
        assert kinds["demo.Foo"] == "class"
        assert kinds["demo.Foo.bar"] == "method"


class TestCallResolution:
    def test_function_calls_local_function(self) -> None:
        src = "def alpha():\n    return beta()\ndef beta():\n    return 1\n"
        flow = walk_source(src, "demo")
        edges = {(e.src, e.dst) for e in flow.edges}
        assert ("demo.alpha", "demo.beta") in edges

    def test_self_dot_method_resolves_to_local_method(self) -> None:
        src = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        return self.baz()\n"
            "    def baz(self):\n"
            "        return 1\n"
        )
        flow = walk_source(src, "demo")
        edges = {(e.src, e.dst) for e in flow.edges}
        assert ("demo.Foo.bar", "demo.Foo.baz") in edges
        # No external self.baz
        assert not any("self.baz" in e.dst for e in flow.edges)

    def test_class_instantiation_resolves_to_class_node(self) -> None:
        src = "class Foo:\n    pass\ndef main():\n    Foo()\n"
        flow = walk_source(src, "demo")
        edges = {(e.src, e.dst) for e in flow.edges}
        assert ("demo.main", "demo.Foo") in edges
        # No external "Foo"
        assert not any(e.dst == "ext:Foo" for e in flow.edges)

    def test_external_call_becomes_external_node(self) -> None:
        src = "def main():\n    json.dumps({})\n"
        flow = walk_source(src, "demo")
        edges = {(e.src, e.dst) for e in flow.edges}
        assert ("demo.main", "ext:json.dumps") in edges
        # The external node is in the node set
        assert any(n.id == "ext:json.dumps" and n.kind == "external" for n in flow.nodes)

    def test_dynamic_call_is_dropped(self) -> None:
        # ``d["k"]()`` — subscript call; target is not statically resolvable.
        src = "def main():\n    d = {}\n    d['k']()\n"
        flow = walk_source(src, "demo")
        # No edges from main to a synthetic name.
        for e in flow.edges:
            assert e.src == "demo.main"
            assert not e.dst.startswith("ext:d[")
            assert e.dst in {"demo.main"}  # only self-loops would be dropped


class TestEdgeDedup:
    def test_self_loop_dropped(self) -> None:
        src = "def alpha():\n    return alpha()\n"  # recursive
        flow = walk_source(src, "demo")
        for e in flow.edges:
            assert e.src != e.dst

    def test_duplicate_edges_collapsed(self) -> None:
        src = "def alpha():\n    beta()\n    beta()\n    beta()\ndef beta():\n    pass\n"
        flow = walk_source(src, "demo")
        # Only one edge alpha->beta.
        ab = [e for e in flow.edges if e.src == "demo.alpha" and e.dst == "demo.beta"]
        assert len(ab) == 1


class TestModuleScope:
    def test_top_level_call_attributed_to_module(self) -> None:
        # Calls at module scope (not inside a function) should be
        # attributed to the module node, not lost.
        src = "if __name__ == '__main__':\n    main()\ndef main(): pass\n"
        flow = walk_source(src, "demo")
        edges = {(e.src, e.dst) for e in flow.edges}
        assert ("demo", "demo.main") in edges
