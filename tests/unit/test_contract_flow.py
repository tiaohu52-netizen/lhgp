"""Protocol-aware flow: walk a contract's referenced source files.



The contract walker loads a contract from SQLite, extracts seed strings

from acceptance/execution/context metadata, resolves them to source

files under ``src/``, and merges the per-file flows into one. These

tests pin the four contracts spelled out in the task:



  - no references → empty Flow with the contract module node

  - module-path seed → real nodes from the matching file

  - unknown contract id → empty Flow, no exception

  - 2 contracts pointing at the same file → merged Flow dedupes nodes

"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lhgp.contracts.schema import Acceptance, Budget, ContractDraft
from lhgp.flow.ast_walker import Flow
from lhgp.flow.contract_flow import walk_contract
from lhgp.persistence.schema import connect as _store_connect
from lhgp.persistence.schema import ensure_schema
from lhgp.persistence.store import save_contract
from lhgp.persistence.types import StoreConfig

pytestmark = pytest.mark.unit


NOW = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)


def _make_draft(
    *,
    acceptance: Acceptance,
    context: dict[str, object] | None = None,
    execution: dict[str, object] | None = None,
) -> ContractDraft:
    return ContractDraft(
        title="contract flow test",
        objective="walk referenced source",
        deadline_at=NOW + timedelta(hours=2),
        hard_constraints={},
        acceptance=acceptance,
        workload_initial_hours=1.0,
        budget=Budget(
            max_dispatches=3,
            max_escalations=1,
            max_concurrent_attempts=1,
            max_attempt_minutes=30,
            max_output_bytes=1048576,
        ),
        context=context or {},
        execution=execution or {},
    )


def _make_store(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """Build a fresh SQLite store under ``tmp_path/data/state.db``."""

    root = tmp_path / "data"

    root.mkdir(parents=True, exist_ok=True)

    conn = _store_connect(StoreConfig(db_path=root / "state.db"))

    ensure_schema(conn)

    return conn, root


def _write_fake_file(src_root: Path, dotted: str, body: str) -> Path:
    """Create ``src_root/<dotted-as-path>.py`` with ``body``."""

    parts = dotted.split(".")

    parts[-1] = parts[-1] + ".py"

    target = src_root.joinpath(*parts)

    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(body, encoding="utf-8")

    return target


def _save(conn: sqlite3.Connection, contract_id: str, draft: ContractDraft) -> None:
    save_contract(conn, contract_id=contract_id, draft=draft, now=NOW)


class TestEmptyResolution:
    def test_no_acceptance_checks_returns_empty_flow(self, tmp_path: Path) -> None:
        conn, _ = _make_store(tmp_path)

        try:
            _save(
                conn,
                "lt-empty01",
                _make_draft(acceptance=Acceptance(standard="empty", checks=("noop",))),
            )

            flow = walk_contract(conn, "lt-empty01", src_root=tmp_path / "src")

            assert isinstance(flow, Flow)

            assert flow.source == "contract:lt-empty01"

            # The empty flow carries exactly one module node whose id

            # matches the contract source line — a stable handle for

            # downstream renderers.

            assert len(flow.nodes) == 1

            only = flow.nodes[0]

            assert only.id == "contract:lt-empty01"

            assert only.kind == "module"

            assert flow.edges == ()

        finally:
            conn.close()

    def test_unknown_contract_returns_empty_flow(self, tmp_path: Path) -> None:
        conn, _ = _make_store(tmp_path)

        try:
            flow = walk_contract(conn, "lt-ghost", src_root=tmp_path / "src")

            assert flow.source == "contract:lt-ghost"

            assert len(flow.nodes) == 1

            assert flow.nodes[0].id == "contract:lt-ghost"

        finally:
            conn.close()


class TestModulePathResolution:
    def test_acceptance_check_resolves_real_source_file(self, tmp_path: Path) -> None:
        conn, _ = _make_store(tmp_path)

        src_root = tmp_path / "src"

        try:
            body = (
                "def handle_my_contract():\n"
                "    return 1\n"
                "def helper():\n"
                "    return handle_my_contract()\n"
            )

            _write_fake_file(src_root, "_fake.handle_my_contract", body)

            _save(
                conn,
                "lt-mod01",
                _make_draft(
                    acceptance=Acceptance(
                        standard="must invoke handler",
                        checks=("_fake.handle_my_contract",),
                    )
                ),
            )

            flow = walk_contract(conn, "lt-mod01", src_root=src_root)

            ids = {n.id for n in flow.nodes}

            # Module + the two top-level functions from the fake file.

            assert "_fake.handle_my_contract" in ids

            assert "_fake.handle_my_contract.handle_my_contract" in ids

            assert "_fake.handle_my_contract.helper" in ids

            # And the call edge survived the merge.

            assert any(
                e.src == "_fake.handle_my_contract.helper"
                and e.dst == "_fake.handle_my_contract.handle_my_contract"
                for e in flow.edges
            )

            assert flow.source == "contract:lt-mod01"

        finally:
            conn.close()

    def test_substring_fallback_finds_module_when_no_dotted_seed(self, tmp_path: Path) -> None:
        conn, _ = _make_store(tmp_path)

        src_root = tmp_path / "src"

        try:
            _write_fake_file(
                src_root,
                "_fake.handle_my_contract",
                "def handle_my_contract():\n    return 1\n",
            )

            # Seed has no dots — module path lookup fails; substring

            # grep finds the file by file-stem match.

            _save(
                conn,
                "lt-sub01",
                _make_draft(
                    acceptance=Acceptance(
                        standard="match",
                        checks=("handle_my_contract",),
                    )
                ),
            )

            flow = walk_contract(conn, "lt-sub01", src_root=src_root)

            ids = {n.id for n in flow.nodes}

            assert "_fake.handle_my_contract.handle_my_contract" in ids

        finally:
            conn.close()

    def test_execution_target_seed(self, tmp_path: Path) -> None:
        conn, _ = _make_store(tmp_path)

        src_root = tmp_path / "src"

        try:
            _write_fake_file(
                src_root,
                "lhgp.contracts.handlers.run_thing",
                "def run_thing():\n    return 1\n",
            )

            _save(
                conn,
                "lt-exec01",
                _make_draft(
                    acceptance=Acceptance(standard="ok", checks=("noop",)),
                    execution={
                        "mode": "python_callable",
                        "target": "lhgp.contracts.handlers.run_thing",
                    },
                ),
            )

            flow = walk_contract(conn, "lt-exec01", src_root=src_root)

            ids = {n.id for n in flow.nodes}

            assert "lhgp.contracts.handlers.run_thing.run_thing" in ids

        finally:
            conn.close()

    def test_context_python_module_seed(self, tmp_path: Path) -> None:
        conn, _ = _make_store(tmp_path)

        src_root = tmp_path / "src"

        try:
            _write_fake_file(
                src_root,
                "lhgp.adapters.drivers.foo",
                "def do_thing():\n    return 1\n",
            )

            _save(
                conn,
                "lt-ctx01",
                _make_draft(
                    acceptance=Acceptance(standard="ok", checks=("noop",)),
                    context={"python_module": "lhgp.adapters.drivers.foo", "required": True},
                ),
            )

            flow = walk_contract(conn, "lt-ctx01", src_root=src_root)

            ids = {n.id for n in flow.nodes}

            assert "lhgp.adapters.drivers.foo.do_thing" in ids

        finally:
            conn.close()


class TestMergeDedup:
    def test_two_seeds_same_file_dedupes_nodes(self, tmp_path: Path) -> None:
        """Two seeds that resolve to the same file produce one set of nodes.



        Both ``acceptance.checks`` and ``context.python_module`` point

        at the same file. The walker runs ``walk_source`` once per

        unique path and the merge step keeps the canonical copy.

        """

        conn, _ = _make_store(tmp_path)

        src_root = tmp_path / "src"

        try:
            _write_fake_file(
                src_root,
                "_shared.helper",
                "def helper():\n    return 1\n",
            )

            _save(
                conn,
                "lt-shared01",
                _make_draft(
                    acceptance=Acceptance(
                        standard="shared",
                        checks=("_shared.helper",),
                    ),
                    context={"python_module": "_shared.helper"},
                ),
            )

            flow = walk_contract(conn, "lt-shared01", src_root=src_root)

            # Module + the one function from the file; the second seed

            # must not produce a duplicate.

            ids = [n.id for n in flow.nodes]

            assert ids.count("_shared.helper") == 1

            assert ids.count("_shared.helper.helper") == 1

        finally:
            conn.close()

    def test_oversized_file_is_skipped_not_aborted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A seed that points at an over-budget file is skipped, not fatal.



        Lower the limit for the test rather than allocating 2 MiB of

        text on disk; the skip path is what we want to exercise.

        """

        from lhgp.flow import ast_walker

        monkeypatch.setattr(ast_walker, "_MAX_SOURCE_BYTES", 16)

        conn, _ = _make_store(tmp_path)

        src_root = tmp_path / "src"

        try:
            body = (
                "def giant():\n"
                "    return 'x' * 1024\n"  # 1 KiB body easily clears 16 bytes
            )

            _write_fake_file(src_root, "_big.giant", body)

            _save(
                conn,
                "lt-big01",
                _make_draft(
                    acceptance=Acceptance(
                        standard="big",
                        checks=("_big.giant",),
                    )
                ),
            )

            flow = walk_contract(conn, "lt-big01", src_root=src_root)

            # Oversized file is skipped → only the empty-flow module node.

            assert len(flow.nodes) == 1

            assert flow.nodes[0].id == "contract:lt-big01"

        finally:
            conn.close()


