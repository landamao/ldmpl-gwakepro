"""消息文本构建。

对齐框架与示例插件的口径：

- 「框架文本」= 唤醒前缀、@自己、@全体 被移除后的纯文本，指令识别用它。
- 「完整消息文本」= 保留 `@某人(QQ)`、`@机器人(myself)`、`@全体成员(all)` 的完整文本，
  正则唤醒 / 屏蔽一律匹配它，因此可以精准命中「艾特了谁」「谁被引用」等场景。

`@自己` 只在完整文本里保留，因为框架会在 `message_str` 中把它去掉，
而「完整文本」需要区分「艾特了机器人」和「艾特了别人」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.core.message.components import At, AtAll, Json, Plain, Poke, Reply

__all__ = ["消息快照", "构建快照", "段类型名"]

_类型名 = {
    Plain: "纯文本",
    AtAll: "艾特全体",
    At: "艾特",
    Reply: "引用",
    Json: "卡片",
    Poke: "戳一戳",
}


@dataclass
class 消息快照:
    去前缀文本: str = ""
    """event.message_str：框架口径，已去掉唤醒前缀与 @自己。"""
    含前缀文本: str = ""
    """从消息链重建的文本（不含 @自己），用于判断是否使用了指令前缀。"""
    完整文本: str = ""
    """正则匹配专用：含 @某人(qq)、@机器人(myself)、@全体(all)。"""

    有艾特自己: bool = False
    有引用自己: bool = False
    有艾特: bool = False
    有引用: bool = False
    被引用者: str = ""
    有艾特全体: bool = False

    段数量: int = 0
    首段类型: str = ""
    类型集合: set[str] = field(default_factory=set)
    纯文本单段: bool = False
    有图片: bool = False
    有卡片: bool = False
    有戳一戳: bool = False

    指令文本: str = ""
    指令全名: str = ""
    指令参数: list[str] = field(default_factory=list)


def 段类型名(段: Any) -> str:
    for 类, 名 in _类型名.items():
        if isinstance(段, 类):
            return 名
    return type(段).__name__


def 构建快照(event: Any) -> 消息快照:
    自己ID = str(event.get_self_id() or "")
    消息链 = list(event.get_messages() or [])

    快照 = 消息快照(
        去前缀文本=str(event.message_str or "").strip(),
        段数量=len(消息链),
    )

    框架段: list[str] = []
    完整段: list[str] = []

    for 段 in 消息链:
        名 = 段类型名(段)
        快照.类型集合.add(名)

        if isinstance(段, Plain):
            框架段.append(段.text or "")
            完整段.append(段.text or "")

        elif isinstance(段, AtAll):
            快照.有艾特全体 = True
            快照.有艾特 = True
            名字 = 段.name or "全体成员"
            完整段.append(f" @{名字}(all) ")

        elif isinstance(段, At):
            qq = str(段.qq)
            名字 = 段.name or qq
            快照.有艾特 = True
            if qq == 自己ID:
                快照.有艾特自己 = True
                完整段.append(f" @{名字}(myself) ")
            else:
                框架段.append(f" @{名字}({qq}) ")
                完整段.append(f" @{名字}({qq}) ")

        elif isinstance(段, Reply):
            快照.有引用 = True
            快照.被引用者 = str(段.sender_id or "")
            if 快照.被引用者 and 快照.被引用者 == 自己ID:
                快照.有引用自己 = True
            # 引用段不进入文本，保持与示例插件一致的口径

        elif isinstance(段, Json):
            快照.有卡片 = True

        elif isinstance(段, Poke):
            快照.有戳一戳 = True

        else:
            # 图片 / 语音 / 视频 / 文件等：只记录类型，不进入文本
            if 名 == "图片":
                快照.有图片 = True

    快照.含前缀文本 = "".join(框架段).strip()
    快照.完整文本 = "".join(完整段).strip()
    快照.首段类型 = 段类型名(消息链[0]) if 消息链 else ""
    快照.纯文本单段 = len(消息链) == 1 and isinstance(消息链[0], Plain)

    参数 = 快照.去前缀文本.split()
    快照.指令参数 = 参数
    快照.指令文本 = 参数[0] if 参数 else ""
    快照.指令全名 = " ".join(参数[:2]) if len(参数) >= 2 else 快照.指令文本

    return 快照
