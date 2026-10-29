"""指令表：扫描框架里注册的所有指令，供指令黑白名单与「指令不唤醒」使用。"""

from __future__ import annotations

from typing import Any, Iterable

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import star_handlers_registry

__all__ = ["扫描指令", "指令候选", "取首词", "系统指令"]

# 框架内置的管理类指令，默认归入「系统指令」
系统指令: frozenset[str] = frozenset(
    {
        "alter_cmd",
        "dashboard_update",
        "del",
        "deop",
        "dwl",
        "groupnew",
        "help",
        "history",
        "key",
        "llm",
        "ls",
        "model",
        "new",
        "op",
        "persona",
        "provider",
        "rename",
        "reset",
        "sid",
        "switch",
        "t2i",
        "tts",
        "wl",
        "stop",
        "plugin",
        "image",
        "set",
    }
)


def _收集(过滤器: Any, 结果: set[str]) -> None:
    取值 = getattr(过滤器, "get_complete_command_names", None)
    if callable(取值):
        try:
            for 名 in 取值():
                if isinstance(名, str) and 名.strip():
                    结果.add(名.strip())
            return
        except Exception:  # noqa: BLE001
            pass
    if isinstance(过滤器, CommandGroupFilter):
        名 = getattr(过滤器, "group_name", "")
        if isinstance(名, str) and 名.strip():
            结果.add(名.strip())
    elif isinstance(过滤器, CommandFilter):
        名 = getattr(过滤器, "command_name", "")
        if isinstance(名, str) and 名.strip():
            结果.add(名.strip())
        for 别名 in getattr(过滤器, "alias", set()) or set():
            if isinstance(别名, str) and 别名.strip():
                结果.add(别名.strip())


def 扫描指令(额外: Iterable[str] = ()) -> list[str]:
    """扫描所有已注册指令（含别名与指令组全名）。"""
    结果: set[str] = set()
    for handler in star_handlers_registry:
        for 过滤器 in getattr(handler, "event_filters", []) or []:
            if isinstance(过滤器, (CommandFilter, CommandGroupFilter)):
                _收集(过滤器, 结果)

    for 名 in 额外 or ():
        if isinstance(名, str) and 名.strip():
            结果.add(名.strip())

    结果.discard("")
    中文 = sorted((i for i in 结果 if _是中文开头(i)))
    其他 = sorted((i for i in 结果 if not _是中文开头(i)), key=str.lower)
    return 中文 + 其他


def _是中文开头(文本: str) -> bool:
    return bool(文本) and "\u4e00" <= 文本[0] <= "\u9fff"


def 取首词(文本: str) -> str:
    部分 = (文本 or "").strip().split()
    return 部分[0] if 部分 else ""


def 指令候选(文本: str) -> list[str]:
    """把一段文本按「首词」「前两词」「前三词」拆成可能的指令名。"""
    部分 = (文本 or "").strip().split()
    if not 部分:
        return []
    候选 = [部分[0]]
    if len(部分) >= 2:
        候选.append(" ".join(部分[:2]))
    if len(部分) >= 3:
        候选.append(" ".join(部分[:3]))
    return 候选
