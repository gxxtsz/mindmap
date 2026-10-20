"""文档完整性与数值校验；校验失败只报告错误，不修补原始输入。"""

import hashlib
import json
import math
from typing import Any

from .branch_styles import get_branch_style
from .config import LayoutConfig
from .errors import MindMapError


def validate_document(document) -> None:
    """验证树、引用、数值和资源。失败时抛出 MindMapError，不修补损坏输入。"""
    if document.root_id not in document.nodes or not document.nodes:
        raise MindMapError("文档缺少有效根节点")
    if not isinstance(document.layout, LayoutConfig):
        raise MindMapError("布局配置必须为 LayoutConfig")
    document.layout.validate()
    seen: set[str] = set()
    stack = [document.root_id]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            raise MindMapError("节点树包含循环或重复父节点")
        if node_id not in document.nodes:
            raise MindMapError(f"子节点引用不存在：{node_id}")
        seen.add(node_id)
        node = document.nodes[node_id]
        if node.id != node_id or not isinstance(node_id, str) or not node_id:
            raise MindMapError("节点标识无效")
        if not all(isinstance(value, str) for value in (node.text, node.note, node.hyperlink)):
            raise MindMapError("节点文本、备注和链接必须为字符串")
        if not isinstance(node.children, list) or not all(isinstance(x, str) for x in node.children):
            raise MindMapError("子节点列表无效")
        if not isinstance(node.expanded, bool) or node.direction not in (None, "left", "right"):
            raise MindMapError("节点展开状态或方向无效")
        if node.text_format not in ("plain", "markdown"):
            raise MindMapError("文本格式必须是 plain 或 markdown")
        for values in (node.tags, node.icons):
            if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
                raise MindMapError("标签和图标必须为字符串列表")
        if not isinstance(node.style, dict) or not isinstance(node.metadata, dict):
            raise MindMapError("节点样式和元数据必须为对象")
        get_branch_style(node.style)
        for key in ("font_size", "width", "border_width"):
            if key in node.style:
                positive_number(node.style[key], f"节点样式 {key}", allow_zero=key == "border_width")
        for key in ("font_family", "text_color", "background", "border_color"):
            if key in node.style and not isinstance(node.style[key], str):
                raise MindMapError(f"节点样式 {key} 必须为字符串")
        for key in ("bold", "italic", "underline"):
            if key in node.style and not isinstance(node.style[key], bool):
                raise MindMapError(f"节点样式 {key} 必须为布尔值")
        positive_number(node.image_width, "图片宽度")
        if node.image_id is not None and node.image_id not in document.assets:
            raise MindMapError(f"图片资源缺失：{node.image_id}")
        stack.extend(reversed(node.children))
    if seen != set(document.nodes):
        raise MindMapError("文档包含未连接到根节点的节点")
    for key, asset in document.assets.items():
        if not isinstance(asset.data, bytes) or not asset.data:
            raise MindMapError("图片数据必须为非空字节串")
        if key != asset.id or hashlib.sha256(asset.data).hexdigest() != key:
            raise MindMapError("图片内容与资源标识不匹配")
        if not isinstance(asset.media_type, str) or not asset.media_type.startswith("image/"):
            raise MindMapError("资源必须是图片")
    if (
        not isinstance(document.theme.palette, list)
        or not document.theme.palette
        or not all(isinstance(x, str) and x for x in document.theme.palette)
    ):
        raise MindMapError("主题分支配色不能为空")
    if not all(
        isinstance(getattr(document.theme, key), str) and getattr(document.theme, key)
        for key in ("name", "background", "node_background", "text", "border", "accent")
    ):
        raise MindMapError("主题名称及颜色必须为非空字符串")
    if not isinstance(document.metadata, dict):
        raise MindMapError("文档元数据必须为对象")
    try:
        json.dumps(
            [
                document.metadata,
                *[n.metadata for n in document.nodes.values()],
                *[n.style for n in document.nodes.values()],
            ],
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MindMapError("元数据与样式必须是有效的 JSON 数据") from exc


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def positive_number(value: Any, name: str, *, allow_zero: bool = False) -> None:
    if not finite(value) or (value < 0 if allow_zero else value <= 0):
        raise MindMapError(f"{name}必须为{'非负' if allow_zero else '正'}有限数值")
