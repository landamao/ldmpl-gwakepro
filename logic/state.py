"""运行时状态：活跃窗口、冷却、监听、群目录。

所有状态都在内存里，进程重启即清空——这与旧插件一致，也避免把「活跃」这种
短周期状态写进磁盘造成脏数据。群目录是唯一需要持久化的小数据，由主插件负责落盘。
"""

from __future__ import annotations

import time
from typing import Any

__all__ = ["运行状态", "监听键"]


def 监听键(群号: str, 目标: str) -> tuple[str, str]:
    return (str(群号), str(目标))


class 运行状态:
    def __init__(self) -> None:
        self.群活跃: dict[str, float] = {}
        self.群上次唤醒: dict[str, float] = {}
        self.用户活跃: dict[str, float] = {}
        self.用户上次唤醒: dict[str, float] = {}
        self.用户冷却: dict[str, float] = {}
        # 概率唤醒冷却：(群号, 条件 id) -> 到期时间。按群隔离，避免群消息频繁时反复触发
        self.概率冷却: dict[tuple[str, str], float] = {}
        # 用户自定义活跃（「/设置活跃」设定，群内隔离）
        self.自定义活跃: dict[tuple[str, str], float] = {}
        self.自定义时长: dict[tuple[str, str], float] = {}
        # 监听（LLM 通过 standby 工具发起）
        self.监听: dict[tuple[str, str], dict[str, Any]] = {}
        # 群目录：群号 -> {"名称": str, "时间": float}
        self.群目录: dict[str, dict[str, Any]] = {}
        self.最后清理 = 0.0

    # ---------------- 活跃 ----------------

    def 活跃到期(self, 群号: str, 发送者: str, 范围: str) -> float:
        if 范围 == "群聊级":
            return self.群活跃.get(群号, 0.0)
        return self.用户活跃.get(发送者, 0.0)

    def 上次唤醒(self, 群号: str, 发送者: str, 范围: str) -> float:
        if 范围 == "群聊级":
            return self.群上次唤醒.get(群号, 0.0)
        return self.用户上次唤醒.get(发送者, 0.0)

    def 记录活跃(self, 群号: str, 发送者: str, 范围: str, 时长: float, 现在: float | None = None) -> None:
        现在 = time.time() if 现在 is None else 现在
        if 时长 <= 0:
            return
        if 范围 == "群聊级":
            self.群活跃[群号] = 现在 + 时长
            self.群上次唤醒[群号] = 现在
        else:
            self.用户活跃[发送者] = 现在 + 时长
            self.用户上次唤醒[发送者] = 现在

    def 结束活跃(self, 群号: str, 发送者: str, 范围: str) -> None:
        if 范围 == "群聊级":
            self.群活跃.pop(群号, None)
            self.群上次唤醒.pop(群号, None)
        else:
            self.用户活跃.pop(发送者, None)
            self.用户上次唤醒.pop(发送者, None)

    # ---------------- 自定义活跃 ----------------

    def 自定义活跃到期(self, 群号: str, 发送者: str) -> float:
        return self.自定义活跃.get((群号, 发送者), 0.0)

    def 设置自定义活跃(self, 群号: str, 发送者: str, 秒: float, 现在: float | None = None) -> None:
        现在 = time.time() if 现在 is None else 现在
        self.自定义时长[(群号, 发送者)] = 秒
        self.自定义活跃[(群号, 发送者)] = 现在 + 秒

    def 刷新自定义活跃(self, 群号: str, 发送者: str, 现在: float | None = None) -> float:
        现在 = time.time() if 现在 is None else 现在
        时长 = self.自定义时长.get((群号, 发送者), 0.0)
        if 时长 > 0:
            self.自定义活跃[(群号, 发送者)] = 现在 + 时长
        return 时长

    def 清除自定义活跃(self, 群号: str, 发送者: str, 同时清除时长: bool = False) -> tuple[bool, bool]:
        清时间 = self.自定义活跃.pop((群号, 发送者), None) is not None
        清时长 = False
        if 同时清除时长:
            清时长 = self.自定义时长.pop((群号, 发送者), None) is not None
        return 清时间, 清时长

    # ---------------- 冷却 ----------------

    def 冷却剩余(self, 发送者: str, 现在: float | None = None) -> float:
        现在 = time.time() if 现在 is None else 现在
        到期 = self.用户冷却.get(发送者, 0.0)
        if 到期 <= 现在:
            if 到期:
                self.用户冷却.pop(发送者, None)
            return 0.0
        return 到期 - 现在

    def 设置冷却(self, 发送者: str, 秒: float, 现在: float | None = None) -> None:
        if 秒 <= 0:
            return
        现在 = time.time() if 现在 is None else 现在
        self.用户冷却[发送者] = 现在 + 秒

    # ---------------- 概率唤醒冷却 ----------------

    def 概率冷却剩余(
        self, 群号: str, 条件ID: str, 现在: float | None = None
    ) -> float:
        现在 = time.time() if 现在 is None else 现在
        键 = (str(群号), str(条件ID))
        到期 = self.概率冷却.get(键, 0.0)
        if 到期 <= 现在:
            if 到期:
                self.概率冷却.pop(键, None)
            return 0.0
        return 到期 - 现在

    def 设置概率冷却(
        self, 群号: str, 条件ID: str, 秒: float, 现在: float | None = None
    ) -> None:
        if 秒 <= 0:
            return
        现在 = time.time() if 现在 is None else 现在
        self.概率冷却[(str(群号), str(条件ID))] = 现在 + 秒

    # ---------------- 监听 ----------------

    def 设置监听(self, 群号: str, 目标: str, 模式: str, 秒: float, 现在: float | None = None) -> None:
        现在 = time.time() if 现在 is None else 现在
        self.监听[监听键(群号, 目标)] = {
            "到期": 现在 + 秒,
            "时长": 秒,
            "模式": 模式,
            "开始": 现在,
        }

    def 取消监听(self, 群号: str, 目标: str) -> bool:
        return self.监听.pop(监听键(群号, 目标), None) is not None

    def 检查监听(
        self,
        群号: str,
        发送者: str,
        现在: float | None = None,
        *,
        艾特了发送者: bool = False,
    ) -> tuple[str, float]:
        """返回（状态, 剩余秒数）。

        状态：
        - "none"  没有监听
        - "hit"   命中监听，应唤醒
        - "reset" 监听了该用户，但本条消息艾特了该用户，监听时长重置
        """
        现在 = time.time() if 现在 is None else 现在
        用户键 = 监听键(群号, 发送者)
        全体键 = 监听键(群号, "*")

        用户监听 = self.监听.get(用户键)
        if 用户监听:
            if 用户监听["到期"] <= 现在:
                self.监听.pop(用户键, None)
            else:
                if 艾特了发送者:
                    用户监听["到期"] = 现在 + 用户监听["时长"]
                    return "reset", 用户监听["到期"] - 现在
                return "hit", 用户监听["到期"] - 现在

        全体监听 = self.监听.get(全体键)
        if 全体监听:
            if 全体监听["到期"] <= 现在:
                self.监听.pop(全体键, None)
            else:
                return "hit", 全体监听["到期"] - 现在

        return "none", 0.0

    def 消耗监听(self, 群号: str, 发送者: str) -> None:
        """命中后消耗监听（监听是一次性的，命中即结束）。"""
        self.监听.pop(监听键(群号, 发送者), None)
        self.监听.pop(监听键(群号, "*"), None)

    def 监听快照(self, 现在: float | None = None) -> list[dict[str, Any]]:
        现在 = time.time() if 现在 is None else 现在
        结果 = []
        for (群号, 目标), 值 in sorted(self.监听.items()):
            到期 = float(值.get("到期", 0))
            if 到期 <= 现在:
                continue
            结果.append(
                {
                    "群号": 群号,
                    "目标": 目标,
                    "模式": 值.get("模式", "user"),
                    "剩余": round(到期 - 现在, 1),
                }
            )
        return 结果

    # ---------------- 群目录 ----------------

    def 记录群(self, 群号: str, 名称: str = "", 现在: float | None = None) -> bool:
        现在 = time.time() if 现在 is None else 现在
        群号 = str(群号 or "").strip()
        if not 群号:
            return False
        原 = self.群目录.get(群号)
        名称 = (名称 or "").strip()
        if 原 and (not 名称 or 原.get("名称") == 名称):
            原["时间"] = 现在
            return False
        self.群目录[群号] = {"名称": 名称 or (原 or {}).get("名称", ""), "时间": 现在}
        return True

    # ---------------- 清理 ----------------

    def 清理(self, 现在: float | None = None) -> None:
        现在 = time.time() if 现在 is None else 现在
        for 表 in (self.群活跃, self.用户活跃, self.自定义活跃, self.用户冷却, self.概率冷却):
            for 键 in [k for k, v in 表.items() if v <= 现在]:
                表.pop(键, None)
        for 表 in (self.群上次唤醒, self.用户上次唤醒):
            for 键 in [k for k, v in 表.items() if 现在 - v > 86400]:
                表.pop(键, None)
        for 键 in [k for k, v in self.监听.items() if v.get("到期", 0) <= 现在]:
            self.监听.pop(键, None)

    def 活跃快照(self, 现在: float | None = None) -> dict[str, Any]:
        现在 = time.time() if 现在 is None else 现在
        return {
            "群聊": [
                {"群号": k, "剩余": round(v - 现在, 1)}
                for k, v in sorted(self.群活跃.items())
                if v > 现在
            ],
            "用户": [
                {"用户": k, "剩余": round(v - 现在, 1)}
                for k, v in sorted(self.用户活跃.items())
                if v > 现在
            ],
            "自定义": [
                {"群号": k[0], "用户": k[1], "剩余": round(v - 现在, 1)}
                for k, v in sorted(self.自定义活跃.items())
                if v > 现在
            ],
            "冷却": [
                {"用户": k, "剩余": round(v - 现在, 1)}
                for k, v in sorted(self.用户冷却.items())
                if v > 现在
            ],
            "概率冷却": [
                {"群号": k[0], "条件": k[1], "剩余": round(v - 现在, 1)}
                for k, v in sorted(self.概率冷却.items())
                if v > 现在
            ],
            "监听": self.监听快照(现在),
        }
