"""Find code lines that have multiple top-level statements (separated by `;`)."""

import ast

paths = [
    "src/lhgp/feedback/lessons.py",
    "src/lhgp/flow/contract_flow.py",
    "src/lhgp/flow/cli.py",
    "src/lhgp/wiki/sync.py",
    "src/lhgp/wiki/__init__.py",
    "src/lhgp/memory/store.py",
    "src/lhgp/memory/index.py",
    "src/lhgp/feedback/store.py",
]

for path in paths:
    try:
        src = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        continue
    lines = src.splitlines(keepends=True)
    # For each line, look for top-level (i.e. outside any triple-quoted string)
    # semicolons that aren't part of `;` in `if x: ;` (no-op) or `; ;` style
    # We'll use ast to find each statement's start/end and flag any line
    # that contains the start of two or more top-level statements.
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    starts_by_line: dict[int, list[int]] = {}
    for node in tree.body:
        line = node.lineno
        starts_by_line.setdefault(line, []).append(id(node))
    # Look for lines with 2+ statement starts.
    for line, ids in starts_by_line.items():
        if len(ids) >= 2:
            content = lines[line - 1].rstrip("\n").strip()
            print(f"{path}:{line}: {len(ids)} statements on this line:")
            print(f"  > {content[:200]}")
