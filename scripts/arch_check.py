#!/usr/bin/env python3
"""架构依赖方向门（CONTRIBUTING「模块边界约束」）。

用 ast 静态分析 import，强制四平面分层：

    cli → rpc → promoter / scheduler → adapters → persistence → contracts

棘轮：存量违规数 ≤ BASELINE 放行（存量不阻断），超基线即新增违规、门红。
修复存量后必须下调 BASELINE，不得静默放宽（修改本值需在 commit 说明理由）。
fail-closed：任一源码包目录缺失或文件解析失败 → 报错退出。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOTS = (REPO_ROOT / "src" / "longtask", REPO_ROOT / "src" / "lhgp")

# Both trees carry the same layer names, and the rule is about the
# layer, not about which tree a file happens to live in: ``lhgp`` is
# the canonical tree, so a rule written only for ``longtask.*``
# imports would leave the whole ``lhgp`` tree unchecked (it did).
# Every forbidden layer rule is therefore matched against *any*
# package prefix, including a cross-tree hop; ``tests`` is listed so
# the "no src layer may import test helpers" rule stays live.
PACKAGE_PREFIXES = ("longtask", "lhgp", "tests")

# 存量基线（棘轮）。骨架期从零起步：任何违规都是新增。
BASELINE = 0

# 每层的禁止 import 目标（包前缀无关，见上）。
# 规则内容与 CONTRIBUTING「模块边界约束」一一对应；
# 改这里必须先改 CONTRIBUTING 与 DESIGN。
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "contracts": (
        "persistence",
        "scheduler",
        "promoter",
        "adapters",
        "rpc",
        "cli",
        "tests",
    ),
    "persistence": (
        "scheduler",
        "promoter",
        "adapters",
        "rpc",
        "cli",
        "tests",
    ),
    "adapters": (
        "promoter",
        "scheduler",
        "rpc",
        "cli",
        # persistence 只允许包级公开接口；内部实现模块禁止
        "persistence.store",
        "persistence.projections",
        "tests",
    ),
    "scheduler": (
        "adapters",
        "rpc",
        "cli",
        "tests",
    ),
    "promoter": (
        # 只经 adapters 公开接口（base/manifest），禁止具体实现
        "adapters.subprocess_adapter",
        "adapters.fake_executor",
        "rpc",
        "cli",
        "tests",
    ),
    "rpc": (
        "cli",
        "tests",
    ),
    "cli": ("tests",),
}


@dataclass(frozen=True, slots=True)
class Violation:
    file: str
    line: int
    layer: str
    imported: str
    rule: str


def imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    """提取模块级 import 目标（含 from X import Y 的 X）。"""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def strip_package_prefix(module: str) -> str | None:
    """Return the module path inside its package, or None if the
    import is not one of the checked packages.
    """
    for prefix in PACKAGE_PREFIXES:
        if module == prefix:
            return ""
        if module.startswith(prefix + "."):
            return module[len(prefix) + 1 :]
    return None


def check_file(path: Path) -> list[Violation]:
    relative = next(
        (path.relative_to(root) for root in SRC_ROOTS if path.is_relative_to(root)), None
    )
    if relative is None:
        raise ValueError(f"source file is outside configured package roots: {path}")
    layer = relative.parts[0]
    forbidden = FORBIDDEN.get(layer, ("tests",))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[Violation] = []
    for module, lineno in imported_modules(tree):
        inner = strip_package_prefix(module) or module
        for rule in forbidden:
            # Layer rules are prefix-agnostic (matched on ``inner``);
            # ``tests`` is a top-level package, matched on ``module``.
            if inner == rule or inner.startswith(rule + ".") or module.startswith(rule + "."):
                violations.append(
                    Violation(
                        file=path.relative_to(REPO_ROOT).as_posix(),
                        line=lineno,
                        layer=layer,
                        imported=module,
                        rule=f"{layer} must not import {module}",
                    )
                )
    return violations


def main() -> int:
    missing_roots = [root for root in SRC_ROOTS if not root.is_dir()]
    if missing_roots:
        # fail-closed：找不到任一源码目录不假装无违规
        for root in missing_roots:
            print(f"[arch] src tree missing: {root}", file=sys.stderr)
        return 1

    violations: list[Violation] = []
    parse_errors: list[str] = []
    for src_root in SRC_ROOTS:
        for path in sorted(src_root.rglob("*.py")):
            try:
                violations.extend(check_file(path))
            except SyntaxError as exc:
                parse_errors.append(f"{path.relative_to(REPO_ROOT)}: {exc}")

    if parse_errors:
        print("[arch] parse failures (fail-closed):", file=sys.stderr)
        for err in parse_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    count = len(violations)
    print(f"[arch] violations: current {count} / baseline {BASELINE}")
    for v in violations:
        print(
            f"  {v.file}:{v.line}: layer '{v.layer}' imports '{v.imported}' (forbidden: {v.rule})"
        )

    if count > BASELINE:
        print("[arch] NEW violations above baseline; gate red.")
        return 1
    if count < BASELINE:
        print(f"[arch] debt reduced; lower BASELINE from {BASELINE} to {count} (ratchet).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
