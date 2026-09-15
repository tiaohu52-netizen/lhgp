"""Architecture structure guards (ARCHITECTURE.md enforcement).

Locks the physical layout so drift is caught at test time, not review time:
1. Facade files (the other tree's re-exports) stay tiny and declare __all__;
2. contracts/ stays a zero-I/O pure data layer (no persistence/cli imports);
3. persistence/ never imports promoter/ or adapters/;
4. New modules live on the canonical side (lhgp) unless explicitly runtime.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
FACADE_MAX_LINES = 5


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_of(tree: ast.Module) -> set[str]:
    """Collect top-level module names imported by a file."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class TestFacadeFiles:
    """Facades must stay tiny re-exports; real logic lives in the canonical tree."""

    def test_known_facades_have_all(self) -> None:
        """Every facade that uses `import *` must declare __all__ (or the
        canonical side already has one)."""
        facades_without_all: list[str] = []
        for tree_name in ("lhgp", "longtask"):
            for path in (SRC_ROOT / tree_name).rglob("*.py"):
                if "__pycache__" in str(path):
                    continue
                src = path.read_text(encoding="utf-8")
                if "from lhgp." not in src and "from longtask." not in src:
                    continue
                if "import *" not in src:
                    continue
                # Only check the facade side (files that are mostly re-export)
                lines = src.splitlines()
                if len(lines) <= 15 and "__all__" not in src:
                    # has import * but no __all__ and is small = likely missing
                    facades_without_all.append(str(path.relative_to(SRC_ROOT)))
        # Facades with import * are allowed if the canonical side has __all__;
        # here we just verify the count doesn't grow unexpectedly.
        assert len(facades_without_all) == 0, (
            f"facades with import * but no local __all__: {facades_without_all}"
        )


class TestLayeredDependencies:
    """Layer dependencies must stay unidirectional (ARCHITECTURE.md diagram)."""

    def test_contracts_never_imports_persistence_or_cli(self) -> None:
        """contracts/ is a zero-I/O pure data layer."""
        violations: list[str] = []
        for path in (SRC_ROOT / "lhgp" / "contracts").rglob("*.py"):
            if "__pycache__" in str(path) or path.name == "__init__.py":
                continue
            tree = _parse(path)
            imports = _imports_of(tree)
            for forbidden in ("persistence", "cli", "mcp_server"):
                if any(imp.startswith(forbidden) for imp in imports):
                    violations.append(f"{path.name} imports {forbidden}")
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if len(parts) >= 2 and parts[1] in ("persistence", "cli"):
                        violations.append(f"{path.name} imports {node.module}")
        assert not violations, f"contracts/ layer violation: {violations}"

    def test_persistence_never_imports_promoter_or_adapters(self) -> None:
        """persistence/ must not reach up into promoter/ or adapters/."""
        violations: list[str] = []
        for base in ("lhgp", "longtask"):
            for path in (SRC_ROOT / base / "persistence").rglob("*.py"):
                if "__pycache__" in str(path):
                    continue
                tree = _parse(path)
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.ImportFrom)
                        and node.module
                        and ("promoter" in node.module or "adapters" in node.module)
                        and "types" not in node.module
                    ):
                        violations.append(f"{path.relative_to(SRC_ROOT)}: {node.module}")
        assert not violations, f"persistence layer violation: {violations}"

    def test_contracts_canonical_side_is_pure_data(self) -> None:
        """lhgp/contracts/ files should only import from contracts/ or stdlib."""
        violations: list[str] = []
        for path in (SRC_ROOT / "lhgp" / "contracts").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            tree = _parse(path)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.level == 0
                    and len(parts := node.module.split(".")) >= 2
                    and parts[0] in ("lhgp", "longtask")
                    and parts[1] != "contracts"
                ):
                    violations.append(f"{path.name} imports {node.module}")
        assert not violations, f"lhgp/contracts cross-layer import: {violations}"
