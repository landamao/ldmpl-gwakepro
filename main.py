"""群唤醒增强 Pro

按群聊精细控制「什么消息能唤醒机器人、什么消息要被屏蔽」。

核心模型：一条规则 = 一批设置 + 两条**条件流水线**。

1. 接收流水线（`条件`）：消息进来时逐条判断，第一条命中的决定唤醒还是屏蔽；
   唤醒类条件可以带自己的 system_reminder 提示词。
2. LLM 请求前流水线（`LLM前条件`）：进入模型前再判一次，只做屏蔽——
   屏蔽类条件与 @ / 引用三类唤醒（同名，动作变屏蔽），命中只终止这一轮回复，
   不打断其他插件对消息的处理
   （走 `on_waiting_llm_request` 钩子，用框架默认优先级）。

其它要点：

1. 数据保存在插件数据目录的 `rules.json`，不使用 `_conf_schema.json`，
   插件详情页只展示自定义的「规则面板」。
2. `规则索引[群号] = 规则对象`（直接持有列表元素，不再存下标）；
   规则列表的顺序就是规则之间的优先级，越靠前越优先。
3. 正则统一匹配「完整消息文本」（含 `@某人(qq)`、`@机器人(myself)`）。
4. 用户黑白名单与指令黑白名单共用一套语法，唯一特殊值 `*`。
"""

from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.all import (
    AstrMessageEvent,
    Context,
    ProviderRequest,
    Star,
    StarTools,
    filter,
)
from astrbot.core.agent.message import TextPart

from .logic import acl, commands as 指令工具, models, patterns
from .logic.engine import 判定上下文, 动作, 规则引擎
from .logic.state import 运行状态
from .logic.store import 数据存储
from .logic.text import 构建快照

try:  # 面板接口通过 quart 兼容层读取请求
    from quart import request as _quart_request
except Exception:  # noqa: BLE001
    _quart_request = None

插件名 = "群唤醒增强Pro"
入口优先级 = 6666

唤醒类型 = "、".join(models.唤醒条件类型)
屏蔽类型 = "、".join(models.屏蔽条件类型)
LLM前类型 = "、".join(models.LLM前条件类型)

帮助文本 = f"""
📖 群唤醒增强 Pro · 帮助

一条规则 = 一批设置 + 两条「条件流水线」。
① 接收消息流水线：消息进来时逐条判断，第一条命中的决定「唤醒」还是「屏蔽」；
② LLM 请求前流水线：进入模型前再判一次，只做屏蔽（屏蔽类 + @/引用三类唤醒），
   不打断其他插件，命令里用 L 开头的序号（L1、L2…）。
唤醒类条件还可以写自己的 system_reminder 提示词，注入到那一轮对话开头。

—— 管理员指令 ——
• /群规则 [群号]                 查看规则：设置 + 两条流水线
• /规则列表                     列出全部规则（含群号，顺序即优先级）
• /添加群规 <群号> [备注]         新建规则
• /设置群规 <键> <值> [群号] [操作]  修改设置项；列表字段默认替换，可 add/del/rep
• /删除群规 <群号>               把群号从规则里移除（最后一个则整条删除）
• /删除规则 <备注或ID>           直接删除整条规则
• /移动规则 <备注或ID> <上|下>    调整规则顺序（越靠前越优先）
• /条件列表 [群号]               按判断顺序列出两条流水线
• /添加条件 <类型> [提示词] [群号]
   类型前加 llm: 表示加到 LLM 请求前流水线，例如 /添加条件 llm:屏蔽正则
• /删除条件 <序号|L序号> [群号]
• /移动条件 <序号|L序号> <上|下> [群号]
• /设置条件 <序号|L序号> <字段> <值> [群号]
• /所有指令 / /刷新指令           查看、重扫框架指令表
• /设置活跃 <秒> / /停止活跃      自定义活跃时长
• /群规帮助                     本帮助

—— 条件类型 ——
唤醒：{唤醒类型}
屏蔽：{屏蔽类型}
LLM 请求前可用：{LLM前类型}

—— 关于「@ / 引用」三类唤醒 ——
• 艾特唤醒：消息里 @ 了机器人
• 引用唤醒：同时 @机器人 并引用机器人消息
• 无艾特引用唤醒：只引用机器人消息（不 @）
• 两条流水线里名字完全一样，只是动作不同：
  接收流水线 —— 开启 = 命中即唤醒，关闭 = 命中即屏蔽此消息
  LLM 前流水线 —— 开启 = 命中即屏蔽这条回复，关闭 = 不拦
• 「关闭 = 屏蔽」的原因：框架自身会把 @ / 引用当唤醒，插件只跳过的话照样会回复
• 什么都不想管就删掉这条条件：插件不唤醒也不屏蔽，交给框架默认判定

—— 设置项 ——
基础：开关 / 备注（必填）/ 群号（可留空）
• 备注：每条规则的唯一标识，不能为空
• 群号：留空的规则会照常保存，但不会在任何群生效，等以后填上群号再用
时间：持续活跃 / 活跃间隔 / 唤醒冷却（-1 继承兜底；兜底规则自身没有下层可继承，只能填 ≥ 0）
      活跃提示词 / 活跃方式 / 活跃范围
指令：指令黑白名单 / 禁用系统指令 / 禁前唤醒 / 指令不唤醒
其他：用户黑白名单 / 管理员豁免

—— 复制规则 ——
• 面板左侧每条规则右下角的「复制」：设置与条件照抄成一条新规则，
  群号与备注会清空（这两项是每条规则专属的），填好备注就能用
• 兜底规则同样能复制：复制出来是一条普通规则，追加到列表末尾

—— 提示词 ——
• 两处位置可填：条件提示词、持续活跃提示词
• 统一规则：留空 = 不注入（不再回落），想要默认文案点「恢复默认」
• 每条提示词都有「临时」开关：开 = 只对本次请求生效（不写入对话历史），关 = 持久写入
• 全局设置里那 3 条只是「恢复默认」的模板，自己不参与注入；
  想整体关掉注入用全局的「提示词注入」开关

—— 概率唤醒 ——
• 概率：0~1，例如 0.05 表示 5%
• 概率冷却：命中后 N 分钟内不再判断这条概率条件（0 = 不冷却），
  用于群消息频繁时避免反复被动唤醒

—— 黑白名单语法（用户黑白名单、指令黑白名单通用）——
• 只有一个特殊值 `*`，不支持 all、0所有 等写法
• `*` 放行全部　`/xxx` 禁止　`xxx` 允许
• 例：`*`、`/123456`  放行所有人但禁止 123456
• 例：`123456`、`234567` 只允许这两个人
• 名单非空时必须显式写 `*` 才会「其余全部放行」

—— 正则 ——
• 唤醒正则 / 屏蔽正则都匹配「完整消息文本」：
  文本里的 `@昵称(QQ号)`、`@昵称(myself)`、`@昵称(all)` 都会保留
• 默认 search 语义（包含即命中），前缀用 `^`，整句用 `^...$`
• 以 `#` 开头的行视为注释

—— 兜底规则 ——
未在自定义规则中出现的群，使用「兜底规则」。兜底规则默认关闭。
""".strip()


