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
from dataclasses import dataclass, field


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


# ---------------------------------------------------------------------------
# Internal walker state
# ---------------------------------------------------------------------------


@dataclass
class _WalkContext:
    """Mutable state shared by the walk helpers.

    A small class beats threading seven parameters through every helper,
    and beats using closures (which can't be picked apart for tests).
    """

    module_id: str
    nodes: dict[str, FlowNode] = field(default_factory=dict)
    edges: list[FlowEdge] = field(default_factory=list)
    top_level_fns: dict[str, str] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)  # qualified name -> node id
    methods: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Module node is always present, so the id exists for both the
        # "bare expression at module scope" caller and the "this file
        # exists" presence check downstream.
        self.nodes[self.module_id] = FlowNode(
            id=self.module_id, label=self.module_id, kind="module"
        )

    def add_node(self, node: FlowNode) -> None:
        if node.id not in self.nodes:
            self.nodes[node.id] = node

    def add_edge(self, src: str, dst: str, label: str | None = None) -> None:
        self.edges.append(FlowEdge(src=src, dst=dst, label=label))


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 1: register definitions (no edges yet)
# ---------------------------------------------------------------------------


def _register_definitions(stmts: list[ast.stmt], ctx: _WalkContext) -> None:
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _register_function(stmt, ctx)
        elif isinstance(stmt, ast.ClassDef):
            _register_class(stmt, qualified=stmt.name, ctx=ctx)


def _register_function(fn: ast.FunctionDef | ast.AsyncFunctionDef, ctx: _WalkContext) -> None:
    fn_id = f"{ctx.module_id}.{fn.name}"
    ctx.top_level_fns[fn.name] = fn_id
    ctx.add_node(FlowNode(id=fn_id, label=fn.name, kind="function"))


def _register_class(cls: ast.ClassDef, *, qualified: str, ctx: _WalkContext) -> None:
    class_id = f"{ctx.module_id}.{qualified}"
    ctx.classes[qualified] = class_id
    ctx.add_node(FlowNode(id=class_id, label=qualified, kind="class"))
    method_names: set[str] = set()
    for child in cls.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_id = f"{class_id}.{child.name}"
            method_names.add(child.name)
            ctx.add_node(FlowNode(id=method_id, label=f"{qualified}.{child.name}", kind="method"))
        elif isinstance(child, ast.ClassDef):
            _register_class(child, qualified=f"{qualified}.{child.name}", ctx=ctx)
    ctx.methods[qualified] = method_names


# ---------------------------------------------------------------------------
# Phase 2: resolve a call target string to a local node id (or None = external)
# ---------------------------------------------------------------------------


def _resolve_local(target: str, *, current_class: str | None, ctx: _WalkContext) -> str | None:
    """Map a call target string to a local node id, or None if external.

    Three resolution paths, in order:
      1. ``self.x`` / ``cls.x`` → method on the enclosing class, or a
         method on a nested class accessed via ``self.Outer.x``.
      2. Bare ``x`` → top-level function, or class instantiation.
      3. Dotted ``X.y`` → class method (full path, or one level into
         a nested class).
    """
    head = target.split(".", 1)[0]

    # Path 1: self.x / cls.x.
    if head in ("self", "cls") and current_class is not None and "." in target:
        callee = target.split(".", 1)[1]
        same_class_id = f"{ctx.module_id}.{current_class}.{callee}"
        if same_class_id in ctx.nodes:
            return same_class_id
        # self.<ClassName>.<rest> — one level of nested-class access.
        nested_tail = callee.split(".", 1)
        if len(nested_tail) == 2 and nested_tail[0] in ctx.classes:
            nested_method_id = f"{ctx.classes[nested_tail[0]]}.{nested_tail[1]}"
            if nested_method_id in ctx.nodes:
                return nested_method_id
        return None

    # Path 2: bare name.
    if "." not in target:
        if head in ctx.top_level_fns:
            return ctx.top_level_fns[head]
        if head in ctx.classes:
            # ``Foo()`` is class instantiation — link to the class node.
            # The ``__init__`` call is implicit and would only add noise.
            return ctx.classes[head]
        return None

    # Path 3: dotted Class.method (or Outer.Inner.method).
    if head in ctx.classes:
        method_id = f"{ctx.classes[head]}.{target.split('.', 1)[1]}"
        if method_id in ctx.nodes:
            return method_id
    return None


def _record_call(
    target: str, *, current_id: str, current_class: str | None, ctx: _WalkContext
) -> None:
    """Add the call edge (local or external) for a resolved target."""
    local = _resolve_local(target, current_class=current_class, ctx=ctx)
    if local is not None:
        ctx.add_edge(current_id, local)
        return
    # External dependency — still record so the diagram shows the import.
    ctx.add_node(FlowNode(id=f"ext:{target}", label=target, kind="external"))
    ctx.add_edge(current_id, f"ext:{target}")


