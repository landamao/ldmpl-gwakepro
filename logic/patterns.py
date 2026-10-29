"""正则工具：带缓存的编译、安全匹配、预检。

约定：
- 每一项都是 Python `re` 语法，默认 `re.search`（包含即命中）。
- 以 `#` 开头的项视为注释，跳过。
- 支持 `(?i)` 等内联标志；不额外传 flags，避免用户无法预期。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable

__all__ = ["整理模式", "编译", "校验", "匹配", "匹配详情", "批量校验"]

_MAX_PATTERN_LEN = 2000


def 整理模式(列表: Any) -> list[str]:
    if 列表 is None:
        return []
    if isinstance(列表, str):
        列表 = 列表.replace("，", ",").split("\n")
    if not isinstance(列表, (list, tuple, set)):
        return []
    结果: list[str] = []
    for 项 in 列表:
        if not isinstance(项, str):
            continue
        s = 项.strip()
        if not s or s.startswith("#"):
            continue
        if len(s) > _MAX_PATTERN_LEN:
            s = s[:_MAX_PATTERN_LEN]
        if s not in 结果:
            结果.append(s)
    return 结果


@lru_cache(maxsize=1024)
def 编译(模式: str) -> re.Pattern[str] | None:
    try:
        return re.compile(模式)
    except re.error:
        return None


def 校验(模式: str) -> str | None:
    """返回错误信息，合法则返回 None。"""
    if not isinstance(模式, str) or not 模式.strip():
        return "空模式"
    if len(模式) > _MAX_PATTERN_LEN:
        return f"模式过长（>{_MAX_PATTERN_LEN} 字符）"
    try:
        re.compile(模式)
    except re.error as e:
        return f"正则语法错误：{e}"
    return None


def 批量校验(列表: Iterable[str]) -> list[dict[str, str]]:
    结果 = []
    for 模式 in 整理模式(list(列表)):
        错误 = 校验(模式)
        if 错误:
            结果.append({"模式": 模式, "错误": 错误})
    return 结果


def 匹配(列表: Any, 文本: str) -> str | None:
    """返回首个命中的模式字符串。"""
    if not 文本:
        文本 = ""
    for 模式 in 整理模式(列表):
        正则 = 编译(模式)
        if 正则 is None:
            continue
        if 正则.search(文本):
            return 模式
    return None


def 匹配详情(列表: Any, 文本: str) -> tuple[str, str] | None:
    """返回（命中的模式, 命中的片段）。"""
    if not 文本:
        文本 = ""
    for 模式 in 整理模式(列表):
        正则 = 编译(模式)
        if 正则 is None:
            continue
        结果 = 正则.search(文本)
        if 结果:
            return 模式, 结果.group(0)
    return None
