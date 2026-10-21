"""文档模型与完整性检查。此模块只使用标准库，不接触 Qt 或文件系统。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .branch_styles import BranchStyle
from .config import LayoutConfig
from .errors import MindMapError


def new_id() -> str:
    """生成不依赖节点内容的稳定标识，复制节点时必须重新生成。"""
    return uuid4().hex


@dataclass
class Theme:
    """画布主题；颜色使用 Qt/SVG 均可识别的十六进制字符串。"""

    name: str = "浅色"
    background: str = "#f5f6f8"
    node_background: str = "#ffffff"
    text: str = "#253044"
    border: str = "#c5ccd7"
    accent: str = "#3478d5"
    palette: list[str] = field(default_factory=lambda: ["#3578cc", "#289b79", "#b076c2", "#cf8b38"])

    @classmethod
    def dark(cls) -> Theme:
        """返回独立的深色主题实例，修改它不会影响其他文档。"""
        return cls(
            "深色",
            "#20242c",
            "#2d3340",
            "#e6eaf0",
            "#596476",
            "#79b3ff",
            ["#79b3ff", "#6ac9a8", "#cba4ed", "#efbd78"],
        )


@dataclass
class Node:
    """节点实体。

    children 按顺序存放子节点 ID，父引用由文档临时索引推导，避免循环序列化。
    style 支持字体、颜色、宽度等原生绘制属性；metadata 只能包含 JSON 数据。
    """

    text: str = "新节点"
    id: str = field(default_factory=new_id)
    children: list[str] = field(default_factory=list)
    expanded: bool = True
    direction: str | None = None
    text_format: str = "plain"
    style: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    icons: list[str] = field(default_factory=list)
    note: str = ""
    hyperlink: str = ""
    image_id: str | None = None
    image_width: float = 160.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Asset:
    """不可变图片资源。内容哈希用于去重，二进制数据不参与历史深拷贝。"""

    id: str
    data: bytes
    media_type: str = "image/png"

    @classmethod
    def from_bytes(cls, data: bytes, media_type: str = "image/png") -> Asset:
        """从图片字节建立资源；图像能否解码由图形适配层进一步检查。"""
        if not isinstance(data, bytes) or not data:
            raise MindMapError("图片内容不能为空")
        return cls(hashlib.sha256(data).hexdigest(), data, media_type)

    def __deepcopy__(self, memo: dict) -> Asset:
        return self


@dataclass
class MindMapDocument:
    """完整文档。节点树以根标识和邻接表表达，支持深层树的非递归读写。"""

    root_id: str
    nodes: dict[str, Node]
    assets: dict[str, Asset] = field(default_factory=dict)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    theme: Theme = field(default_factory=Theme)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, text: str = "中心主题") -> MindMapDocument:
        """建立默认向右展开、使用圆角正交连接线的新文档。

        初始样式显式写入根主题，后续新增主题通过既有的父主题复制规则沿用。
        不改变 Node 的空样式语义，文件读取时原有的未配置曲线仍可原样恢复；
        初始化也不经过引擎事务，因此新文档保持未修改状态且没有额外撤销记录。
        """
        root = Node(text, style={"branch": {"kind": BranchStyle.ROUND.value}})
        return cls(root.id, {root.id: root})

    @property
    def root(self) -> Node:
        return self.nodes[self.root_id]

    def parents(self) -> dict[str, str]:
        """计算临时父索引；它不属于持久化内容。"""
        return {child: node.id for node in self.nodes.values() for child in node.children}

    def descendants(self, node_id: str) -> list[str]:
        """按先序返回节点和全部后代，使用显式栈避免深层树的 Python 递归限制。"""
        result, stack = [], [node_id]
        while stack:
            current = stack.pop()
            result.append(current)
            stack.extend(reversed(self.nodes[current].children))
        return result

    def branch_sides(self) -> dict[str, str]:
        """推导一级分支方向；双向模式优先保留指定方向，再按分支数量平衡。"""
        result: dict[str, str] = {}
        counts = {"left": 0, "right": 0}
        for child in self.root.children:
            side = self.layout.direction
            if side == "both":
                side = self.nodes[child].direction or min(counts, key=counts.get)
                counts[side] += 1
            for node_id in self.descendants(child):
                result[node_id] = side
        result[self.root_id] = self.layout.direction if self.layout.direction != "both" else "right"
        return result

    def validate(self) -> None:
        """验证完整文档；规则由独立校验模块维护，失败时不改变当前对象。"""
        from .validation import validate_document

        validate_document(self)
