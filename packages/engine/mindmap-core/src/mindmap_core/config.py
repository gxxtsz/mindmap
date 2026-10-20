"""可持久化的布局配置；核心只检查结构，算法负责自身选项的语义。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import MindMapError

Direction = Literal["left", "right", "both", "down"]


@dataclass
class LayoutConfig:
    """算法标识与通用方向分开保存，options 承载具体扩展的 JSON 参数。

    未安装的算法也能随文档保存；只有请求布局或在界面使用时才需要对应实现。
    """

    algorithm_id: str = "tree"
    direction: Direction = "right"
    compact: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """检查可序列化结构，不导入或查找任何布局插件。"""
        if not isinstance(self.algorithm_id, str) or not self.algorithm_id.strip():
            raise MindMapError("布局算法标识必须为非空字符串")
        if self.direction not in ("left", "right", "both", "down"):
            raise MindMapError("不支持的布局方向")
        if not isinstance(self.compact, bool):
            raise MindMapError("紧凑布局标记必须为布尔值")
        if not isinstance(self.options, dict) or not all(isinstance(key, str) for key in self.options):
            raise MindMapError("布局选项必须为字符串键对象")
        try:
            json.dumps(self.options, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise MindMapError("布局选项必须是有效的 JSON 数据") from exc