# ---------------------------------------------------------------------------
# Phase 3: walk call sites, attaching edges to definitions
# ---------------------------------------------------------------------------


class _CallVisitor(ast.NodeVisitor):
    """One visitor per (current_id, current_class) scope.

    Each ``visit_Call`` resolves the call target against the shared
    context and either links it to a local node or to an ``ext:<...>``
    node. The visitor does not own any state besides the scope tag.
    """

    __slots__ = ("ctx", "current_class", "current_id")

    def __init__(self, *, current_id: str, current_class: str | None, ctx: _WalkContext) -> None:
        self.current_id = current_id
        self.current_class = current_class
        self.ctx = ctx

    def visit_Call(self, node: ast.Call) -> None:
        target = _qualified(node.func)
        if target is not None:
            _record_call(
                target,
                current_id=self.current_id,
                current_class=self.current_class,
                ctx=self.ctx,
            )
        self.generic_visit(node)


def _walk_decorators(
    decos: list[ast.expr], *, current_id: str, current_class: str | None, ctx: _WalkContext
) -> None:
    """Visit a function/class decorator list, emitting edges to each."""
    for deco in decos:
        if isinstance(deco, ast.Call):
            # The decorator-call itself is a call site attributed to the
            # decorated definition; the visitor handles generic_visit.
            _CallVisitor(current_id=current_id, current_class=current_class, ctx=ctx).visit(deco)
            continue
        target = _qualified(deco)
        if target is None:
            continue
        _record_call(target, current_id=current_id, current_class=current_class, ctx=ctx)


def _walk_function_body(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    current_id: str,
    current_class: str | None,
    ctx: _WalkContext,
) -> None:
    visitor = _CallVisitor(current_id=current_id, current_class=current_class, ctx=ctx)
    _walk_decorators(fn.decorator_list, current_id=current_id, current_class=current_class, ctx=ctx)
    for stmt in fn.body:
        visitor.visit(stmt)


def _walk_class_body(cls: ast.ClassDef, *, qualified: str, ctx: _WalkContext) -> None:
    """Walk a class body, recursing into nested classes.

    Class decorators are attributed to the class's qualified id (a class
    def is a "statement that produces a value", so the decorator is the
    call site).
    """
    class_id = f"{ctx.module_id}.{qualified}"
    _walk_decorators(cls.decorator_list, current_id=class_id, current_class=qualified, ctx=ctx)
    for child in cls.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_id = f"{class_id}.{child.name}"
            _walk_function_body(child, current_id=method_id, current_class=qualified, ctx=ctx)
        elif isinstance(child, ast.ClassDef):
            _walk_class_body(child, qualified=f"{qualified}.{child.name}", ctx=ctx)


def _walk_top_level(stmts: list[ast.stmt], *, ctx: _WalkContext) -> None:
    """Walk each top-level statement and emit edges for the calls inside.

    Function/class definitions get their own node; bare expressions are
    attributed to the module node.
    """
    for stmt in stmts:
        if isinstance(stmt, ast.ClassDef):
            _walk_class_body(stmt, qualified=stmt.name, ctx=ctx)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_id = f"{ctx.module_id}.{stmt.name}"
            _walk_function_body(stmt, current_id=fn_id, current_class=None, ctx=ctx)
        else:
            _CallVisitor(current_id=ctx.module_id, current_class=None, ctx=ctx).visit(stmt)


# ---------------------------------------------------------------------------
# Phase 4: edge dedup
# ---------------------------------------------------------------------------


def _dedup_edges(edges: list[FlowEdge]) -> list[FlowEdge]:
    """Drop self-loops (recursive functions) and exact duplicates.

    Self-loops would clutter the diagram without adding information;
    dedup keeps repeated callers (e.g. two calls in the same body) from
    producing parallel edges.
    """
    seen: set[tuple[str, str, str | None]] = set()
    out: list[FlowEdge] = []
    for edge in edges:
        if edge.src == edge.dst:
            continue
        key = (edge.src, edge.dst, edge.label)
        if key in seen:
            continue
        seen.add(key)
        out.append(edge)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


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
    ctx = _WalkContext(module_id=module_name)
    _register_definitions(tree.body, ctx)
    _walk_top_level(tree.body, ctx=ctx)
    return Flow(
        title=module_name,
        nodes=tuple(ctx.nodes.values()),
        edges=tuple(_dedup_edges(ctx.edges)),
        source=f"ast:{module_name}",
    )


__all__ = ["Flow", "FlowEdge", "FlowNode", "walk_source"]
