"""Project-wide pytest configuration.

Enforces the "real entry" rule (see
``docs/wiki/playbook/regression-from-external-review.md``,
pattern 7): a test that imports ``RequestEnvelope`` or
``route`` from the RPC layer is almost certainly testing
through the production entry point, not a mock.  Such tests
must carry the ``real_entry`` marker so the CI log makes the
distinction visible.

Without this marker, a future maintainer looking at a
``tests/unit/`` file that does
``from longtask.rpc.server import RequestEnvelope`` cannot
tell at a glance whether the test exercises the real handler
or a hand-rolled wrapper.  The 3rd- and 4th-round reviews
both found real bugs in tests that *appeared* to cover the
handler but actually called a wrapper that had its own
allowlist bypass.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Imports that indicate "this test is exercising a real entry
# point, not a hand-rolled wrapper".  A test that imports any
# of these without carrying the ``real_entry`` marker is a
# smell — the file is in the wrong directory or is missing
# the marker.
_REAL_ENTRY_IMPORTS = (
    "longtask.rpc.server.RequestEnvelope",
    "longtask.rpc.server.route",
    "longtask.cli.daemon.run_daemon_tick",
    "longtask.cli.main",  # CLI module
    "longtask.mcp_server",  # MCP server module
)


def _file_imports_real_entry(path: Path) -> list[str]:
    """Return the list of real-entry imports the test file pulls in.

    Cheap AST scan (no execution).  We only look at top-level
    ``import`` and ``from ... import ...`` statements so an
    import nested inside a function does not trip the rule.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    hits: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                full = alias.name
                if any(full == name or full.startswith(name + ".") for name in _REAL_ENTRY_IMPORTS):
                    hits.append(full)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            full_prefix = module
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                if any(
                    full == name or full.startswith(name + ".") or full_prefix == name
                    for name in _REAL_ENTRY_IMPORTS
                ):
                    hits.append(full)
    return hits


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Walk every collected test; for ones under ``tests/unit/`` that
    pull in a real-entry import, emit a session-scoped warning
    unless the test is marked ``real_entry`` (or is in a
    directory that's explicitly OK to mock).

    The warning is non-fatal: a hard fail would block existing
    tests that the project has lived with, and the goal is to
    make the distinction visible so a future PR can correct it.
    """
    for item in items:
        path = Path(item.fspath)
        # Only inspect tests in tests/unit/.  tests/integration
        # and tests/conformance already do real entry work.
        try:
            path.relative_to(Path(config.rootdir) / "tests" / "unit")
        except ValueError:
            continue
        hits = _file_imports_real_entry(path)
        if not hits:
            continue
        if "real_entry" in item.keywords:
            continue
        # Surface as a warning so it shows up in CI output.
        item.warn(
            pytest.PytestUnknownMarkWarning(
                f"\n[real_entry check] {path.name} imports {hits} "
                f"but is not marked @pytest.mark.real_entry.  "
                f"See docs/wiki/playbook/regression-from-external-review.md "
                f"pattern 7.  Either add the marker (if the test "
                f"exercises the real entry point) or move the test "
                f"to tests/integration/."
            )
        )
