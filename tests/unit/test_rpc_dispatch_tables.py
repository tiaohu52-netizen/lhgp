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

import pytest

from lhgp.rpc.handlers import HANDLERS as CANONICAL_HANDLERS
from lhgp.rpc.methods import IDEMPOTENT_METHODS, Method
from longtask.rpc.handlers import HANDLERS as LEGACY_HANDLERS

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
