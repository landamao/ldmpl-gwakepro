"""黑白名单：一套语法同时用于「用户黑白名单」与「指令黑白名单」。

语法（只有一个列表，一个特殊值 `*`）：
    *          放行全部（写进黑名单时用 `/*` 表示禁止全部）
    /xxx       禁止 xxx
    xxx        允许 xxx
空列表 = 不启用该名单。

注意：列表非空时**必须**显式写 `*` 才会「其余全部放行」，
      否则未列出的条目一律拒绝——这是刻意保留的严格语义。
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = ["通配", "解析名单", "名单启用", "名单通过", "名单描述"]

通配 = "*"


def _迭代(原: Any) -> Iterable[str]:
    if 原 is None:
        return ()
    if isinstance(原, str):
        return tuple(i for i in 原.replace("，", ",").split(","))
    if isinstance(原, (list, tuple, set)):
        return tuple(str(i) for i in 原)
    return (str(原),)


def 解析名单(原: Any) -> tuple[set[str], set[str]]:
    """解析为（黑名单, 白名单）。

    - `/*` 或 `/ *` 会让黑名单直接变成通配（禁止全部）。
    - 同名条目同时出现在两侧时，黑名单优先（显式禁用覆盖通配）。
    """
    黑: set[str] = set()
    白: set[str] = set()

    for 项 in _迭代(原):
        s = str(项).strip()
        if not s:
            continue
        if s.startswith("/") or s.startswith("／"):
            s = s[1:].strip()
            if not s:
                continue
            if s == 通配:
                return {通配}, set()
            黑.add(s)
        else:
            白.add(s)

    if 通配 in 白:
        白 = {通配}
    if 通配 in 黑:
        黑 = {通配}
        白 = set()
    白 -= 黑
    return 黑, 白


def 名单启用(原: Any) -> bool:
    return bool(_迭代(原))


def 名单通过(值: str, 名单: Any) -> bool:
    """名单为空或值未命中白名单时返回 False。"""
    黑, 白 = 解析名单(名单)
    if not (黑 or 白):
        return False
    值 = str(值)
    if 通配 in 黑 or 值 in 黑:
        return False
    if 通配 in 白 or 值 in 白:
        return True
    return False


def 名单描述(原: Any) -> str:
    黑, 白 = 解析名单(原)
    if not (黑 or 白):
        return "未启用"
    部分 = []
    if 通配 in 黑:
        部分.append("禁止全部")
    elif 黑:
        部分.append("禁止 " + "、".join(sorted(黑)))
    if 通配 in 白:
        部分.append("放行全部")
    elif 白:
        部分.append("仅放行 " + "、".join(sorted(白)))
    return "；".join(部分)