class 群唤醒增强Pro(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.context = context
        self.启动时间 = time.time()

        self.数据目录 = self._解析数据目录()
        self.存储 = 数据存储(self.数据目录)
        self.数据: dict[str, Any] = self.存储.加载()
        self.状态 = 运行状态()
        self.引擎 = 规则引擎(self.状态, self._兜底, self._全局)
        self._锁 = asyncio.Lock()

        # 规则索引：群号 -> 规则对象（直接持有列表元素，避免二次查表）
        self.规则索引: dict[str, dict[str, Any]] = {}
        self.通配规则: list[dict[str, Any]] = []
        self.所有指令: list[str] = []
        self.所有指令集合: set[str] = set()
        self.指令前缀: tuple[str, ...] = ("/",)

        self._群目录脏 = False
        self._群目录最后落盘 = 0.0
        self._最后清理 = 0.0

        self._刷新指令表()
        self._读取指令前缀()
        self._重建索引()
        self._注册面板接口()

        logger.info(
            f"【{插件名}】已加载：{len(self.数据['规则列表'])} 条自定义规则，"
            f"{len(self.规则索引)} 个群已绑定，{len(self.所有指令)} 个指令。"
        )
        if self.存储.最后错误:
            logger.warning(f"【{插件名}】数据文件读取失败并已自愈：{self.存储.最后错误}")

    # ------------------------------------------------------------------ 初始化

    def _解析数据目录(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(插件名))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"【{插件名}】无法获取插件数据目录，改用插件目录：{e}")
            return Path(__file__).resolve().parent / "data"

    def _读取指令前缀(self) -> None:
        try:
            前缀 = self.context.get_config()["wake_prefix"]
            if isinstance(前缀, str):
                前缀 = [前缀]
            self.指令前缀 = tuple(str(i) for i in 前缀 if str(i))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"【{插件名}】读取唤醒前缀失败，使用默认 '/'：{e}")
            self.指令前缀 = ("/",)

    def _刷新指令表(self) -> None:
        额外 = (self.数据.get("全局") or {}).get("额外指令") or []
        self.所有指令 = 指令工具.扫描指令(额外)
        self.所有指令集合 = set(self.所有指令)

    def _重建索引(self) -> None:
        """重建「群号 -> 规则」索引。

        顺序即优先级：同一群号出现在多条规则里时，只有列表里最靠前的那条
        会进入索引，后面的会被忽略（面板上可用 ↑↓ 调整顺序）。
        """
        规则列表 = self.数据.get("规则列表") or []

        self.规则索引 = {}
        self.通配规则 = []
        冲突: dict[str, str] = {}

        for 规则 in 规则列表:
            if not isinstance(规则, dict):
                continue
            for 群号 in 规则.get("群号") or []:
                群号 = str(群号).strip()
                if not 群号:
                    continue
                if 群号 == "*":
                    self.通配规则.append(规则)
                    continue
                if 群号 in self.规则索引:
                    冲突[群号] = 规则.get("备注", "")
                    continue
                self.规则索引[群号] = 规则

        for 群号, 备注 in 冲突.items():
            生效 = self.规则索引.get(群号, {}).get("备注", "")
            logger.warning(
                f"【{插件名}】群 {群号} 同时出现在多条规则中，"
                f"按顺序使用「{生效}」，忽略「{备注}」（可在面板调整规则顺序）"
            )

        # 兜底规则永远排在通配规则最后
        self.通配规则.append(self.数据.get("兜底规则") or {})

    # ------------------------------------------------------------------ 存取

    def _全局(self) -> dict[str, Any]:
        return self.数据.get("全局") or {}

    def _兜底(self) -> dict[str, Any]:
        return self.数据.get("兜底规则") or {}

    def _取规则(self, 群号: str) -> dict[str, Any] | None:
        """群号 -> 生效规则；没有自定义规则时回落到通配/兜底。"""
        if not 群号:
            return None
        规则 = self.规则索引.get(str(群号))
        if 规则 is not None:
            return 规则
        for 规则 in self.通配规则:
            if 规则 is self._兜底() or 规则.get("开关", True):
                return 规则
        return None

    async def _保存(self) -> None:
        self.数据 = models.规范化数据(self.数据)
        self._重建索引()
        self._刷新指令表()
        try:
            # 落盘用快照，避免写线程与事件循环里的修改互相踩踏
            await asyncio.to_thread(self.存储.保存, copy.deepcopy(self.数据))
        except Exception as e:  # noqa: BLE001
            logger.error(f"【{插件名}】保存数据失败：{e}", exc_info=True)

    async def _保存群目录(self, 强制: bool = False) -> None:
        现在 = time.time()
        if not self._群目录脏:
            return
        if not 强制 and 现在 - self._群目录最后落盘 < 60:
            return
        self.数据["群目录"] = dict(self.状态.群目录)
        self._群目录脏 = False
        self._群目录最后落盘 = 现在
        try:
            await asyncio.to_thread(self.存储.保存, copy.deepcopy(self.数据))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"【{插件名}】保存群目录失败：{e}")

    # ------------------------------------------------------------------ 上下文

    def _构建上下文(self, event: AstrMessageEvent, *, 快照=None) -> 判定上下文:
        快照 = 快照 or 构建快照(event)
        return 判定上下文(
            群号=str(event.get_group_id() or ""),
            发送者=str(event.get_sender_id() or ""),
            发送者名=event.get_sender_name() or "",
            管理员=bool(event.is_admin()),
            快照=快照,
            现在=time.time(),
            所有指令=self.所有指令集合,
            指令前缀=self.指令前缀,
            平台=event.get_platform_name() or "",
        )

    def _记录群(self, ctx: 判定上下文, event: AstrMessageEvent) -> None:
        群对象 = getattr(event.message_obj, "group", None)
        名称 = ""
        if 群对象 is not None:
            名称 = str(getattr(群对象, "group_name", "") or "")
        if self.状态.记录群(ctx.群号, 名称, ctx.现在):
            self._群目录脏 = True

    def _周期性清理(self, 现在: float) -> None:
        if 现在 - self._最后清理 < 120:
            return
        self._最后清理 = 现在
        self.状态.清理(现在)

    # ------------------------------------------------------------------ 主入口

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=入口优先级)
    async def 消息入口(self, event: AstrMessageEvent) -> None:
        try:
            群号 = str(event.get_group_id() or "")
            if not 群号:
                return

            # 先做零成本的判断：群黑名单 / 没规则 / 规则关闭，直接返回，
            # 不构建消息快照（构建要遍历整条消息链，是这里最贵的一步）。
            全局 = self._全局()
            if 群号 in (全局.get("黑名单群聊") or []):
                logger.info(f"【{插件名}】群 {群号} 在黑名单群聊中，直接屏蔽")
                event.stop_event()
                return

            规则 = self._取规则(群号)
            if 规则 is None:
                return
            if not 规则.get("开关", True):
                logger.debug(f"【{插件名}】群 {群号} 规则「{规则.get('备注')}」已关闭，跳过")
                return

            快照 = 构建快照(event)

            # 空消息链不再单独短路：交给流水线里的「屏蔽空消息链」条件判断
            ctx = self._构建上下文(event, 快照=快照)
            self._记录群(ctx, event)
            self._周期性清理(ctx.现在)

            结果 = self.引擎.判定(ctx, 规则)
            self.引擎.应用(ctx, 结果, event)

            if 结果.动作 in (动作.WAKE, 动作.STOP):
                logger.info(
                    f"【{插件名}】群 {群号} 用户「{ctx.发送者名}({ctx.发送者})」"
                    f"消息 | {event.get_message_outline()} | {结果.描述()}"
                )
            else:
                # 未命中任何条件这类明细只打 debug，需要就把日志级别调成 DEBUG
                logger.debug(f"【{插件名}】群 {群号} {结果.描述()}")

            await self._保存群目录()

        except Exception as e:  # noqa: BLE001
            logger.error(f"【{插件名}】消息处理出错：{e}", exc_info=True)

    # ------------------------------------------------------------------ 钩子

    @filter.on_waiting_llm_request()
    async def 进入LLM前(self, event: AstrMessageEvent) -> None:
        """进入 LLM 流程前的最后一道闸门。

        有些场景（别的插件重新唤醒、指令处理器没有发消息）会让本不该进入 LLM 的
        事件继续走到这里，因此再判一次；随后再跑 `LLM前条件` 流水线。

        这一层刻意不加优先级，用框架默认值，保证和其他插件的等待钩子同一批次执行。
        """
        try:
            群号 = str(event.get_group_id() or "")
            if not 群号:
                return
            规则 = self._取规则(群号)
            if 规则 is None or not 规则.get("开关", True):
                return

            ctx = self._构建上下文(event)
            if ctx.管理员 and 规则.get("管理员豁免", True):
                return

            # 禁前唤醒只拦「进入 LLM」这一步，不终止事件：别的插件照常能看到这条消息
            if 规则.get("禁前唤醒") and ctx.使用前缀 and not ctx.是指令:
                logger.info(f"【{插件名}】群 {群号} 禁止前缀唤醒：{ctx.快照.指令文本}")
                event.stop_event()
                return

            if 规则.get("指令不唤醒") and ctx.是指令:
                logger.info(f"【{插件名}】群 {群号} 指令触发后不唤醒 LLM：{ctx.快照.指令文本}")
                event.stop_event()
                return

            名单 = 规则.get("指令黑白名单")
            if acl.名单启用(名单) and ctx.指令候选:
                if not any(acl.名单通过(项, 名单) for 项 in ctx.指令候选):
                    logger.info(f"【{插件名}】群 {群号} 指令黑白名单屏蔽：{ctx.快照.指令文本}")
                    event.stop_event()
                    return

            if acl.名单启用(规则.get("用户黑白名单")) and not acl.名单通过(
                ctx.发送者, 规则.get("用户黑白名单")
            ):
                logger.info(f"【{插件名}】群 {群号} 用户黑白名单屏蔽：{ctx.发送者}")
                event.stop_event()
                return

            # LLM 请求前流水线：只做屏蔽，命中就终止这一轮回复
            结果 = self.引擎.判定LLM前(ctx, 规则)
            if 结果 is not None:
                event.set_extra("qwb_屏蔽原因", 结果.原因)
                logger.info(f"【{插件名}】群 {群号} LLM 前流水线 | {结果.描述()}")
                event.stop_event()
        except Exception as e:  # noqa: BLE001
            logger.error(f"【{插件名}】LLM 前置检查出错：{e}", exc_info=True)

    @filter.on_llm_request()
    async def 注入提示词(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """把一段 <system_reminder> 挂到本轮请求最前面。

        所有提示词位置一律「留空 = 不注入」，规则与条件之间没有回落：
        条件命中时用条件自己那段，活跃窗口这类没有命中条件的唤醒用规则里的
        「持续活跃提示词」。全局那 3 条只是「恢复默认」的模板。
        """
        try:
            全局 = self._全局()
            if not 全局.get("提示词注入", True):
                return
            群号 = str(event.get_group_id() or "")
            规则 = self._取规则(群号) or {}
            来源 = str(event.get_extra("qwb_唤醒来源", "") or "")
            命中条件 = bool(event.get_extra("qwb_有命中条件"))

            提示词 = str(event.get_extra("qwb_唤醒提示词", "") or "").strip()
            临时 = bool(event.get_extra("qwb_提示词临时", True))
            if not 提示词 and not 命中条件:
                # 没有命中条件的唤醒只有活跃窗口会注入；
                # 其它来源（别的插件重新唤醒等）一律不注入，免得摸不清什么时候冒出来
                if "活跃" in 来源:
                    规则键 = "活跃提示词"
                    提示词 = str(规则.get(规则键) or "").strip()
                    if 提示词:
                        临时 = bool(规则.get(f"{规则键}临时", True))
            if not 提示词:
                return

            部件 = TextPart(text=f"<system_reminder>{提示词}</system_reminder>")
            if 临时:
                部件.mark_as_temp()
            req.leading_user_content_parts.append(部件)
            event.set_extra("qwb_已注入提示词", True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"【{插件名}】注入提示词出错：{e}", exc_info=True)

    @filter.on_llm_response()
    async def LLM响应后(self, event: AstrMessageEvent, _resp: Any = None) -> None:
        try:
            event.set_extra("qwb_llm已请求", True)
            群号 = str(event.get_group_id() or "")
            规则 = self._取规则(群号)
            if 规则 is None or not 规则.get("开关", True):
                return
            if 规则.get("活跃方式") == "llm请求后":
                ctx = self._构建上下文(event)
                self.引擎.记录活跃(ctx.群号, ctx.发送者, 规则, ctx.现在)
        except Exception as e:  # noqa: BLE001
            logger.error(f"【{插件名}】LLM 响应后处理出错：{e}", exc_info=True)

    @filter.after_message_sent()
    async def 发送消息后(self, event: AstrMessageEvent) -> None:
        try:
            if not event.get_extra("qwb_llm已请求"):
                return
            if event.get_extra("qwb_活跃已记录"):
                return
            event.set_extra("qwb_活跃已记录", True)
            群号 = str(event.get_group_id() or "")
            规则 = self._取规则(群号)
            if 规则 is None or not 规则.get("开关", True):
                return
            if 规则.get("活跃方式") == "发送消息后":
                ctx = self._构建上下文(event)
                self.引擎.记录活跃(ctx.群号, ctx.发送者, 规则, ctx.现在)
        except Exception as e:  # noqa: BLE001
            logger.error(f"【{插件名}】发送消息后处理出错：{e}", exc_info=True)

    @filter.on_astrbot_loaded()
    async def 框架加载完成(self) -> None:
        self._刷新指令表()
        logger.info(f"【{插件名}】指令表已刷新，共 {len(self.所有指令)} 个指令")

    async def terminate(self) -> None:
        """插件卸载时由框架调用：把还没落盘的群目录写下去。"""
        try:
            await self._保存群目录(强制=True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"【{插件名}】卸载时保存群目录失败：{e}")

    # ------------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="terminate")
    async def terminate_tool(self, event: AstrMessageEvent, reason: str = "") -> None:
        """结束/停止本轮对话，适用于工具使用后无需继续、无需回复（不回复，不理）用户消息，使用后静默终止本轮对话

        Args:
            reason(string): 可选，原因，原因记录在工具调用记录上下文，不会发送到聊天
        """
        logger.info(f"【{插件名}】LLM 主动终止本轮对话：{reason or '未说明原因'}")
        event.stop_event()
        return None

    # ------------------------------------------------------------------ 面板接口

    def _注册面板接口(self) -> None:
        注册 = getattr(self.context, "register_web_api", None)
        if not callable(注册):
            logger.warning(f"【{插件名}】当前框架不支持插件 Web 接口，规则面板可能无法使用")
            return

        路由 = (
            ("api/overview", self.api_overview, ["GET"], "规则面板数据"),
            ("api/rule/save", self.api_rule_save, ["POST"], "保存规则"),
            ("api/rule/delete", self.api_rule_delete, ["POST"], "删除规则"),
            ("api/rule/new", self.api_rule_new, ["POST"], "新建规则"),
            ("api/rule/duplicate", self.api_rule_duplicate, ["POST"], "复制规则"),
            ("api/rule/move", self.api_rule_move, ["POST"], "调整规则顺序"),
            ("api/condition/new", self.api_condition_new, ["POST"], "新建条件"),
            ("api/settings/save", self.api_settings_save, ["POST"], "保存全局设置"),
            ("api/test", self.api_test, ["POST"], "正则测试"),
            ("api/state/clear", self.api_state_clear, ["POST"], "清理运行时状态"),
            ("api/commands/refresh", self.api_commands_refresh, ["POST"], "刷新指令表"),
        )
        for 端点, 处理函数, 方法, 描述 in 路由:
            路径 = f"/{插件名}/{端点}"
            try:
                注册(路径, 处理函数, 方法, 描述)
            except TypeError:
                注册(路径, 处理函数, 方法)
        logger.info(f"【{插件名}】面板接口已注册 {len(路由)} 条")

    @staticmethod
    async def _读取请求体() -> dict[str, Any]:
        if _quart_request is None:
            return {}
        try:
            body = await _quart_request.get_json(silent=True)
        except Exception:  # noqa: BLE001
            body = None
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _成功(数据: Any = None) -> dict[str, Any]:
        return {"status": "ok", "data": 数据 if 数据 is not None else {}}

    @staticmethod
    def _失败(消息: str) -> dict[str, Any]:
        return {"status": "error", "message": 消息}

    def _面板数据(self) -> dict[str, Any]:
        return {
            "规则列表": self.数据.get("规则列表") or [],
            "兜底规则": self.数据.get("兜底规则") or {},
            "全局": self._全局(),
            "群目录": sorted(
                (
                    {"群号": 群号, "名称": 值.get("名称", ""), "时间": 值.get("时间", 0)}
                    for 群号, 值 in (self.数据.get("群目录") or {}).items()
                ),
                key=lambda i: -float(i.get("时间") or 0),
            ),
            "指令": self.所有指令,
            "系统指令": sorted(指令工具.系统指令),
            "条件类型": [
                {
                    "类型": 名称,
                    "动作": 值["动作"],
                    "阶段": list(值["阶段"]),
                    "说明": 值["说明"],
                    "参数": 值["参数"],
                }
                for 名称, 值 in models.条件类型.items()
            ],
            "LLM前条件类型": list(models.LLM前条件类型),
            "默认提示词": dict(models.默认提示词),
            "选项": {
                "概率方式": list(models.概率方式可选),
                "活跃方式": list(models.活跃方式可选),
                "活跃范围": list(models.活跃范围可选),
                "默认定时": {
                    "唤醒冷却": self._兜底().get("唤醒冷却", 0),
                    "持续活跃": self._兜底().get("持续活跃", 0),
                    "活跃间隔": self._兜底().get("活跃间隔", 0),
                },
            },
            "运行时": {
                "活跃": self.状态.活跃快照(time.time()),
                "已绑定群": sorted(self.规则索引.keys()),
                "指令前缀": list(self.指令前缀),
                "运行时长": round(time.time() - self.启动时间, 0),
                "数据目录": str(self.数据目录),
            },
        }

    async def api_overview(self) -> dict[str, Any]:
        return self._成功(self._面板数据())

    async def api_rule_save(self) -> dict[str, Any]:
        body = await self._读取请求体()
        原始 = body.get("规则")
        if not isinstance(原始, dict):
            return self._失败("缺少规则数据")

        规则 = models.规范化规则(原始)
        if 规则["id"] == "fallback":
            return self._失败("规则 ID 非法")
        # 备注是规则的唯一标识，必填；群号可以留空——留空的规则不会进入群号索引，
        # 也就不会在任何群生效，但会照样存着，等以后填上群号再用
        if not 规则["备注"].strip():
            return self._失败("请填写备注（备注用来区分每条规则）")

        async with self._锁:
            列表 = self.数据.setdefault("规则列表", [])
            找到 = False
            for 位置, 现有 in enumerate(列表):
                if 现有.get("id") == 规则["id"]:
                    列表[位置] = 规则
                    找到 = True
                    break
            if not 找到:
                列表.append(规则)
            await self._保存()
        logger.info(f"【{插件名}】已保存规则：{models.规则摘要(规则)}")
        return self._成功({"规则": 规则})

    async def api_rule_new(self) -> dict[str, Any]:
        body = await self._读取请求体()
        群号 = body.get("群号") or []
        if isinstance(群号, str):
            群号 = [i.strip() for i in 群号.replace(",", "\n").split("\n") if i.strip()]
        备注 = str(body.get("备注") or "").strip()
        if not 备注:
            return self._失败("请填写备注（备注用来区分每条规则）")

        async with self._锁:
            现有 = self.数据.setdefault("规则列表", [])
            新 = models.新规则(群号=list(群号), 备注=备注)
            现有.append(新)
            await self._保存()
        return self._成功({"规则": 新})

    async def api_rule_duplicate(self) -> dict[str, Any]:
        """复制一条规则：设置与条件照抄，群号与备注清空后另存为一条新规则。

        - 普通规则：新规则插在源规则后面；
        - 兜底规则：复制成一条**普通规则**追加到列表末尾（兜底本身不会被替换）。
        群号清空 → 不进入群号索引 → 在任何群都不生效，但记录会保留，下次填上群号就能直接用。
        """
        body = await self._读取请求体()
        规则ID = str(body.get("id") or "").strip()
        if not 规则ID:
            return self._失败("缺少规则 ID")

        async with self._锁:
            列表 = self.数据.setdefault("规则列表", [])
            if 规则ID == "fallback":
                源名称 = "兜底规则"
                副本 = models.复制规则(self._兜底())
                列表.append(副本)
            else:
                位置 = next((i for i, r in enumerate(列表) if r.get("id") == 规则ID), -1)
                if 位置 < 0:
                    return self._失败("未找到该规则")
                源名称 = models.规则名称(列表[位置])
                副本 = models.复制规则(列表[位置])
                列表.insert(位置 + 1, 副本)
            await self._保存()
        logger.info(
            f"【{插件名}】已复制规则「{源名称}」→ {副本['id']}"
            f"（群号与备注已清空，暂不生效）"
        )
        return self._成功({"规则": 副本, "来源": 规则ID})

    async def api_rule_move(self) -> dict[str, Any]:
        """上下移动规则：顺序即优先级，越靠前越优先。"""
        body = await self._读取请求体()
        规则ID = str(body.get("id") or "").strip()
        try:
            方向 = int(body.get("方向", body.get("direction", 0)))
        except (TypeError, ValueError):
            方向 = 0
        if not 规则ID:
            return self._失败("缺少规则 ID")
        if 方向 == 0:
            return self._失败("缺少移动方向（-1 上移，1 下移）")
        方向 = -1 if 方向 < 0 else 1

        async with self._锁:
            列表 = self.数据.setdefault("规则列表", [])
            位置 = next((i for i, r in enumerate(列表) if r.get("id") == 规则ID), -1)
            if 位置 < 0:
                return self._失败("未找到该规则")
            目标 = 位置 + 方向
            if 目标 < 0 or 目标 >= len(列表):
                return self._失败("已经到顶了" if 方向 < 0 else "已经到底了")
            列表[位置], 列表[目标] = 列表[目标], 列表[位置]
            await self._保存()
            顺序 = [r.get("id") for r in 列表]
        return self._成功({"顺序": 顺序})

    async def api_rule_delete(self) -> dict[str, Any]:
        body = await self._读取请求体()
        规则ID = str(body.get("id") or "").strip()
        if not 规则ID:
            return self._失败("缺少规则 ID")
        if 规则ID == "fallback":
            return self._失败("兜底规则不可删除")

        async with self._锁:
            列表 = self.数据.get("规则列表") or []
            新列表 = [r for r in 列表 if r.get("id") != 规则ID]
            if len(新列表) == len(列表):
                return self._失败("未找到该规则")
            self.数据["规则列表"] = 新列表
            await self._保存()
        logger.info(f"【{插件名}】已删除规则 {规则ID}")
        return self._成功({"id": 规则ID})

    async def api_condition_new(self) -> dict[str, Any]:
        body = await self._读取请求体()
        类型 = str(body.get("类型") or "艾特唤醒")
        if 类型 not in models.条件类型:
            return self._失败(f"未知条件类型：{类型}")
        if str(body.get("阶段") or "接收").strip().lower() in ("llm", "llm前"):
            if 类型 not in models.LLM前条件类型:
                return self._失败(f"「{类型}」是唤醒类条件，只能放在接收消息流水线里")
        条件 = models.新条件(
            类型,
            提示词=str(body.get("提示词") or "").strip(),
        )
        return self._成功({"条件": 条件})

    async def api_settings_save(self) -> dict[str, Any]:
        body = await self._读取请求体()
        async with self._锁:
            if isinstance(body.get("兜底规则"), dict):
                self.数据["兜底规则"] = models.规范化兜底(
                    {**(self.数据.get("兜底规则") or {}), **body["兜底规则"]}
                )
            if isinstance(body.get("全局"), dict):
                self.数据["全局"] = models.规范化全局(
                    {**(self.数据.get("全局") or {}), **body["全局"]}
                )
            await self._保存()
        return self._成功({"兜底规则": self._兜底(), "全局": self._全局()})

    async def api_test(self) -> dict[str, Any]:
        body = await self._读取请求体()
        文本 = str(body.get("文本") or "")
        截断 = 2000
        if len(文本) > 截断:
            文本 = 文本[:截断]

        结果: dict[str, Any] = {"文本": 文本}
        for 键 in ("唤醒正则", "屏蔽正则"):
            命中 = patterns.匹配详情(body.get(键) or [], 文本)
            结果[键] = {"命中": bool(命中), "模式": 命中[0] if 命中 else "", "片段": 命中[1] if 命中 else ""}

        名单 = body.get("名单")
        if isinstance(名单, list):
            值 = str(body.get("名单值") or "").strip()
            结果["名单"] = {"通过": acl.名单通过(值, 名单), "描述": acl.名单描述(名单)}

        return self._成功(结果)

    async def api_state_clear(self) -> dict[str, Any]:
        self.状态.清理(time.time() + 86400 * 365)
        return self._成功({"消息": "运行时状态已清空"})

    async def api_commands_refresh(self) -> dict[str, Any]:
        self._刷新指令表()
        return self._成功({"指令": self.所有指令})

    # ------------------------------------------------------------------ 指令

    @filter.command("群规帮助")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_帮助(self, event: AstrMessageEvent):
        """查看群唤醒增强 Pro 的帮助"""
        yield event.plain_result(帮助文本)

    @filter.command("群规则")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_查看(self, event: AstrMessageEvent, 群号: str = None):
        """查看某群生效的规则：/群规则 [群号]"""
        群号 = str(群号).strip() if 群号 else str(event.get_group_id() or "")
        if not 群号:
            yield event.plain_result("❌ 请在群聊中使用，或传入群号")
            return
        规则 = self._取规则(群号)
        if 规则 is None:
            yield event.plain_result(f"❌ 群 {群号} 没有可用规则（兜底规则也不存在）")
            return
        来源 = "自定义规则" if 群号 in self.规则索引 else "兜底/通配规则"
        yield event.plain_result(self._规则文本(群号, 规则, 来源))

    @filter.command("规则列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_列表(self, event: AstrMessageEvent):
        """列出全部规则"""
        列表 = self.数据.get("规则列表") or []
        行 = [f"📋 共 {len(列表)} 条自定义规则（从上到下依次优先）"]
        for 序号, 规则 in enumerate(列表, 1):
            状态 = "✅" if 规则.get("开关") else "❌"
            群号 = "、".join(规则.get("群号") or []) or "无"
            行.append(
                f"{序号}. {状态} {models.规则名称(规则)}｜群：{群号}"
                f"｜条件 {len(规则.get('条件') or [])} 条｜id：{规则.get('id', '')}"
            )
        兜底 = self._兜底()
        行.append(
            f"\n🪂 兜底规则：{'开启' if 兜底.get('开关') else '关闭'}"
            f"｜持续活跃 {self.引擎.决议(兜底, '持续活跃'):g}s"
            f"｜活跃间隔 {self.引擎.决议(兜底, '活跃间隔'):g}s"
        )
        yield event.plain_result("\n".join(行))

    @filter.command("添加群规")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_添加(self, event: AstrMessageEvent, 群号: str = None, 备注: str = None):
        """新建规则：/添加群规 <群号>[,群号] [备注]"""
        群号 = str(群号).strip() if 群号 else str(event.get_group_id() or "")
        群号列表 = [i.strip() for i in 群号.replace("，", ",").split(",") if i.strip()]
        if not 群号列表:
            yield event.plain_result("❌ 请提供有效的群号")
            return

        已存在 = [i for i in 群号列表 if i in self.规则索引]
        if 已存在:
            yield event.plain_result(
                f"❌ 群 {('、'.join(已存在))} 已有规则（{models.规则名称(self.规则索引[已存在[0]])}），"
                f"请改用 /设置群规 修改，或在面板里调整规则顺序"
            )
            return

        说明 = 备注 or (f"群 {群号列表[0]}" if len(群号列表) == 1 else "共享规则")
        规则 = models.新规则(群号列表, 说明)
        self.数据.setdefault("规则列表", []).append(规则)
        await self._保存()
        yield event.plain_result(
            f"✅ 已创建规则「{规则['备注']}」（id {规则['id']}）\n"
            f"群号：{('、'.join(群号列表))}\n"
            f"默认条件：艾特唤醒 → 引用唤醒（从上到下判断）\n"
            f"用 /添加条件 追加，或在 WebUI 规则面板里编辑。"
        )

    @filter.command("设置群规")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_设置(self, event: AstrMessageEvent, 键: str, 值: str, 目标: str = None, 操作: str = None):
        """修改规则设置项：/设置群规 <键> <值> [群号或备注] [add|del|rep]"""
        键 = str(键).strip()
        值 = str(值).strip()

        # 操作参数：显式传 add/del/rep，或直接把操作写在「目标」位置
        实际操作 = str(操作).strip().lower() if 操作 else ""
        目标文本 = str(目标).strip() if 目标 else ""
        if not 实际操作 and 目标文本.lower() in ("add", "del", "rep"):
            实际操作 = 目标文本.lower()
            目标文本 = ""

        if 键 in ("条件", "LLM前条件"):
            yield event.plain_result(
                "❌ 条件请用 /添加条件、/设置条件、/删除条件 管理"
                "（LLM 请求前流水线加 llm: 前缀，或用 L 开头的序号）"
            )
            return
        if 键 not in models.默认规则:
            可用 = "、".join(k for k in models.默认规则 if k not in ("id", "条件", "LLM前条件"))
            yield event.plain_result(f"❌ 不存在的字段「{键}」\n可用字段：\n{可用}")
            return
        if 键 == "id":
            yield event.plain_result("❌ id 由系统维护，不能手动修改")
            return

        规则, 错误 = self._查找规则(目标文本, event)
        if 规则 is None:
            yield event.plain_result(错误)
            return

        原值 = 规则.get(键)
        原类型 = type(原值)

        try:
            新值 = self._转换值(键, 值, 原值, 原类型, 实际操作)
            # 兜底规则没有更下层可继承，-1 对它没有意义
            if (
                规则.get("id") == "fallback"
                and 键 in models.时间字段
                and isinstance(新值, (int, float))
                and 新值 < 0
            ):
                raise ValueError("兜底规则没有更下层可继承，时间项不能填 -1")
        except ValueError as e:
            yield event.plain_result(f"⚠️ {e}")
            return

        规则[键] = 新值
        await self._保存()

        行 = [f"✅ 已更新规则「{models.规则名称(规则)}」：{键} = {self._展示值(新值)}"]
        if 实际操作 in ("add", "del", "rep"):
            行.append(f"• 列表操作：{实际操作}")
        if 键 in ("群号", "开关"):
            行.append("• 规则索引已重建，立即生效")
        if 键 == "群号" and not 新值:
            行.append("• ⚠️ 群号已清空：这条规则会保存着，但不会在任何群生效")
        if not 规则.get("开关") and 键 != "开关":
            行.append("• ⚠️ 该规则当前已关闭")
        yield event.plain_result("\n".join(行))

    @filter.command("删除群规")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_删除群(self, event: AstrMessageEvent, 群号: str = None):
        """把群号从规则中移除"""
        群号 = str(群号).strip() if 群号 else str(event.get_group_id() or "")
        if not 群号:
            yield event.plain_result("❌ 请在群聊中使用，或传入群号")
            return

        规则 = self.规则索引.get(群号)
        if 规则 is None:
            yield event.plain_result(f"❌ 群 {群号} 没有自定义规则")
            return

        剩余 = [i for i in (规则.get("群号") or []) if i != 群号]
        if 剩余:
            规则["群号"] = 剩余
            await self._保存()
            yield event.plain_result(
                f"✅ 已把群 {群号} 从规则「{models.规则名称(规则)}」移除，"
                f"剩余群号：{'、'.join(剩余)}"
            )
            return

        列表 = self.数据.get("规则列表") or []
        self.数据["规则列表"] = [r for r in 列表 if r.get("id") != 规则.get("id")]
        await self._保存()
        yield event.plain_result(
            f"✅ 群 {群号} 是规则「{models.规则名称(规则)}」的最后一个群号，该规则已一并删除"
        )

    @filter.command("删除规则")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_删除规则(self, event: AstrMessageEvent, 目标: str = None):
        """按备注或 id 删除整条规则"""
        目标 = str(目标).strip() if 目标 else ""
        if not 目标:
            yield event.plain_result("❌ 用法：/删除规则 <备注或ID>")
            return
        if 目标 == "兜底规则":
            yield event.plain_result("❌ 兜底规则不可删除，可在面板里关闭它")
            return

        列表 = self.数据.get("规则列表") or []
        命中 = [r for r in 列表 if r.get("id") == 目标 or r.get("备注") == 目标]
        if not 命中:
            yield event.plain_result(f"❌ 未找到备注或 id 为「{目标}」的规则")
            return
        命中ID = {r.get("id") for r in 命中}
        self.数据["规则列表"] = [r for r in 列表 if r.get("id") not in 命中ID]
        await self._保存()
        yield event.plain_result(f"✅ 已删除 {len(命中)} 条规则：{models.规则名称(命中[0])}")

    @filter.command("移动规则")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_移动规则(self, event: AstrMessageEvent, 目标: str = None, 方向: str = None):
        """调整规则顺序：/移动规则 <备注或ID> <上|下>"""
        目标 = str(目标).strip() if 目标 else ""
        方向文本 = str(方向 or "").strip().lower()
        if not 目标 or not 方向文本:
            yield event.plain_result("❌ 用法：/移动规则 <备注或ID> <上|下>")
            return

        列表 = self.数据.get("规则列表") or []
        位置 = next(
            (i for i, r in enumerate(列表) if 目标 in (r.get("备注"), r.get("id"))),
            -1,
        )
        if 位置 < 0:
            yield event.plain_result(f"❌ 未找到备注或 id 为「{目标}」的规则")
            return

        if 方向文本 in ("上", "up", "↑", "-1"):
            目标位置 = 位置 - 1
        elif 方向文本 in ("下", "down", "↓", "1"):
            目标位置 = 位置 + 1
        else:
            yield event.plain_result("❌ 方向只支持 上 / 下")
            return
        if not (0 <= 目标位置 < len(列表)):
            yield event.plain_result("⚠️ 已经在最前/最后，无法继续移动")
            return

        列表[位置], 列表[目标位置] = 列表[目标位置], 列表[位置]
        await self._保存()
        yield event.plain_result(self._规则顺序文本())

    def _规则顺序文本(self) -> str:
        列表 = self.数据.get("规则列表") or []
        行 = [f"📋 规则顺序（共 {len(列表)} 条，越靠前越优先）"]
        for 序号, 规则 in enumerate(列表, 1):
            状态 = "✅" if 规则.get("开关") else "❌"
            群号 = "、".join(规则.get("群号") or []) or "无"
            行.append(f"{序号}. {状态} {models.规则名称(规则)}｜群：{群号}")
        return "\n".join(行)

    # ------------------------------------------------------------------ 条件指令

    @filter.command("条件列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_条件列表(self, event: AstrMessageEvent, 群号: str = None):
        """按判断顺序列出条件：/条件列表 [群号]"""
        规则, 错误 = self._查找规则(群号, event)
        if 规则 is None:
            yield event.plain_result(错误)
            return
        yield event.plain_result(self._条件文本(规则))

    @filter.command("添加条件")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_添加条件(
        self,
        event: AstrMessageEvent,
        类型: str,
        提示词: str = None,
        群号: str = None,
    ):
        """新增条件：/添加条件 <类型> [提示词] [群号]
        类型前加 llm: 表示加到 LLM 请求前流水线，例如 llm:屏蔽正则
        """
        类型文本 = str(类型).strip()
        阶段 = "接收"
        for 前缀 in ("llm:", "LLM:", "l:", "L:"):
            if 类型文本.startswith(前缀):
                阶段 = "LLM"
                类型文本 = 类型文本[len(前缀) :].strip()
                break

        if 类型文本 not in models.条件类型:
            yield event.plain_result(
                f"❌ 未知条件类型「{类型文本}」\n唤醒：{唤醒类型}\n屏蔽：{屏蔽类型}"
            )
            return
        if 阶段 == "LLM" and 类型文本 not in models.LLM前条件类型:
            yield event.plain_result(
                f"❌ 「{类型文本}」是唤醒类条件，只能放在接收消息流水线里\n"
                f"LLM 请求前只接受屏蔽类条件（以及 @ / 引用三类唤醒）：{LLM前类型}"
            )
            return

        规则, 错误 = self._查找规则(群号, event)
        if 规则 is None:
            yield event.plain_result(错误)
            return

        键 = "LLM前条件" if 阶段 == "LLM" else "条件"
        条件列表 = 规则.setdefault(键, [])
        条件 = models.新条件(类型文本, 提示词=str(提示词 or ""))
        条件列表.append(条件)
        await self._保存()
        位置 = f"L{len(条件列表)}" if 阶段 == "LLM" else f"第 {len(条件列表)} 条"
        阶段名 = "LLM 请求前" if 阶段 == "LLM" else "接收消息"
        yield event.plain_result(
            f"✅ 已为「{models.规则名称(规则)}」的{阶段名}流水线添加条件 {类型文本}"
            f"（{位置}，最后判断）\n"
            f"提示词：{条件['提示词'] or '（未设置，使用规则/全局默认）'}\n"
            f"要调顺序请用 /移动条件 <序号> 上|下"
        )

    @filter.command("删除条件")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_删除条件(self, event: AstrMessageEvent, 序号: str, 群号: str = None):
        """删除条件：/删除条件 <序号|L序号> [群号]"""
        规则, 错误 = self._查找规则(群号, event)
        if 规则 is None:
            yield event.plain_result(错误)
            return

        try:
            阶段, 条件列表, 位置 = self._解析条件目标(规则, 序号)
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return
        阶段名 = "LLM 请求前" if 阶段 == "LLM" else "接收消息"
        if not (1 <= 位置 <= len(条件列表)):
            yield event.plain_result(f"❌ 序号超出范围（{阶段名}流水线共 {len(条件列表)} 条）")
            return

        键 = "LLM前条件" if 阶段 == "LLM" else "条件"
        目标 = 条件列表[位置 - 1]
        规则[键] = [c for c in 条件列表 if c.get("id") != 目标.get("id")]
        await self._保存()
        标签 = f"L{位置}" if 阶段 == "LLM" else str(位置)
        yield event.plain_result(f"✅ 已删除 {标签} 条件：{目标.get('类型')}")

    @filter.command("移动条件")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_移动条件(
        self, event: AstrMessageEvent, 序号: str, 方向: str, 群号: str = None
    ):
        """调整条件顺序：/移动条件 <序号|L序号> <上|下> [群号]"""
        规则, 错误 = self._查找规则(群号, event)
        if 规则 is None:
            yield event.plain_result(错误)
            return

        try:
            阶段, 条件列表, 位置 = self._解析条件目标(规则, 序号)
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return
        阶段名 = "LLM 请求前" if 阶段 == "LLM" else "接收消息"
        if not (1 <= 位置 <= len(条件列表)):
            yield event.plain_result(f"❌ 序号超出范围（{阶段名}流水线共 {len(条件列表)} 条）")
            return

        方向文本 = str(方向 or "").strip().lower()
        if 方向文本 in ("上", "up", "↑", "-1"):
            目标位置 = 位置 - 2
        elif 方向文本 in ("下", "down", "↓", "1"):
            目标位置 = 位置
        else:
            yield event.plain_result("❌ 方向只支持 上 / 下")
            return
        if not (0 <= 目标位置 < len(条件列表)):
            yield event.plain_result("⚠️ 已经在最前/最后，无法继续移动")
            return

        条件列表[位置 - 1], 条件列表[目标位置] = 条件列表[目标位置], 条件列表[位置 - 1]
        规则["LLM前条件" if 阶段 == "LLM" else "条件"] = 条件列表
        await self._保存()
        yield event.plain_result(self._条件文本(规则))

    @filter.command("设置条件")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_设置条件(
        self, event: AstrMessageEvent, 序号: str, 字段: str, 值: str, 群号: str = None
    ):
        """修改条件：/设置条件 <序号|L序号> <字段> <值> [群号]
        字段：开关 / 提示词 / 正则 / 概率 / 概率方式 / 概率冷却
        """
        规则, 错误 = self._查找规则(群号, event)
        if 规则 is None:
            yield event.plain_result(错误)
            return

        try:
            阶段, 条件列表, 位置 = self._解析条件目标(规则, 序号)
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return
        阶段名 = "LLM 请求前" if 阶段 == "LLM" else "接收消息"
        if not (1 <= 位置 <= len(条件列表)):
            yield event.plain_result(f"❌ 序号超出范围（{阶段名}流水线共 {len(条件列表)} 条）")
            return

        条件 = 条件列表[位置 - 1]
        字段 = str(字段).strip()
        值 = str(值).strip()

        参数 = models.条件类型.get(条件.get("类型"), {}).get("参数", [])
        需要的参数 = {"正则": "正则", "概率": "概率", "概率方式": "概率", "概率冷却": "概率"}.get(字段)
        if 需要的参数 and 需要的参数 not in 参数:
            yield event.plain_result(
                f"❌ 「{条件.get('类型')}」没有「{字段}」这个参数\n"
                f"可用：开关 / 提示词" + (f" / {' / '.join(参数)}" if 参数 else "")
            )
            return

        try:
            if 字段 == "开关":
                if 值.lower() in ("开", "true", "1", "on", "是", "启用"):
                    条件["开关"] = True
                elif 值.lower() in ("关", "false", "0", "off", "否", "禁用"):
                    条件["开关"] = False
                else:
                    raise ValueError("开关只接受 开/关、on/off、1/0")
            elif 字段 == "提示词":
                条件["提示词"] = "" if 值.lower() in ("null", "none", "空") else 值
            elif 字段 == "正则":
                条件["正则"] = [] if 值.lower() in ("null", "none", "空") else [
                    i.strip() for i in 值.replace("，", ",").split(",") if i.strip()
                ]
            elif 字段 == "概率":
                条件["概率"] = max(0.0, min(1.0, float(值)))
            elif 字段 == "概率冷却":
                条件["概率冷却"] = max(0.0, min(10080.0, float(值.rstrip("分mM"))))
            elif 字段 == "概率方式":
                方式 = [i.strip() for i in 值.replace("，", ",").split(",") if i.strip()]
                非法 = [i for i in 方式 if i not in models.概率方式可选]
                if 非法:
                    raise ValueError(f"不支持的概率方式：{'、'.join(非法)}")
                条件["概率方式"] = 方式 or ["纯文本"]
            else:
                yield event.plain_result(
                    "❌ 字段只支持：开关 / 提示词 / 正则 / 概率 / 概率方式 / 概率冷却\n"
                    "（顺序请用 /移动条件）"
                )
                return
        except ValueError as e:
            yield event.plain_result(f"⚠️ {e}")
            return

        await self._保存()
        标签 = f"L{位置}" if 阶段 == "LLM" else str(位置)
        yield event.plain_result(
            f"✅ 已更新 {标签} 条件「{条件.get('类型')}」的 {字段}"
        )

    @filter.command("所有指令")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_所有指令(self, event: AstrMessageEvent):
        """查看已识别的全部指令"""
        if not self.所有指令:
            yield event.plain_result("⚠️ 指令表为空，请先 /刷新指令")
            return
        前缀 = self.指令前缀[0] if self.指令前缀 else "/"
        yield event.plain_result(
            f"共 {len(self.所有指令)} 个指令：\n" + "\n".join(前缀 + i for i in self.所有指令)
        )

    @filter.command("刷新指令")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def 指令_刷新指令(self, event: AstrMessageEvent):
        """重新扫描指令表"""
        之前 = len(self.所有指令)
        self._刷新指令表()
        差值 = len(self.所有指令) - 之前
        yield event.plain_result(f"✅ 已刷新指令表，共 {len(self.所有指令)} 个（{差值:+d}）")

    @filter.command("设置活跃")
    async def 指令_设置活跃(self, event: AstrMessageEvent, 秒数: str = None):
        """为自己设置活跃时长（最大 600 秒）"""
        群号 = str(event.get_group_id() or "")
        if not 群号:
            yield event.plain_result("❌ 请在群聊中使用")
            return
        try:
            秒 = float(str(秒数).rstrip("秒s"))
        except (TypeError, ValueError):
            yield event.plain_result("❌ 请输入有效的秒数")
            return
        if 秒 <= 0:
            yield event.plain_result("❌ 秒数必须大于 0")
            return
        if 秒 > 600:
            yield event.plain_result("❌ 最大支持 600 秒（10 分钟）")
            return
        self.状态.设置自定义活跃(群号, str(event.get_sender_id()), 秒)
        yield event.plain_result(f"✅ 已设置活跃时长 {秒:g} 秒，唤醒后会自动续期")

    @filter.command("停止活跃")
    async def 指令_停止活跃(self, event: AstrMessageEvent, 清除: str = None):
        """停止活跃状态"""
        群号 = str(event.get_group_id() or "")
        if not 群号:
            yield event.plain_result("❌ 请在群聊中使用")
            return
        清时间, 清时长 = self.状态.清除自定义活跃(
            群号, str(event.get_sender_id()), 同时清除时长=清除 is not None
        )
        if 清时间 or 清时长:
            尾巴 = "，并清除了自定义时长" if 清时长 else "（自定义时长保留）"
            yield event.plain_result(f"✅ 已停止活跃状态{尾巴}")
        else:
            yield event.plain_result("⚠️ 你当前没有活跃状态")

    # ------------------------------------------------------------------ 指令辅助

    def _查找规则(self, 目标: str | None, event: AstrMessageEvent) -> tuple[dict[str, Any] | None, str]:
        目标 = str(目标).strip() if 目标 else ""
        if 目标 in ("兜底规则", "兜底"):
            return self._兜底(), ""
        if not 目标:
            群号 = str(event.get_group_id() or "")
            规则 = self._取规则(群号)
            if 规则 is None:
                return None, "❌ 当前群没有可用规则，请用 /添加群规 新建，或显式指定群号/备注"
            return 规则, ""

        for 规则 in self.数据.get("规则列表") or []:
            if 目标 in (规则.get("备注"), 规则.get("id")) or 目标 in (规则.get("群号") or []):
                return 规则, ""
        return None, f"❌ 未找到群号/备注/ID 为「{目标}」的规则"

    @staticmethod
    def _解析条件目标(
        规则: dict[str, Any], 序号文本: str
    ) -> tuple[str, list[dict[str, Any]], int]:
        """解析条件序号：`L1` 指向 LLM 请求前流水线，纯数字指向接收消息流水线。

        返回 (阶段, 条件列表, 1 基位置)；解析失败抛 ValueError。
        """
        文本 = str(序号文本 or "").strip()
        if 文本[:1] in ("L", "l"):
            阶段 = "LLM"
            数字 = 文本[1:]
            条件列表 = 规则.get("LLM前条件") or []
        else:
            阶段 = "接收"
            数字 = 文本
            条件列表 = 规则.get("条件") or []
        try:
            位置 = int(数字)
        except (TypeError, ValueError) as e:
            raise ValueError("序号必须是整数，LLM 请求前流水线请写成 L1、L2…") from e
        return 阶段, 条件列表, 位置

    def _转换值(self, 键: str, 值: str, 原值: Any, 原类型: type, 操作: str) -> Any:
        if 原类型 is bool:
            if 操作:
                raise ValueError(f"字段「{键}」是布尔值，不支持操作参数")
            if 值.lower() in ("开", "true", "1", "on", "yes", "是", "启用"):
                return True
            if 值.lower() in ("关", "false", "0", "off", "no", "否", "禁用"):
                return False
            raise ValueError(f"无法解析布尔值「{值}」，支持 开/关、on/off、1/0")

        if 原类型 is int:
            try:
                return int(float(值))
            except (TypeError, ValueError) as e:
                raise ValueError(f"无法解析整数「{值}」") from e

        if 原类型 is float:
            文本 = 值.rstrip("秒sS")
            try:
                数 = float(文本)
            except (TypeError, ValueError) as e:
                raise ValueError(f"无法解析数字「{值}」") from e
            if 键 in ("唤醒冷却", "持续活跃", "活跃间隔") and 数 < -1:
                raise ValueError("时间类字段最小为 -1（-1 表示继承兜底规则）")
            return round(数, 3)

        if 原类型 is list:
            如果清空 = 值.strip().lower() in ("null", "none", "空", "")
            新项 = [] if 如果清空 else [i.strip() for i in 值.replace("，", ",").split(",") if i.strip()]
            实际 = 操作 or "rep"
            if 实际 == "add":
                结果 = list(原值 or [])
                for i in 新项:
                    if i not in 结果:
                        结果.append(i)
                return 结果
            if 实际 == "del":
                缺失 = [i for i in 新项 if i not in (原值 or [])]
                if 缺失:
                    raise ValueError(f"原列表中不存在：{'、'.join(缺失)}")
                return [i for i in (原值 or []) if i not in 新项]
            if 实际 in ("rep", "set", "替换"):
                return 新项
            raise ValueError(f"不支持的操作「{操作}」，可用 add/del/rep")

        if 原类型 is str:
            if 操作:
                raise ValueError(f"字段「{键}」是字符串，不支持操作参数")
            if 键 == "活跃方式" and 值 not in models.活跃方式可选:
                raise ValueError(f"活跃方式可选：{'、'.join(models.活跃方式可选)}")
            if 键 == "活跃范围" and 值 not in models.活跃范围可选:
                raise ValueError(f"活跃范围可选：{'、'.join(models.活跃范围可选)}")
            return 值

        raise ValueError(f"不支持的字段类型：{原类型}")

    def _展示值(self, 值: Any) -> str:
        if isinstance(值, bool):
            return "开" if 值 else "关"
        if isinstance(值, list):
            return "、".join(值) if 值 else "（空）"
        return str(值)

    def _条件描述(self, 条件: dict[str, Any]) -> str:
        类型 = 条件.get("类型", "")
        参数 = models.条件类型.get(类型, {}).get("参数", [])
        尾巴 = []
        if "正则" in 参数:
            正则 = 条件.get("正则") or []
            尾巴.append(f"正则 {len(正则)} 条" if 正则 else "正则未填")
        if "概率" in 参数:
            尾巴.append(f"{float(条件.get('概率') or 0) * 100:.0f}%")
            冷却 = float(条件.get("概率冷却") or 0)
            if 冷却 > 0:
                尾巴.append(f"冷却 {冷却:g} 分钟")
        return f"（{'，'.join(尾巴)}）" if 尾巴 else ""

    def _条件行(self, 序号: Any, 条件: dict[str, Any], 阶段: str = "接收") -> str:
        类型 = 条件.get("类型")
        动作 = models.条件类型.get(类型, {}).get("动作", "?")
        if 阶段 == "LLM" and 动作 == "唤醒":
            动作 = "屏蔽"  # LLM 前流水线只做屏蔽，@ / 引用类在这里也是拦
        开关 = "✅" if 条件.get("开关") else "⏸"
        行 = [f"{序号}. {开关} {动作}·{类型}{self._条件描述(条件)}"]
        if 阶段 == "接收" and not 条件.get("开关") and 类型 in models.关闭即屏蔽类型:
            行.append("      ↳ 已关闭：命中即屏蔽（不再唤醒，也不再放行）")
        if 条件.get("提示词"):
            行.append(f"      提示词：{条件['提示词']}")
        return "\n".join(行)

    def _条件文本(self, 规则: dict[str, Any]) -> str:
        接收 = 规则.get("条件") or []
        LLM前 = 规则.get("LLM前条件") or []
        名称 = models.规则名称(规则)
        行 = [f"🧩 规则「{名称}」· 接收消息流水线（{len(接收)} 条，从上到下判断）"]
        行.extend(self._条件行(序号, 条件) for 序号, 条件 in enumerate(接收, 1))

        行.append("")
        行.append(f"🤖 规则「{名称}」· LLM 请求前流水线（{len(LLM前)} 条，只认屏蔽）")
        if LLM前:
            行.extend(
                self._条件行(f"L{序号}", 条件, "LLM") for 序号, 条件 in enumerate(LLM前, 1)
            )
        else:
            行.append("（空：进入模型前不再额外拦截）")

        行.append("")
        行.append("• 序号带 L 的操作对应 LLM 前流水线，例如 /删除条件 L1、/移动条件 L2 上")
        return "\n".join(行)

    def _规则文本(self, 群号: str, 规则: dict[str, Any], 来源: str) -> str:
        def 时间(键: str) -> str:
            原值 = 规则.get(键, -1)
            try:
                原值 = float(原值)
            except (TypeError, ValueError):
                原值 = -1
            值 = self.引擎.决议(规则, 键)
            return f"{值:g} 秒（继承兜底）" if 原值 < 0 else f"{值:g} 秒"

        行 = [
            f"📋 群 {群号} 的规则（{来源}）",
            f"• 备注：{models.规则名称(规则)}",
            f"• 状态：{'✅ 开启' if 规则.get('开关') else '❌ 关闭'}",
            f"• 群号：{'、'.join(规则.get('群号') or []) or '无'}",
            "— 时间 —",
            f"• 持续活跃：{时间('持续活跃')}｜活跃间隔：{时间('活跃间隔')}｜唤醒冷却：{时间('唤醒冷却')}",
            f"• 活跃方式：{规则.get('活跃方式')}｜活跃范围：{规则.get('活跃范围')}",
            "— 指令 —",
            f"• 指令黑白名单：{acl.名单描述(规则.get('指令黑白名单'))}",
            f"• 禁用系统指令：{'✅' if 规则.get('禁用系统指令') else '❌'}"
            f"｜禁前唤醒：{'✅' if 规则.get('禁前唤醒') else '❌'}"
            f"｜指令不唤醒：{'✅' if 规则.get('指令不唤醒') else '❌'}",
            "— 其他 —",
            f"• 用户黑白名单：{acl.名单描述(规则.get('用户黑白名单'))}",
            f"• 管理员豁免：{'✅' if 规则.get('管理员豁免') else '❌'}",
            "",
            self._条件文本(规则),
        ]
        return "\n".join(行)
