"""DESIGN §14：外部进程输出按不可信文本处理，在入站边界做结构性清洗。

执行器的 stdout/stderr 是**任意 harness 的原始输出**，它会流进事件 payload、
证据 details、投影文件，并经由 handover/上下文快照进入下一个 attempt 的提示词。
未清洗的控制序列在这里造成两类实际伤害：

1. **方向欺骗**：U+202E 之类的双向格式字符让同一段文字在终端与提示词里呈现为
   另一段文字，人看到的和执行者写的可以不是一回事；
2. **围栏破坏**：ANSI 着色序列插在 ```lhgp-verdict 围栏与 JSON 之间，会让
   SPEC §12.4 的判定块解析失败——明明是有效证据却退化成「无证据」。

本模块只做结构性剥离，不做任何语义判断：不识别「像不像指令」、不改写措辞。
清洗必须幂等（``sanitize_terminal_text`` 的输出再清洗一次不变），
这样重复经过边界不会累积损耗。
"""

from __future__ import annotations

import unicodedata

_ESC = "\x1b"
_BEL = "\x07"
_ST_FINAL = "\\"

# CSI：参数字节 0x30-0x3F、中间字节 0x20-0x2F、终止字节 0x40-0x7E。
_CSI_FINAL_LOW = 0x40
_CSI_FINAL_HIGH = 0x7E
_CSI_BODY_LOW = 0x20
_CSI_BODY_HIGH = 0x3F

# 字符串型序列的引导字节：OSC、DCS、SOS、PM、APC。
_STRING_INTRODUCERS = frozenset({"]", "P", "X", "^", "_"})

# 保留的控制字符：制表、换行、回车是文本结构的一部分。
_KEEP_C0 = frozenset({"\t", "\n", "\r"})
# 保留的 Unicode 格式字符：零宽连接符与非连接符参与文字成形，不是欺骗面。
_KEEP_FORMAT = frozenset({"\u200c", "\u200d"})

_C1_LOW = 0x80
_C1_HIGH = 0x9F


def _skip_escape(text: str, index: int) -> int:
    """返回 ``text[index] == ESC`` 处整条转义序列之后的下标。

    无法识别或中途截断的序列按「消费到可确定处」处理：宁可从宽丢弃控制面，
    也不把半个序列当普通文本留下（半条序列在终端里同样能改变后续显示）。
    """
    length = len(text)
    cursor = index + 1
    if cursor >= length:
        return length
    intro = text[cursor]
    if intro == "[":
        cursor += 1
        while cursor < length:
            code = ord(text[cursor])
            if _CSI_FINAL_LOW <= code <= _CSI_FINAL_HIGH:
                return cursor + 1
            if not _CSI_BODY_LOW <= code <= _CSI_BODY_HIGH:
                return cursor
            cursor += 1
        return length
    if intro in _STRING_INTRODUCERS:
        cursor += 1
        while cursor < length:
            if text[cursor] == _BEL:
                return cursor + 1
            if text[cursor] == _ESC and cursor + 1 < length and text[cursor + 1] == _ST_FINAL:
                return cursor + 2
            cursor += 1
        return length
    # 其余为两字节 / 多字节转义：中间字节 0x20-0x2F 之后是终止字节 0x30-0x7E。
    while cursor < length and 0x20 <= ord(text[cursor]) <= 0x2F:
        cursor += 1
    if cursor < length and 0x30 <= ord(text[cursor]) <= 0x7E:
        return cursor + 1
    return cursor


def sanitize_terminal_text(text: str) -> str:
    """剥离转义序列、C0/C1 控制符与双向格式字符（DESIGN §14）。

    - 保留 ``\\t`` / ``\\n`` / ``\\r`` 与 U+200C / U+200D；
    - 丢弃 ``\\x1b`` 引导的 CSI / OSC / DCS / SOS / PM / APC 与两字节转义；
    - 丢弃其余 C0、DEL、C1 与 Unicode ``Cf`` 类字符（含 U+202A-U+202E 方向覆盖）；
    - 幂等：``sanitize_terminal_text(sanitize_terminal_text(s)) == sanitize_terminal_text(s)``。
    """
    if not text:
        return ""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == _ESC:
            index = _skip_escape(text, index)
            continue
        code = ord(char)
        if code < 0x20 or code == 0x7F:
            if char in _KEEP_C0:
                out.append(char)
            index += 1
            continue
        if _C1_LOW <= code <= _C1_HIGH:
            index += 1
            continue
        if unicodedata.category(char) == "Cf" and char not in _KEEP_FORMAT:
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def has_terminal_controls(text: str) -> bool:
    """文本是否含会被清洗掉的内容（调用方据此决定要不要提示）。

    以 ``sanitize_terminal_text`` 的结果为准，而不是另写一份判定表——
    两份实现必然漂移，漂移之后告警就与清洗事实对不上了。
    """
    if not text:
        return False
    return sanitize_terminal_text(text) != text


__all__ = ["has_terminal_controls", "sanitize_terminal_text"]
