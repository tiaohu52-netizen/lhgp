"""架构依赖门的正向能力测试（``scripts/arch_check.py``）。

此前这个门**全仓无测试**：`tests/unit/test_architecture.py` 是另写一套 AST
规则，既不 import 也不执行门禁脚本本身。于是门里的三处盲点长期无人发现——
门「通过」只证明它没看见违规，不证明没有违规。

本文件钉住两件事：
1. **正向能力**：三种 import 写法都必须能被抓到（import 包、from 包 import
   子模块、相对 import）。这三条正是曾经的漏检路径。
2. **当前存量**：仓库现在确实是 0 违规（棘轮基线为 0），加固后没有新增。
"""

from __future__ import annotations

import ast

import pytest
from scripts import arch_check

pytestmark = pytest.mark.unit


def _hits(layer: str, source: str, package: list[str]) -> list[str]:
    """按门的判定链（imported_modules → rule_violated）收集命中。"""
    tree = ast.parse(source)
    return [
        module
        for module, _lineno in arch_check.imported_modules(tree, package)
        if arch_check.rule_violated(layer, module) is not None
    ]


class TestImportShapeCoverage:
    """三种 import 写法都要能穿到规则判定（曾经的盲点）。"""

    def test_plain_import_of_package_is_caught(self) -> None:
        hits = _hits("contracts", "import longtask.persistence\n", ["longtask", "contracts"])
        assert "longtask.persistence" in hits

    def test_from_package_import_submodule_is_caught(self) -> None:
        """``from longtask import persistence`` 的别名可能正是被禁的子包。"""
        hits = _hits("contracts", "from longtask import persistence\n", ["longtask", "contracts"])
        assert "longtask.persistence" in hits

    def test_relative_import_is_caught(self) -> None:
        """``from ..persistence import store``（level=2）曾被整体丢弃。"""
        hits = _hits(
            "contracts",
            "from ..persistence import store\n",
            ["longtask", "contracts"],
        )
        assert "longtask.persistence" in hits

    def test_single_dot_resolves_to_the_files_own_package(self) -> None:
        """``from .persistence import store``（level=1）解析到同层子包。

        这条是**解析正确性**断言：单点在 ``longtask.contracts`` 内解析成
        ``longtask.contracts.persistence``（同层，合法），不是 ``longtask.persistence``。
        """
        tree = ast.parse("from .persistence import store\n")
        modules = [m for m, _ in arch_check.imported_modules(tree, ["longtask", "contracts"])]
        assert "longtask.contracts.persistence" in modules
        assert "longtask.persistence" not in modules

    def test_parent_package_importing_child_module_is_caught(self) -> None:
        """promoter 层禁止 ``adapters.subprocess_adapter``；语句只报父包。

        旧实现只记录 ``node.module``（``longtask.adapters``），规则目标是子模块
        ``adapters.subprocess_adapter``，于是永远匹配不上。
        """
        hits = _hits(
            "promoter",
            "from longtask.adapters import subprocess_adapter\n",
            ["longtask", "promoter"],
        )
        assert "longtask.adapters.subprocess_adapter" in hits

    def test_cross_tree_hop_is_caught(self) -> None:
        """规则按层名匹配、与包前缀无关：lhgp 侧违规同样要抓。"""
        hits = _hits(
            "contracts", "from lhgp.persistence.store import save_contract\n", ["lhgp", "contracts"]
        )
        assert "lhgp.persistence.store" in hits


class TestNoFalsePositives:
    """合法 import 不得被误报（门一旦误报就会被人为放宽）。"""

    @pytest.mark.parametrize(
        ("layer", "source"),
        [
            ("cli", "from lhgp.contracts.validation import is_safe_contract_id\n"),
            ("rpc", "from lhgp.persistence.store import get_contract\n"),
            ("persistence", "from lhgp.contracts.state_machine import is_valid_transition\n"),
            ("contracts", "from lhgp.contracts.schema import Acceptance\n"),
            # adapters 只允许 persistence 的包级公开接口，禁止内部实现模块
            ("adapters", "from longtask.persistence import get_contract\n"),
        ],
    )
    def test_legal_imports_are_clean(self, layer: str, source: str) -> None:
        package = ["longtask", layer]
        assert _hits(layer, source, package) == []

    def test_sibling_module_with_prefix_name_is_not_matched(self) -> None:
        """``persistence.store_helpers`` 不得被 ``persistence.store`` 规则吃掉。"""
        assert arch_check.rule_violated("adapters", "longtask.persistence.store_helpers") is None


class TestRepositoryIsClean:
    def test_current_tree_has_no_violations(self) -> None:
        """棘轮基线为 0：当前源码树必须真的 0 违规。"""
        violations: list[arch_check.Violation] = []
        for src_root in arch_check.SRC_ROOTS:
            for path in sorted(src_root.rglob("*.py")):
                violations.extend(arch_check.check_file(path))
        assert violations == [], [f"{v.file}:{v.line} {v.rule}" for v in violations]

    def test_baseline_is_zero(self) -> None:
        """BASELINE 只允许下调；调高必须在 commit 里说明理由。"""
        assert arch_check.BASELINE == 0
