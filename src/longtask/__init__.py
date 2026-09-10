"""远期任务协议参考实现（骨架）。

设计本体见 DESIGN.md；本包只实现该文档定义的行为，不引入文档外的概念。
"""

__version__ = "0.1.0a13"  # 与 pyproject.toml 的包版本同步; PROTOCOL_VERSION 独立演进
PROTOCOL_VERSION = 1  # DESIGN §11：线协议版本，独立于包版本演进
