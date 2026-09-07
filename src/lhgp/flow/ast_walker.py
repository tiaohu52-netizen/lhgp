"""P6+1 / memory-and-wiki Phase 3: AST-based call-graph extraction.

Walks a single Python source file and returns a :class:`Flow` describing
the top-level functions, classes, methods, and the call edges between
them. Import targets become ``external`` nodes so a reader can see at a
glance which deps the module pulls in.

Scope:
  - One file at a time. Cross-file graphs are the caller's job (they
    would just be the union of per-file flows anyway, and the
    bookkeeping for partial imports is not worth it for v1).
  - The walker is structural: it does not run the code. ``if TYPE_CHECKING``
    imports become plain ``external`` nodes, which is the right behavior
    for a static diagram.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FlowNode:
    id: str
    label: str
    kind: str  # "module" | "function" | "class" | "method" | "external"


@dataclass(frozen=True, slots=True)
class FlowEdge:
    src: str
    dst: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class Flow:
    title: str
    nodes: tuple[FlowNode, ...]
    edges: tuple[FlowEdge, ...]
    source: str  # "ast:<module>" or "wiki:<page>"

    def node(self, node_id: str) -> FlowNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None


def _qualified(name: ast.expr) -> str | None:
    """Render a dotted ``ast.Name`` / ``ast.Attribute`` chain to a string.

    Returns ``None`` for calls whose target we cannot statically resolve
    (subscripts, calls-of-calls, etc.) — those are intentionally dropped
    to keep the diagram honest.
    """
    if isinstance(name, ast.Name):
        return name.id
    if isinstance(name, ast.Attribute):
        parent = _qualified(name.value)
        if parent is None:
            return None
        return f"{parent}.{name.attr}"
    return None


def walk_source(source: str, module_name: str) -> Flow:
    """Build a Flow from a single Python source string.

    The returned Flow has:
      - one ``module`` node per file
      - one node per top-level ``def`` / ``async def``
      - one ``class`` node per top-level ``class`` plus one ``method`` node
        per function defined inside its body
      - one ``external`` node per unique call target not defined locally
      - edges: caller -> callee (caller is the enclosing function/method,
        or the module node itself for top-level code)
    """
    tree = ast.parse(source)
    nodes: dict[str, FlowNode] = {}
    edges: list[FlowEdge] = []

    def add(node: FlowNode) -> None:
        if node.id not in nodes:
            nodes[node.id] = node

    module_id = module_name
    add(FlowNode(id=module_id, label=module_name, kind="module"))

    # Pre-compute the set of locally-defined top-level functions and
    # classes. Methods live under ``class.method`` and are not in this
    # set; they are still resolvable via class-scope context below.
    top_level_fns: dict[str, str] = {}  # name -> node id
    classes: dict[str, str] = {}  # name -> class node id
    methods: dict[str, set[str]] = {}  # class name -> set of method names

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_level_fns[stmt.name] = f"{module_name}.{stmt.name}"
            add(FlowNode(id=top_level_fns[stmt.name], label=stmt.name, kind="function"))
        elif isinstance(stmt, ast.ClassDef):
            class_id = f"{module_name}.{stmt.name}"
            classes[stmt.name] = class_id
            add(FlowNode(id=class_id, label=stmt.name, kind="class"))
            method_names: set[str] = set()
            for child in stmt.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_id = f"{class_id}.{child.name}"
                    method_names.add(child.name)
                    add(FlowNode(id=method_id, label=f"{stmt.name}.{child.name}", kind="method"))
            methods[stmt.name] = method_names

    def _resolve(target: str, current_class: str | None) -> str | None:
        """Map a call target string to a node id, or None if external-ish."""
        head = target.split(".", 1)[0]
        if head in ("self", "cls") and current_class is not None and "." in target:
            callee = target.split(".", 1)[1]
            method_id = f"{module_name}.{current_class}.{callee}"
            if method_id in nodes:
                return method_id
            return None
        if "." not in target:
            if head in top_level_fns:
                return top_level_fns[head]
            if head in classes:
                # ``Foo()`` is class instantiation — link to the class
                # node, which is the most useful static view. The
                # ``__init__`` call is implicit and would only add noise.
                return classes[head]
            return None
        # Dotted: ``Class.method`` if it's our class; otherwise external.
        if head in classes:
            method_id = f"{classes[head]}.{target.split('.', 1)[1]}"
            if method_id in nodes:
                return method_id
        return None

    def _enclosing_class(node: ast.AST) -> str | None:
        """Walk up parents to find the immediate ClassDef. O(n) per call —
        fine for v1; we cache at the visitor boundary."""
        return _class_context.get(id(node))

    class _CallVisitor(ast.NodeVisitor):
        def __init__(self, current_id: str, current_class: str | None) -> None:
            self.current_id = current_id
            self.current_class = current_class

        def visit_Call(self, node: ast.Call) -> None:
            target = _qualified(node.func)
            if target is None:
                # Dynamic call (subscript, lambda, etc.) — drop silently.
                self.generic_visit(node)
                return
            local = _resolve(target, self.current_class)
            if local is not None:
                edges.append(FlowEdge(src=self.current_id, dst=local))
                self.generic_visit(node)
                return
            # External. Self/cls that don't resolve: still external — the
            # diagram should at least show the dependency.
            ext_id = f"ext:{target}"
            add(FlowNode(id=ext_id, label=target, kind="external"))
            edges.append(FlowEdge(src=self.current_id, dst=ext_id))
            self.generic_visit(node)

    # Build a fast id->enclosing-class map. We only need it for the call
    # resolution; walking parents per-call would be O(n^2) on deep trees.
    _class_context: dict[int, str] = {}

    def _index(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                _class_context[id(child)] = current or ""  # outer
                for grandchild in child.body:
                    _class_context[id(grandchild)] = child.name
                    _index(grandchild, child.name)
            else:
                _class_context[id(child)] = current or ""
                _index(child, current)

    _index(tree, None)

    # Walk top-level statements. The module node is the "current_id" for
    # bare expressions; function/method bodies get their own node.
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            for child in stmt.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mid = f"{module_name}.{stmt.name}.{child.name}"
                    _CallVisitor(current_id=mid, current_class=stmt.name).visit(child)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fid = f"{module_name}.{stmt.name}"
            _CallVisitor(current_id=fid, current_class=None).visit(stmt)
        else:
            # Module-level statements (calls, expressions). Attribute them
            # to the module node.
            _CallVisitor(current_id=module_id, current_class=None).visit(stmt)

    # Drop self-loop edges and exact duplicates. Self-loops (a -> a) mean
    # a recursive function — those are dropped because the diagram is
    # cleaner without them; the relationship is implied by the function.
    seen: set[tuple[str, str, str | None]] = set()
    deduped: list[FlowEdge] = []
    for e in edges:
        if e.src == e.dst:
            continue
        key = (e.src, e.dst, e.label)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    return Flow(
        title=module_name,
        nodes=tuple(nodes[nid] for nid in nodes),
        edges=tuple(deduped),
        source=f"ast:{module_name}",
    )


__all__ = ["Flow", "FlowEdge", "FlowNode", "walk_source"]
