"""文档完整性与数值校验；校验失败只报告错误，不修补原始输入。"""

import hashlib
import json
import math
from collections import Counter
from typing import Any

from .branch_styles import get_branch_style
from .config import LayoutConfig
from .errors import MindMapError
from .models import MindMapPage, Theme


def validate_document(document, *, page_only=False) -> None:
    """验证树、引用、数值和资源。失败时抛出 MindMapError，不修补损坏输入。"""
    if document.root_id not in document.nodes or not document.nodes:
        raise MindMapError("文档缺少有效根节点")
    if not isinstance(document.submaps, dict):
        raise MindMapError("子导图表必须为对象")
    if not isinstance(document.detail_pages, dict):
        raise MindMapError("详情页表必须为对象")
    if document.submaps.keys() & document.detail_pages.keys():
        raise MindMapError("不同类型的页面不能共用标识")
    if not isinstance(document.layout, LayoutConfig):
        raise MindMapError("布局配置必须为 LayoutConfig")
    document.layout.validate()
    seen: set[str] = set()
    pages: set[str] = set()
    # 栈中保留所属页面类别，普通后代沿用上下文；跨页时才改变类别。
    # 因而完整导入同样不能把附属页面藏在详情页的深层节点中。
    stack = [(document.root_id, getattr(document, "kind", "main") == "detail")]
    while stack:
        node_id, in_detail = stack.pop()
        if node_id in seen:
            raise MindMapError("节点树包含循环或重复父节点")
        if node_id not in document.nodes:
            raise MindMapError(f"子节点引用不存在：{node_id}")
        seen.add(node_id)
        node = document.nodes[node_id]
        validate_node(document, node_id, node, json_data=False)
        if in_detail and (node.submap_id or node.detail_page_id):
            raise MindMapError("详情页内不能拥有详情页或子导图")
        stack.extend((child, in_detail) for child in reversed(node.children))
        if not page_only:
            for kind, page_id, table in (("submap", node.submap_id, document.submaps),
                                         ("detail", node.detail_page_id, document.detail_pages)):
                if not page_id:
                    continue
                page = table.get(page_id)
                if not isinstance(page, MindMapPage) or page.id != page_id or page.owner_id != node_id:
                    raise MindMapError("页面引用不存在或页面归属无效")
                if page.id in pages:
                    raise MindMapError("页面包含循环或重复入口")
                pages.add(page.id)
                validate_page(document, page, kind=kind)
                stack.append((page.root_id, kind == "detail"))
    if seen != set(document.nodes):
        raise MindMapError("文档包含未连接到根节点的节点")
    if not page_only and pages != set(document.submaps) | set(document.detail_pages):
        raise MindMapError("文档包含没有入口的子导图或详情页")
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


