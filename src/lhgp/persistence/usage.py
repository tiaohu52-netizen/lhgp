"""attempt 消耗台账的形状校验（DESIGN §11.3 写回字段）。

usage 由**执行者写回时自报**——harness 最清楚自己的消耗，守护进程既无法
复算也不该猜。所以这里的校验只回答一件事：「这个自报能不能作为台账数据
落库」。负数、错型、未知键一律拒（fail-closed）：台账是预算强制的地基
（本轮只记账不强制），脏数据进账等于预算线画在沙子上。

字段口径（对齐 openpi workflow usage() 的下界语义）：
- ``input_tokens`` / ``output_tokens``：必填、非负整数；
- ``cache_read_tokens`` / ``cache_write_tokens``：可选、非负整数；
- ``cost_estimate``：可选、非负数（货币单位由部署约定，协议不解释）。
自报值是**下界**：压缩/缓存刷新可能让真实消耗更高，消费方不得把它当精确值。
"""

from __future__ import annotations

from typing import Any

# 整数 token 字段：必填与可选分开——有 output 必有 input，缓存字段按需自报。
_REQUIRED_INT_FIELDS = ("input_tokens", "output_tokens")
_OPTIONAL_INT_FIELDS = ("cache_read_tokens", "cache_write_tokens")
# 成本允许小数；协议不解释货币单位。
_OPTIONAL_FLOAT_FIELDS = ("cost_estimate",)

_KNOWN_FIELDS = frozenset(_REQUIRED_INT_FIELDS + _OPTIONAL_INT_FIELDS + _OPTIONAL_FLOAT_FIELDS)


class UsageInvalidError(ValueError):
    """usage 自报不合规：负数、错型或未知键——拒收，绝不静默截断。"""


def normalize_usage(raw: Any) -> dict[str, Any]:
    """校验并归一 usage 自报；不合规抛 :class:`UsageInvalidError`。

    通过的返回值只含已知字段且类型正确，可直接 json.dumps 落库。
    ``raw=None`` 视为「本次写回不携带台账」，返回 ``{}``（调用方据此跳过落库）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise UsageInvalidError("usage must be an object")
    unknown = sorted(set(raw) - _KNOWN_FIELDS)
    if unknown:
        raise UsageInvalidError(f"usage has unknown fields: {unknown}")

    out: dict[str, Any] = {}
    for field in _REQUIRED_INT_FIELDS:
        out[field] = _int_field(raw, field, required=True)
    for field in _OPTIONAL_INT_FIELDS:
        if field in raw:
            out[field] = _int_field(raw, field, required=False)
    for field in _OPTIONAL_FLOAT_FIELDS:
        if field in raw:
            out[field] = _float_field(raw, field)
    return out


def _int_field(raw: dict[str, Any], field: str, *, required: bool) -> int:
    value = raw.get(field)
    if value is None:
        if required:
            raise UsageInvalidError(f"usage.{field} is required")
        raise UsageInvalidError(f"usage.{field} must not be null when present")
    # bool 是 int 的子类：True 会被当 1 记账，必须显式排除。
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageInvalidError(f"usage.{field} must be a non-negative integer")
    if value < 0:
        raise UsageInvalidError(f"usage.{field} must be >= 0, got {value}")
    return int(value)


def _float_field(raw: dict[str, Any], field: str) -> float:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageInvalidError(f"usage.{field} must be a non-negative number")
    if value < 0:
        raise UsageInvalidError(f"usage.{field} must be >= 0, got {value}")
    return float(value)


__all__ = ["UsageInvalidError", "normalize_usage"]