class TestSeedEscapeIsRefused:
    """seed 来自合同（用户/模型提供的数据），必须当不可信处理。

    回归：``_resolve_by_module_path`` 直接 ``src_root / seed.replace('.', '/')``，
    对 ``../../x``、``/abs/x``、``C:x``、UNC 四种写法都不设防；而且
    ``walk_contract`` 先 ``read_text()`` 再用 ``relative_to`` 校验——
    越界文件已经进内存了，校验来得太晚。
    """

    @staticmethod
    def _root(tmp_path: Path) -> Path:
        root = tmp_path / "src"
        root.mkdir(exist_ok=True)
        return root

    @pytest.mark.parametrize(
        "seed",
        [
            "../../../../etc/passwd",
            "../../../outside",
            "/etc/passwd",
            r"\server\share\secret",
            "C:Windows",
            "C:/Windows",
        ],
    )
    def test_module_path_resolution_refuses_out_of_tree(self, tmp_path: Path, seed: str) -> None:
        from lhgp.flow.contract_flow import _resolve_by_module_path

        assert _resolve_by_module_path(self._root(tmp_path), seed) is None

    def test_dot_dot_anywhere_in_seed_is_refused(self, tmp_path: Path) -> None:
        from lhgp.flow.contract_flow import _resolve_by_module_path

        # 中间夹一个 .. 也要拦，不只是前缀
        assert _resolve_by_module_path(self._root(tmp_path), "lhgp..persistence") is None

    def test_normal_module_seed_still_resolves(self, tmp_path: Path) -> None:
        from lhgp.flow.contract_flow import _resolve_by_module_path

        root = self._root(tmp_path)
        (root / "pkg").mkdir()
        target = root / "pkg" / "mod.py"
        target.write_text("def f():\n    ...\n", encoding="utf-8")
        assert _resolve_by_module_path(root, "pkg.mod") == target

    def test_is_within_helper(self, tmp_path: Path) -> None:
        from lhgp.flow.contract_flow import _is_within

        root = self._root(tmp_path)
        assert _is_within(root, root / "lhgp" / "flow" / "cli.py") is True
        assert _is_within(root, root.parent / "README.md") is False
        assert _is_within(root, root / "lhgp" / ".." / ".." / "pyproject.toml") is False
