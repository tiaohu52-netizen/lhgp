"""DESIGN §14：不可信外部文本的入站清洗与注入边界。

两个面各自独立可测：

- :mod:`lhgp.untrusted.sanitize` —— 结构性清洗（转义序列 / 控制符 / 双向格式字符）；
- :mod:`lhgp.untrusted.boundary` —— 注入提示词时的可验证数据边界。

两者都是纯函数、零第三方依赖，可以在适配器收流、证据固化、续跑 brief
拼装三处复用，不需要各自复制一份实现。
"""

from lhgp.untrusted.boundary import UNTRUSTED_FENCE_PREFIX, make_untrusted_block
from lhgp.untrusted.sanitize import has_terminal_controls, sanitize_terminal_text

__all__ = [
    "UNTRUSTED_FENCE_PREFIX",
    "has_terminal_controls",
    "make_untrusted_block",
    "sanitize_terminal_text",
]