def validate_node(document, node_id, node, *, json_data=True):
    """校验单个实体，完整导入和受控差量提交共用同一套字段规则。"""
    if node.id != node_id or not isinstance(node_id, str) or not node_id:
        raise MindMapError("节点标识无效")
    if not all(isinstance(value, str) for value in (node.text, node.note, node.hyperlink)):
        raise MindMapError("节点文本、备注和链接必须为字符串")
    if not isinstance(node.children, list) or not all(isinstance(x, str) for x in node.children):
        raise MindMapError("子节点列表无效")
    if node.submap_id is not None:
        if not isinstance(node.submap_id, str) or not node.submap_id:
            raise MindMapError("子导图标识无效")
        if node.children:
            raise MindMapError("此节点已有子导图，请进入子导图添加内容")
    if node.detail_page_id is not None and (not isinstance(node.detail_page_id, str) or not node.detail_page_id):
        raise MindMapError("详情页标识无效")
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
    if json_data:
        try:
            json.dumps([node.metadata, node.style], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise MindMapError("元数据与样式必须是有效的 JSON 数据") from exc


def validate_page(document, page, *, kind=None):
    """校验单张页面的归属、名称和独立外观，不遍历它所拥有的下级页面。"""
    if not isinstance(page, MindMapPage):
        raise MindMapError("页面记录无效")
    if not all(isinstance(x, str) and x for x in (page.id, page.root_id, page.owner_id)):
        raise MindMapError("页面标识无效")
    if page.root_id not in document.nodes or page.owner_id not in document.nodes:
        raise MindMapError("页面根节点或入口不存在")
    owner = document.nodes[page.owner_id]
    kind = kind or document.page_kind(page.id)
    if kind == "submap" and (owner.submap_id != page.id or owner.children):
        raise MindMapError("子导图必须属于唯一叶子入口")
    if kind == "detail" and owner.detail_page_id != page.id:
        raise MindMapError("详情页必须属于唯一入口节点")
    if document.nodes[page.root_id].text != owner.text:
        raise MindMapError("页面根节点与入口名称不同步")
    if not isinstance(page.layout, LayoutConfig):
        raise MindMapError("布局配置必须为 LayoutConfig")
    page.layout.validate()
    if (not isinstance(page.theme, Theme) or not isinstance(page.theme.palette, list)
            or not page.theme.palette or not all(isinstance(x, str) and x for x in page.theme.palette)):
        raise MindMapError("主题分支配色不能为空")
    if not all(isinstance(getattr(page.theme, key), str) and getattr(page.theme, key)
               for key in ("name", "background", "node_background", "text", "border", "accent")):
        raise MindMapError("主题名称及颜色必须为非空字符串")


def validate_changes(state, journal):
    """校验受控编辑产生的局部变化，保留完整导入的字段和资源规则。

    编辑器不允许外部直接改写子列表。变更父边仍校验引用、重复父级及循环；
    只沿新父链检查连接性，并复用已确认路径。资源引用数在成功提交后才更新。
    """
    document = state.document
    for key in journal.nodes:
        node = document.nodes.get(key)
        if node is None:
            continue
        validate_node(document, key, node)
        if node.submap_id or node.detail_page_id:
            if document.page_kind(state.page_id(key)) == "detail":
                raise MindMapError("详情页内不能拥有详情页或子导图")
            for kind, page_id, table in (("submap", node.submap_id, document.submaps),
                                         ("detail", node.detail_page_id, document.detail_pages)):
                if not page_id:
                    continue
                page = table.get(page_id)
                if not isinstance(page, MindMapPage) or page.id != page_id or page.owner_id != key:
                    raise MindMapError("页面引用不存在或页面归属无效")
                validate_page(document, page, kind=kind)
        page_id = state.root_pages.get(key)
        if page_id is not None:
            validate_page(document, document.page_record(page_id))
        if len(set(node.children)) != len(node.children):
            raise MindMapError("节点树包含循环或重复父节点")
        for child in node.children:
            if child not in document.nodes:
                raise MindMapError(f"子节点引用不存在：{child}")
            if state.parents.get(child) != key:
                raise MindMapError("节点树包含循环或重复父节点")
    verified = {document.root_id}
    for key in journal.parents:
        if key not in document.nodes:
            continue
        path, seen = [], set()
        while key not in verified and key not in state.root_pages:
            if key in seen:
                raise MindMapError("节点树包含循环或重复父节点")
            seen.add(key)
            path.append(key)
            key = state.parents.get(key)
            if key is None or key not in document.nodes:
                raise MindMapError("文档包含未连接到根节点的节点")
        verified.update(path)
    for name in ("submaps", "detail_pages"):
        for key in getattr(journal, name):
            page = getattr(document, name).get(key)
            if page is not None:
                if page.id != key:
                    raise MindMapError("页面标识与记录不匹配")
                validate_page(document, page)
    usage = Counter()
    for key, old in journal.nodes.items():
        node = document.nodes.get(key)
        if old is not None and old.image_id:
            usage[old.image_id] -= 1
        if node is not None and node.image_id:
            usage[node.image_id] += 1
    candidates = set(usage) | set(journal.assets) | getattr(state, "unused_assets", set())
    for key in candidates:
        if state.asset_counts.get(key, 0) + usage[key] == 0 and key in document.assets:
            state.set_asset(key, None)
    for key in journal.assets:
        asset = document.assets.get(key)
        if asset is None:
            continue
        if not isinstance(asset.data, bytes) or not asset.data:
            raise MindMapError("图片数据必须为非空字节串")
        if key != asset.id or hashlib.sha256(asset.data).hexdigest() != key:
            raise MindMapError("图片内容与资源标识不匹配")
        if not isinstance(asset.media_type, str) or not asset.media_type.startswith("image/"):
            raise MindMapError("资源必须是图片")
    if journal.settings:
        if not isinstance(document.layout, LayoutConfig):
            raise MindMapError("布局配置必须为 LayoutConfig")
        document.layout.validate()
        theme = document.theme
        if (not isinstance(theme.palette, list) or not theme.palette
                or not all(isinstance(value, str) and value for value in theme.palette)):
            raise MindMapError("主题分支配色不能为空")
        if not all(isinstance(getattr(theme, key), str) and getattr(theme, key)
                   for key in ("name", "background", "node_background", "text", "border", "accent")):
            raise MindMapError("主题名称及颜色必须为非空字符串")
        if not isinstance(document.metadata, dict):
            raise MindMapError("文档元数据必须为对象")
        try:
            json.dumps(document.metadata, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise MindMapError("元数据与样式必须是有效的 JSON 数据") from exc


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def positive_number(value: Any, name: str, *, allow_zero: bool = False) -> None:
    if not finite(value) or (value < 0 if allow_zero else value <= 0):
        raise MindMapError(f"{name}必须为{'非负' if allow_zero else '正'}有限数值")
