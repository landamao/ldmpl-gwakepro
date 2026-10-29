"""判定引擎。

一条消息进来后，先由 `main` 找到它所属的规则，再交给这里判定。

判定分三段：
1. **规则级**：开关、用户黑白名单、监听、指令、冷却、活跃窗口；
2. **接收流水线**：规则里的 `条件` 按数组顺序从上到下逐条判断，第一条命中的
   决定唤醒还是屏蔽，并带上该条件自己的提示词；
3. **LLM 前流水线**：进入模型前再跑一遍 `LLM前条件`，只认屏蔽——
   命中只终止这一轮回复，不打断其他插件对消息的处理。

因此「唤醒优先还是屏蔽优先」不需要单独开关，调整条件顺序即可表达。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import acl, commands, patterns
from .models import LLM前条件类型, 关闭即屏蔽类型
from .text import 消息快照

__all__ = ["动作", "判定结果", "判定上下文", "规则引擎", "概率通过", "艾特引用命中"]


class 动作(str, Enum):
    WAKE = "wake"
    """唤醒：设置 is_at_or_wake_command，让消息进入 LLM 流程。"""
    STOP = "stop"
    """屏蔽：终止事件传播。"""
    PASS = "pass"
    """本条规则不下结论，交给框架默认行为（通常是不唤醒）。"""
    BYPASS = "bypass"
    """整条规则关闭：本群完全不参与插件处理。"""


@dataclass
class 判定结果:
    动作: 动作
    原因: str = ""
    规则: dict[str, Any] | None = None
    详情: str = ""
    提示词: str = ""
    """该条件专属的 system_reminder，空则回落到规则/全局提示词。"""
    提示词临时: bool = True
    """True：只对这一轮请求生效（不写入对话历史）；False：持久注入。"""
    条件: dict[str, Any] | None = None
    """命中的条件对象（用于写回概率冷却等副作用）。"""

    @property
    def 唤醒(self) -> bool:
        return self.动作 == 动作.WAKE

    @property
    def 屏蔽(self) -> bool:
        return self.动作 == 动作.STOP

    def 描述(self) -> str:
        名字 = (self.规则 or {}).get("备注", "-")
        标签 = "唤醒" if self.唤醒 else "屏蔽" if self.屏蔽 else self.动作.value
        return f"[{标签}] {名字}：{self.原因}{('（' + self.详情 + '）') if self.详情 else ''}"


@dataclass
class 判定上下文:
    群号: str
    发送者: str
    发送者名: str
    管理员: bool
    快照: 消息快照 = field(default_factory=消息快照)
    现在: float = 0.0
    所有指令: set[str] = field(default_factory=set)
    指令前缀: tuple[str, ...] = ()
    平台: str = ""

    @property
    def 是指令(self) -> bool:
        if not self.所有指令:
            return False
        for 候选 in commands.指令候选(self.快照.去前缀文本):
            if 候选 in self.所有指令:
                return True
        return False

    @property
    def 指令候选(self) -> list[str]:
        return [i for i in commands.指令候选(self.快照.去前缀文本) if i in self.所有指令]

    @property
    def 使用前缀(self) -> bool:
        if not self.指令前缀:
            return False
        return self.快照.含前缀文本.startswith(tuple(self.指令前缀))

    @property
    def 艾特了发送者(self) -> bool:
        return f"({self.发送者})" in self.快照.完整文本


def 概率通过(快照: 消息快照, 概率: float, 方式: list[str] | tuple[str, ...]) -> bool:
    if 概率 <= 0:
        return False
    if random.random() >= 概率:
        return False
    方式 = list(方式 or ())
    if "任何消息" in 方式:
        return True
    if "纯文本" in 方式 and 快照.纯文本单段:
        return True
    if "图片" in 方式 and 快照.有图片:
        return True
    if "卡片分享" in 方式 and 快照.有卡片:
        return True
    return False


def 艾特引用命中(类型: str, 快照: 消息快照) -> bool:
    """@ / 引用类条件共用的命中口径。

    开启时用它判断是否唤醒，关闭时用它判断是否屏蔽——两边必须完全一致，
    否则「关闭」会拦住本来不该拦的消息。
    """
    if 类型 == "艾特唤醒":
        return bool(快照.有艾特自己)
    if 类型 == "引用唤醒":
        return bool(快照.有艾特自己 and 快照.有引用自己)
    if 类型 in ("无艾特引用唤醒",):
        return bool(快照.有引用自己 and not 快照.有艾特自己)
    return False


class 规则引擎:
    def __init__(
        self,
        状态: Any,
        取兜底: Callable[[], dict[str, Any]],
        取全局: Callable[[], dict[str, Any]],
    ) -> None:
        self.状态 = 状态
        self._取兜底 = 取兜底
        self._取全局 = 取全局

    # ------------------------------------------------------------------ 工具

    def 决议(self, 规则: dict[str, Any], 字段: str) -> float:
        """解析时间类字段：-1 表示继承兜底规则。"""
        try:
            值 = float(规则.get(字段, -1))
        except (TypeError, ValueError):
            值 = -1.0
        if 值 >= 0:
            return 值
        兜底 = self._取兜底() or {}
        try:
            底 = float(兜底.get(字段, 0) or 0)
        except (TypeError, ValueError):
            底 = 0.0
        return max(底, 0.0)

    # ------------------------------------------------------------------ 判定

    def 判定(self, ctx: 判定上下文, 规则: dict[str, Any]) -> 判定结果:
        if not 规则.get("开关", True):
            return 判定结果(动作.BYPASS, "规则已关闭", 规则)

        豁免 = ctx.管理员 and bool(规则.get("管理员豁免", True))

        # 1. 用户黑白名单
        if acl.名单启用(规则.get("用户黑白名单")) and not acl.名单通过(
            ctx.发送者, 规则.get("用户黑白名单")
        ):
            if not 豁免:
                return 判定结果(
                    动作.STOP, "用户不在名单内", 规则, acl.名单描述(规则.get("用户黑白名单"))
                )

        # 2. 监听唤醒（LLM 主动发起的 standby）
        监听状态, 剩余 = self.状态.检查监听(
            ctx.群号, ctx.发送者, ctx.现在, 艾特了发送者=ctx.艾特了发送者
        )
        if 监听状态 == "hit":
            self.状态.消耗监听(ctx.群号, ctx.发送者)
            return 判定结果(动作.WAKE, "监听唤醒", 规则, f"监听剩余 {剩余:.0f}s")
        # reset：本条消息艾特了被监听者，监听时长已续期，继续正常判定

        # 3. 指令控制
        指令结果 = self._指令判定(ctx, 规则, 豁免)
        if 指令结果 is not None:
            return 指令结果

        # 4. 用户冷却
        if not 豁免:
            冷却 = self.状态.冷却剩余(ctx.发送者, ctx.现在)
            if 冷却 > 0:
                return 判定结果(动作.STOP, "唤醒冷却中", 规则, f"剩余 {冷却:.1f}s")

        # 5. 自定义活跃（/设置活跃）
        自定义到期 = self.状态.自定义活跃到期(ctx.群号, ctx.发送者)
        if 自定义到期 > ctx.现在:
            结果 = self._活跃放行(
                ctx,
                规则,
                自定义到期,
                "自定义活跃",
                self.状态.用户上次唤醒.get(ctx.发送者, 0.0),
            )
            if 结果.动作 == 动作.WAKE:
                self.状态.刷新自定义活跃(ctx.群号, ctx.发送者, ctx.现在)
            return 结果
        if 自定义到期:
            self.状态.自定义活跃.pop((ctx.群号, ctx.发送者), None)

        # 6. 持续活跃窗口
        范围 = 规则.get("活跃范围", "用户级")
        活跃到期 = self.状态.活跃到期(ctx.群号, ctx.发送者, 范围)
        if 活跃到期 > ctx.现在:
            return self._活跃放行(
                ctx,
                规则,
                活跃到期,
                "持续活跃",
                self.状态.上次唤醒(ctx.群号, ctx.发送者, 范围),
            )
        if 活跃到期:
            self.状态.结束活跃(ctx.群号, ctx.发送者, 范围)

        # 7. 条件流水线
        结果 = self.条件判定(ctx, 规则)
        if 结果 is not None:
            return 结果

        return 判定结果(动作.PASS, "未命中任何条件", 规则)

    # ------------------------------------------------------------- 条件流水线

    def 判定LLM前(self, ctx: 判定上下文, 规则: dict[str, Any]) -> 判定结果 | None:
        """LLM 请求前的第二道闸门：只跑 `LLM前条件`，且只认屏蔽。

        这一步发生在消息已经决定进入模型之后，因此不做唤醒判断，也不改唤醒标志：
        命中就终止这一轮回复，其他插件对这条消息的处理不受影响。
        """
        if not 规则.get("开关", True):
            return None
        return self.条件判定(ctx, 规则, 规则.get("LLM前条件") or [], 阶段="LLM")

    def 条件判定(
        self,
        ctx: 判定上下文,
        规则: dict[str, Any],
        条件列表: list[dict[str, Any]] | None = None,
        阶段: str = "接收",
    ) -> 判定结果 | None:
        """按数组顺序从上到下逐条判断，返回第一条命中的结果。

        `阶段="LLM"` 时只认屏蔽：唤醒类条件在这一步没有意义，直接跳过。
        """
        if 条件列表 is None:
            条件列表 = 规则.get("条件") or []
        for 条件 in 条件列表:
            if not isinstance(条件, dict):
                continue
            if 阶段 == "LLM" and str(条件.get("类型") or "") not in LLM前条件类型:
                continue
            结果 = self.单条判定(ctx, 规则, 条件, 阶段)
            if 结果 is None:
                continue
            if 阶段 == "LLM" and 结果.动作 != 动作.STOP:
                continue
            return 结果
        return None

    def 单条判定(
        self,
        ctx: 判定上下文,
        规则: dict[str, Any],
        条件: dict[str, Any],
        阶段: str = "接收",
    ) -> 判定结果 | None:
        类型 = str(条件.get("类型") or "")
        快照 = ctx.快照
        提示词 = str(条件.get("提示词") or "").strip()
        提示词临时 = bool(条件.get("提示词临时", True))
        豁免 = ctx.管理员 and bool(规则.get("管理员豁免", True))

        def 唤醒(详情: str = "") -> 判定结果:
            return 判定结果(动作.WAKE, 类型, 规则, 详情, 提示词, 提示词临时, 条件)

        def 屏蔽(详情: str = "") -> 判定结果 | None:
            # 设置项写明「管理员不受屏蔽条件限制」，所有屏蔽类条件走同一套豁免
            if 豁免:
                return None
            return 判定结果(动作.STOP, 类型, 规则, 详情, 提示词, 提示词临时, 条件)

        # 关闭的条件一般等于跳过；但接收流水线里的 @ / 引用类唤醒框架自己也会判定，
        # 只跳过的话机器人照样会被叫起来，所以它们关闭时按同样口径转成屏蔽。
        # LLM 前流水线只有屏蔽，「关闭」就是「不拦这条」，保持跳过。
        if not 条件.get("开关", True):
            if 阶段 == "LLM":
                return None
            if 类型 in 关闭即屏蔽类型 and 艾特引用命中(类型, 快照):
                return 屏蔽("该条件已关闭，命中即屏蔽")
            return None

        if 类型 in 关闭即屏蔽类型:
            if not 艾特引用命中(类型, 快照):
                return None
            if 阶段 == "LLM":
                return 屏蔽("LLM 请求前拦截")
            return 唤醒()

        if 类型 == "唤醒正则":
            命中 = patterns.匹配详情(条件.get("正则"), 快照.完整文本)
            return 唤醒(命中[0]) if 命中 else None

        if 类型 == "概率唤醒":
            条件ID = str(条件.get("id") or "")
            剩余 = self.状态.概率冷却剩余(ctx.群号, 条件ID, ctx.现在)
            if 剩余 > 0:
                return None
            概率 = float(条件.get("概率") or 0)
            if 概率通过(快照, 概率, 条件.get("概率方式") or ()):
                return 唤醒(f"{概率 * 100:.0f}%")
            return None

        if 类型 == "屏蔽正则":
            命中 = patterns.匹配详情(条件.get("正则"), 快照.完整文本)
            return 屏蔽(命中[0]) if 命中 else None

        if 类型 == "屏蔽卡片":
            return 屏蔽() if 快照.有卡片 else None

        if 类型 == "屏蔽戳一戳":
            return 屏蔽() if 快照.有戳一戳 else None

        if 类型 == "屏蔽空消息链":
            return 屏蔽("空消息链") if 快照.段数量 == 0 else None

        if 类型 == "其余屏蔽":
            return 屏蔽()

        return None

    # ------------------------------------------------------------- 子判定

    def _活跃放行(
        self,
        ctx: 判定上下文,
        规则: dict[str, Any],
        到期: float,
        原因: str,
        上次唤醒: float,
    ) -> 判定结果:
        """活跃期内的放行逻辑：屏蔽条件照常生效，其余消息按间隔唤醒。"""
        结果 = self.条件判定(ctx, 规则)
        if 结果 is not None and 结果.动作 == 动作.STOP:
            return 结果

        间隔 = self.决议(规则, "活跃间隔")
        if 间隔 > 0 and ctx.现在 - 上次唤醒 < 间隔:
            return 判定结果(动作.STOP, f"{原因}间隔不足", 规则, f"间隔 {间隔:.1f}s")

        if 结果 is not None:
            结果.详情 = f"{结果.详情} · {原因}中".strip(" ·")
            return 结果

        return 判定结果(动作.WAKE, 原因, 规则, f"剩余 {到期 - ctx.现在:.0f}s")

    def _指令判定(self, ctx: 判定上下文, 规则: dict[str, Any], 豁免: bool) -> 判定结果 | None:
        快照 = ctx.快照
        候选 = ctx.指令候选
        像指令 = ctx.使用前缀 or bool(候选)
        首词 = commands.取首词(快照.去前缀文本) or 快照.指令文本
        全名 = " ".join(快照.去前缀文本.split()[:2]) if 快照.去前缀文本.split() else 首词

        if 规则.get("禁用系统指令") and 像指令 and not 豁免:
            if 首词 in commands.系统指令 or 全名 in commands.系统指令:
                return 判定结果(动作.STOP, "系统指令已禁用", 规则, 首词)

        名单 = 规则.get("指令黑白名单")
        if acl.名单启用(名单) and 候选:
            通过 = any(acl.名单通过(项, 名单) for 项 in 候选)
            if not 通过 and not 豁免:
                return 判定结果(动作.STOP, "指令不在名单内", 规则, "、".join(候选))

        # 「禁前唤醒」不在这里处理：它只负责不叫醒机器人，不该终止事件，
        # 否则别的插件就收不到这条消息了 —— 统一放到 LLM 请求前的钩子里拦。
        return None

    # ------------------------------------------------------------------ 执行

    def 应用(self, ctx: 判定上下文, 结果: 判定结果, event: Any) -> None:
        if 结果.动作 == 动作.WAKE:
            event.is_at_or_wake_command = True
            event.set_extra("qwb_唤醒来源", 结果.原因)
            event.set_extra("qwb_唤醒提示词", 结果.提示词)
            event.set_extra("qwb_提示词临时", 结果.提示词临时)
            # 命中条件时，提示词由条件自己说了算：留空就代表「不注入」，
            # 不再回落到规则/全局默认；未命中条件（活跃、监听）才走默认链路。
            event.set_extra("qwb_有命中条件", 结果.条件 is not None)
            event.set_extra("qwb_唤醒规则", (结果.规则 or {}).get("备注", ""))
            # 概率唤醒命中后进入冷却：接下来 N 分钟内不再判断这条概率条件
            if 结果.条件 and 结果.条件.get("类型") == "概率唤醒":
                try:
                    分钟 = float(结果.条件.get("概率冷却") or 0)
                except (TypeError, ValueError):
                    分钟 = 0.0
                if 分钟 > 0:
                    self.状态.设置概率冷却(
                        ctx.群号, str(结果.条件.get("id") or ""), 分钟 * 60, ctx.现在
                    )
            if 结果.规则:
                self.记录活跃(ctx.群号, ctx.发送者, 结果.规则, ctx.现在)
                if not (ctx.管理员 and 结果.规则.get("管理员豁免", True)):
                    冷却 = self.决议(结果.规则, "唤醒冷却")
                    if 冷却 > 0:
                        self.状态.设置冷却(ctx.发送者, 冷却, ctx.现在)
            return

        if 结果.动作 == 动作.STOP:
            event.set_extra("qwb_屏蔽原因", 结果.原因)
            event.stop_event()

    def 记录活跃(self, 群号: str, 发送者: str, 规则: dict[str, Any], 现在: float | None = None) -> None:
        if not 群号 or not 发送者:
            return
        自定义 = self.状态.刷新自定义活跃(群号, 发送者, 现在)
        if 自定义 > 0:
            # 自定义活跃同样要刷新「上次唤醒」，否则活跃间隔会拿旧时间比较
            self.状态.用户上次唤醒[发送者] = time.time() if 现在 is None else 现在
            return
        时长 = self.决议(规则, "持续活跃")
        if 时长 <= 0:
            return
        self.状态.记录活跃(群号, 发送者, 规则.get("活跃范围", "用户级"), 时长, 现在)
