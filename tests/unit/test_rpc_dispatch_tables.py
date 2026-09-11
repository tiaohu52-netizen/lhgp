"""RPC 分发表一致性：两张手工装配的表必须指向同一批方法。

``longtask/rpc/handlers/__init__.py`` 与 ``lhgp/rpc/handlers/__init__.py``
各自手写了一份 ``Method → handler`` 表（两棵命名空间的错误类不同，不能简单
合并，见 ARCHITECTURE「常见陷阱 3」）。手工装配两份 = 必然漂移：第 6 轮审查
发现 ``Method.CONTRACT_USER_CONFIRM`` 在 canonical 表里、却不在 longtask 表里，
于是经 ``longtask.rpc.handlers.HANDLERS`` 路由的调用者得到 "method not
implemented"，而 canonical 侧正常。这里把「两张表相等」钉成不变量。

另有一条已知缺口：7 个方法在枚举与 ``IDEMPOTENT_METHODS`` 里声明，但两边都
没有 handler。它们是**已登记的缺口**，不是意外——所以这里精确列出，而不是
写成「有多少算多少」：新增缺口会红，补上一个也要来改这张清单（提醒同步文档）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from lhgp.rpc.handlers import HANDLERS as CANONICAL_HANDLERS
from lhgp.rpc.methods import IDEMPOTENT_METHODS, Method
from longtask.rpc.handlers import HANDLERS as LEGACY_HANDLERS

REPO_ROOT = Path(__file__).resolve().parents[2]

# ARCHITECTURE「真身位置地图」记的 rpc/handlers 方向是**混的**，这里把实测结果
# 钉死。审计 B2 的根因就是这行地图记反了：它声称 _common 的真身在 lhgp，于是
# 安全守卫只加在 legacy 侧，canonical 侧缺失。地图若不与实现对账，读地图的人
# （包括 AI）就会照着一个错误的方向改代码。
FACADE_ON: dict[str, str] = {
    # 模块名 → 哪一侧是薄门面（真身在另一侧）
    "contract": "lhgp",  # 真身 1382 行在 longtask，lhgp 是 3 行 import *
    "goal": "lhgp",  # 真身 591 行在 longtask
    "executor": "longtask",  # 真身 136 行在 lhgp
    "protocol": "longtask",  # 真身 152 行在 lhgp
}
# 两侧各有一份实现（不是门面）；_common 另有专门的对拍测试
INDEPENDENT_TREES = frozenset({"_common", "__init__"})
# 只存在于 longtask 侧
LEGACY_ONLY = frozenset({"_lifecycle"})
# 薄门面的行数上限：实测门面侧为 13 行（longtask/executor）、9 行、3 行，
# 而真实现侧最小 83 行。取 20 是两者之间的明确分界——这里只需要一个触发器，
# 不是精确判定「是不是门面」。
_THIN_FACADE_MAX_LINES = 20

# 枚举里有、两边都无 handler 的方法。补实现时要连同 SPEC/DESIGN 一起改。
KNOWN_UNIMPLEMENTED = frozenset(
    {
        Method.CONTEXT_PROMOTE,
        Method.CONTEXT_REFRESH,
        Method.CONTROL_FOLLOWUP,
        Method.CONTROL_NOTIFY,
        Method.CONTROL_SPAWN,
        Method.CONTROL_STEER,
        Method.LEASE_RELEASE,
    }
)


class TestDispatchTablesAgree:
    def test_both_tables_cover_the_same_methods(self) -> None:
        only_canonical = sorted(m.name for m in set(CANONICAL_HANDLERS) - set(LEGACY_HANDLERS))
        only_legacy = sorted(m.name for m in set(LEGACY_HANDLERS) - set(CANONICAL_HANDLERS))
        assert only_canonical == [], f"canonical 表多出（longtask 路由会拒）: {only_canonical}"
        assert only_legacy == [], f"longtask 表多出（canonical 路由会拒）: {only_legacy}"

    def test_each_method_routes_to_the_same_function_object(self) -> None:
        """handler 对象必须同一：两棵树共享实现，只是各自装配了一张表。"""
        for method in sorted(set(CANONICAL_HANDLERS) & set(LEGACY_HANDLERS), key=lambda m: m.name):
            assert CANONICAL_HANDLERS[method] is LEGACY_HANDLERS[method], (
                f"{method.name} 在两张表里指向不同函数——说明出现了一份复制的实现"
            )

    def test_bound_handlers_are_callable(self) -> None:
        for method, handler in CANONICAL_HANDLERS.items():
            assert callable(handler), f"{method.name} 的 handler 不可调用"


class TestKnownUnimplementedStaysPinned:
    def test_only_the_known_methods_lack_a_handler(self) -> None:
        covered = set(CANONICAL_HANDLERS) | set(LEGACY_HANDLERS)
        missing = {m for m in Method if m not in covered}
        assert missing == set(KNOWN_UNIMPLEMENTED), (
            "无 handler 的方法集合变了。新增缺口请一并补 SPEC/DESIGN 与文档；"
            f"实际={sorted(m.name for m in missing)}"
        )

    def test_idempotent_methods_without_handler_are_declared(self) -> None:
        """幂等清单里声明了却没实现的方法，必须落在已知缺口的名单里。

        幂等声明意味着「重试安全」这一承诺；没有 handler 就没有承诺对象。
        """
        unhandled = {m for m in IDEMPOTENT_METHODS if m not in CANONICAL_HANDLERS}
        assert unhandled <= set(KNOWN_UNIMPLEMENTED), (
            f"IDEMPOTENT_METHODS 里有无 handler 且未登记的方法: "
            f"{sorted(m.name for m in unhandled - set(KNOWN_UNIMPLEMENTED))}"
        )


@pytest.mark.parametrize("method_name", ["CONTRACT_USER_CONFIRM", "CONTRACT_AUTO_APPROVE"])
def test_contract_gates_are_exported_by_the_canonical_facade(method_name: str) -> None:
    """canonical 门面必须能再导出这两个 handler。

    回归：``longtask.rpc.handlers.contract.__all__`` 漏了这两个名字，而 lhgp
    门面是 ``import *``——于是 ``from lhgp.rpc.handlers.contract import
    handle_contract_user_confirm`` 直接 ImportError，canonical 命名空间反而
    拿不到签字门（ARCHITECTURE「常见陷阱 1」：facade 必须显式 __all__）。
    """
    import lhgp.rpc.handlers.contract as canonical_contract

    method = getattr(Method, method_name)
    handler = CANONICAL_HANDLERS[method]
    assert getattr(canonical_contract, handler.__name__) is handler


def _module_lines(side: str, name: str) -> int:
    path = REPO_ROOT / "src" / side / "rpc" / "handlers" / f"{name}.py"
    assert path.is_file(), f"缺少 {path}"
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


class TestHandlerModuleDirection:
    """ARCHITECTURE 的 rpc/handlers 方向必须与实现对账。

    守卫方式与分发表一致：把地图钉成会红的测试。有人把薄门面写成真实现（或
    反过来把真实现改成转发），方向就变了，此处必须红，提醒同步地图——否则
    下一个人会照着过期地图改错那一侧（审计 B2 的成因）。
    """

    def test_direction_map_covers_every_module_on_disk(self) -> None:
        on_disk = {path.stem for path in (REPO_ROOT / "src/longtask/rpc/handlers").glob("*.py")}
        known = set(FACADE_ON) | set(INDEPENDENT_TREES) | set(LEGACY_ONLY)
        assert on_disk == known, (
            "rpc/handlers 的文件清单变了。新增模块请先判定真身方向，"
            f"再更新 ARCHITECTURE 地图与本清单；实际={sorted(on_disk)}"
        )

    @pytest.mark.parametrize(("name", "facade_side"), sorted(FACADE_ON.items()))
    def test_facade_side_stays_a_thin_reexport(self, name: str, facade_side: str) -> None:
        implementation_side = "longtask" if facade_side == "lhgp" else "lhgp"
        assert _module_lines(facade_side, name) <= _THIN_FACADE_MAX_LINES, (
            f"{facade_side}/rpc/handlers/{name}.py 不再是薄门面——真身方向变了，"
            "请同步 ARCHITECTURE「真身位置地图」"
        )
        assert _module_lines(implementation_side, name) > _THIN_FACADE_MAX_LINES, (
            f"{implementation_side}/rpc/handlers/{name}.py 不再是真实现"
        )
        # 门面转发必须落在同一批函数对象上（同一实现，不是复制品）
        legacy = importlib.import_module(f"longtask.rpc.handlers.{name}")
        canonical = importlib.import_module(f"lhgp.rpc.handlers.{name}")
        shared = {n for n in dir(legacy) if n.startswith("handle_")} & {
            n for n in dir(canonical) if n.startswith("handle_")
        }
        assert shared, f"{name}: 两侧没有同名 handler，方向表需要复核"
        for handler_name in sorted(shared):
            assert getattr(canonical, handler_name) is getattr(legacy, handler_name), (
                f"{name}.{handler_name} 两侧不是同一对象——门面被复制成了独立实现"
            )

    @pytest.mark.parametrize("name", sorted(INDEPENDENT_TREES))
    def test_independent_modules_keep_two_implementations(self, name: str) -> None:
        for side in ("lhgp", "longtask"):
            assert _module_lines(side, name) > _THIN_FACADE_MAX_LINES, (
                f"{side}/rpc/handlers/{name}.py 变成了薄门面——若这是有意的收敛，"
                "请同步 ARCHITECTURE 地图与 tests/unit/test_handler_common_parity.py"
            )
