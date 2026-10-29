"""落盘：原子写入 + 损坏自愈。

数据文件固定为 `<数据目录>/rules.json`，不经过框架的插件配置系统，
因此不受 `_conf_schema.json` 的渲染限制（面板可以完全自定义）。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from . import models

__all__ = ["数据存储"]

文件名 = "rules.json"
备份名 = "rules.json.bak"


class 数据存储:
    def __init__(self, 目录: Path) -> None:
        self.目录 = Path(目录)
        self.路径 = self.目录 / 文件名
        self.备份路径 = self.目录 / 备份名
        self.最后错误: str | None = None

    def 确保目录(self) -> None:
        self.目录.mkdir(parents=True, exist_ok=True)

    def 加载(self) -> dict[str, Any]:
        self.确保目录()
        self.最后错误 = None
        if not self.路径.exists():
            return models.空数据()
        try:
            文本 = self.路径.read_text(encoding="utf-8-sig")
            if not 文本.strip():
                return models.空数据()
            原始 = json.loads(文本)
        except Exception as e:  # noqa: BLE001 - 任何解析问题都走自愈
            self.最后错误 = str(e)
            try:
                时间戳 = time.strftime("%Y%m%d-%H%M%S")
                损坏备份 = self.目录 / f"rules.broken-{时间戳}.json"
                shutil.copy2(self.路径, 损坏备份)
            except Exception:  # noqa: BLE001
                pass
            return models.空数据()
        return models.规范化数据(原始)

    def 保存(self, 数据: dict[str, Any]) -> None:
        self.确保目录()
        标准 = models.规范化数据(数据)
        临时路径 = self.路径.with_suffix(".json.tmp")
        文本 = json.dumps(标准, ensure_ascii=False, indent=2)
        with open(临时路径, "w", encoding="utf-8", newline="\n") as f:
            f.write(文本)
            f.flush()
            os.fsync(f.fileno())
        if self.路径.exists():
            try:
                shutil.copy2(self.路径, self.备份路径)
            except Exception:  # noqa: BLE001
                pass
        os.replace(临时路径, self.路径)

    def 重置(self) -> dict[str, Any]:
        数据 = models.空数据()
        self.保存(数据)
        return 数据
