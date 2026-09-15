"""命名空间镜像面覆盖：canonical 的模块必须在 legacy 侧有门面。

ARCHITECTURE「真身位置地图」承诺：`contracts/`、`acceptance/` 等领域的真身在
`lhgp`，`longtask` 侧是门面。承诺必须**成套**成立——只镜像 12 个模块里的 10 个，
等于告诉使用者「按老路径导入即可」，然后在其中两个模块上 ModuleNotFoundError。

现状缺口（审计 C3 家族，本次补齐）：`lhgp/contracts/auto_approve.py` 与
`lhgp/contracts/plan.py` 从来没有 legacy 门面，而同级 10 个模块都有。写
``test_patch_contract_snapshot.py`` 时正是按惯例写
``from longtask.contracts.auto_approve import AutoApprove`` 才撞出来的。

本文件是 rpc/handlers 方向守卫（``test_rpc_dispatch_tables.py``）与 CLI 方向守卫
（``test_cli.py::TestCliFacadeDirection``）在**模块存在性**这一层的对应物。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# 领域目录 → (canonical 侧, legacy 侧)
MIRRORED_DOMAINS = {"contracts": ("lhgp", "longtask")}
# 有意不镜像的模块：模块名 → 理由。空集表示当前没有例外。
INTENTIONAL_EXCEPTIONS: dict[str, str] = {}


def _public_modules(package: Path) -> set[str]:
    return {
        path.stem
        for path in package.glob("*.py")
        if path.stem != "__init__" and not path.stem.startswith("_")
    }


@pytest.mark.parametrize("domain", sorted(MIRRORED_DOMAINS))
def test_every_canonical_module_has_a_legacy_facade(domain: str) -> None:
    canonical_side, legacy_side = MIRRORED_DOMAINS[domain]
    canonical = _public_modules(REPO_ROOT / "src" / canonical_side / domain)
    legacy = _public_modules(REPO_ROOT / "src" / legacy_side / domain)
    missing = sorted(canonical - legacy - set(INTENTIONAL_EXCEPTIONS))
    assert missing == [], (
        f"{canonical_side}/{domain}/ 的这些模块在 {legacy_side}/{domain}/ 没有门面："
        f"{missing}。要么补一个 3 行转发门面（与同级模块一致），要么加进 "
        "INTENTIONAL_EXCEPTIONS 并写明理由。"
    )
    extra = sorted(legacy - canonical)
    assert extra == [], (
        f"{legacy_side}/{domain}/ 多出这些模块：{extra}——legacy 侧只应转发，"
        "不应有自己的实现（若是有意的，请更新 ARCHITECTURE 地图）"
    )


def _star_import_surface(module: object) -> set[str]:
    """``from module import *`` 会转发的名字集合（Python 语义，不是 ``dir()``）。

    源模块定义 ``__all__`` 时只转发 ``__all__``；否则转发全部非下划线名。
    用 ``dir()`` 直接比会要求门面转发源模块**导入**的东西（``Any``、``dataclass``
    这类），那是把门面语义写错——门面转发的正是源的公开面。
    """
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return set(declared)
    return {name for name in dir(module) if not name.startswith("_")}


@pytest.mark.parametrize("domain", sorted(MIRRORED_DOMAINS))
def test_legacy_facades_forward_the_canonical_objects(domain: str) -> None:
    """门面必须转发同一批对象，不是复制品。"""
    canonical_side, legacy_side = MIRRORED_DOMAINS[domain]
    canonical = _public_modules(REPO_ROOT / "src" / canonical_side / domain)
    for name in sorted(canonical):
        facade = importlib.import_module(f"{legacy_side}.{domain}.{name}")
        source = importlib.import_module(f"{canonical_side}.{domain}.{name}")
        expected = _star_import_surface(source)
        assert expected, f"{canonical_side}.{domain}.{name} 的公开面是空的"
        missing = {n for n in expected if not hasattr(facade, n)}
        assert missing == set(), f"{legacy_side}.{domain}.{name} 漏了公开名 {sorted(missing)}"
        for attr in sorted(expected):
            assert getattr(facade, attr) is getattr(source, attr), (
                f"{legacy_side}.{domain}.{name}.{attr} 不是同一对象——门面被复制成了实现"
            )
