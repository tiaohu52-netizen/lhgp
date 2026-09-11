"""LHGP public namespace shim.

``lhgp`` 是协议的 canonical Python 命名空间，但**它不是整体上的门面**：
每个模块的真身在哪一侧并不一致——`lhgp/cli/` 全部是 `longtask/cli/` 的薄门面
（3~10 行），而 `lhgp/contracts/`、`lhgp/persistence/attempts.py` 等模块本身就是
真身，`rpc/handlers/contract.py` 的真身反而在 longtask 侧。逐个模块的权威答案
在 ``ARCHITECTURE.md`` 的「真身位置地图」，并由此处的实现方向测试对账
（``tests/unit/test_rpc_dispatch_tables.py``、``tests/unit/test_cli.py``）。

注意这不是「只有一份实现」：至少 ``rpc/handlers/_common.py`` 是两侧各一份独立
实现（审计 B2：安全守卫曾只加在 legacy 侧），方向与差异由
``tests/unit/test_handler_common_parity.py`` 钉住。

本模块自身只做命名空间身份：依赖为零，导出协议版本，不复制运行时。
"""

from longtask import PROTOCOL_VERSION, __version__

__all__ = ["PROTOCOL_VERSION", "__version__"]
