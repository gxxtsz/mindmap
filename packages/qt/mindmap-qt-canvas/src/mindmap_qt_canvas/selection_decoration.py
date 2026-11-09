"""文本节点的视口选中装饰；不进入场景图元、正式测量、命中及图形导出。"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath

from .items import NodeItem

# 单位为视口逻辑像素，设备像素比由 Qt 处理；缩放导图不会缩小留白或加粗外框。
SELECTION_GAP = 3.0
SELECTION_WIDTH = 2.0
_REPAINT_PADDING = SELECTION_GAP + SELECTION_WIDTH + 1.0


def _selected_nodes(view):
    """只枚举已选节点及当前编辑节点，不扫描整图，也不强制创建屏外图元。"""
    owner = view.owner
    if owner._disposed:
        return
    items = [item for item in view.scene().selectedItems() if isinstance(item, NodeItem)]
    editor = getattr(owner, "editor", None)
    if editor is not None:
        editing = owner.node_items.get(editor.node_id)
        if editing is not None and editing not in items:
            items.append(editing)
    for item in items:
        if item.isVisible() and item.effectiveOpacity() > 0 and not item.node.is_image_node:
            yield item


def _body_outline(item, transform, pixel_ratio):
    """取得正文连同正式描边的视口外缘，防止宽边框被留白遮盖或随缩放拉大间隔。"""
    visual = item.visual
    mapping = item.deviceTransform(transform)
    rect = mapping.mapRect(QRectF(1, 1, visual.width - 2, visual.height - 2))
    # NodeVisual 在内缩 1 像素的路径上居中描边；宽度为零时 Qt 使用设备像素细线。
    half_border = (visual.border_width * mapping.m11() / 2 if visual.border_width > 0
                   else 0.5 / pixel_ratio)
    radius = min(7 * mapping.m11(), rect.width() / 2, rect.height() / 2) + half_border
    return rect.adjusted(-half_border, -half_border, half_border, half_border), radius


def selection_bounds(view):
    """返回当前文本选中外框在视口内的联合重绘范围，空选择返回空 QRect。

    view 为存活的交互视图，仅在 GUI 线程借用；返回值包含一个逻辑像素的抗锯齿
    余量，并裁到实际视口。调用方同时重绘上次范围即可清除移动、缩短及取消选择
    后的旧外框，不必扩大节点 boundingRect 或影响滚动范围，不修改文档及选择。
    """
    bounds = QRectF()
    transform = view.viewportTransform()
    viewport = QRectF(view.viewport().rect())
    pixel_ratio = view.viewport().devicePixelRatioF()
    for item in _selected_nodes(view):
        rect, _ = _body_outline(item, transform, pixel_ratio)
        rect = rect.adjusted(-_REPAINT_PADDING, -_REPAINT_PADDING,
                             _REPAINT_PADDING, _REPAINT_PADDING).intersected(viewport)
        bounds = bounds.united(rect)
    return bounds.toAlignedRect()


def _rounded_rect(rect, radius):
    """构造独立圆角路径，半径包含外扩距离，使外框与节点保持均匀间隔。"""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def paint_node_selection(view, painter: QPainter) -> None:
    """按实际画布底色绘制 3 像素间隔及 2 像素外框，返回 None。

    view、painter 仅供本次 GUI 绘制借用；先映射到视口再绘制，因此 10%～400%
    缩放与高 DPI 均保持相同的逻辑线宽。根节点、普通节点、多选及编辑草稿复用
    同一规则；编辑器借用焦点时仍显示其所属节点。图片继续使用原有轮廓和手柄。
    间隔只覆盖正文外部，并避让可见折叠按钮；透明度跟随拖动中的源节点。
    不持有输入对象、不创建场景图元、不修改文档或缓存；结束或异常时恢复画笔。
    """
    transform = view.viewportTransform()
    viewport = QRectF(view.viewport().rect())
    background = view.scene().backgroundBrush()
    pixel_ratio = painter.device().devicePixelRatioF()
    painter.save()
    try:
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        opacity = painter.opacity()
        for item in _selected_nodes(view):
            rect, radius = _body_outline(item, transform, pixel_ratio)
            outer_rect = rect.adjusted(-SELECTION_GAP - SELECTION_WIDTH, -SELECTION_GAP - SELECTION_WIDTH,
                                       SELECTION_GAP + SELECTION_WIDTH, SELECTION_GAP + SELECTION_WIDTH)
            if not viewport.intersects(outer_rect):
                continue
            body = _rounded_rect(rect, radius)
            gap = _rounded_rect(rect.adjusted(-SELECTION_GAP, -SELECTION_GAP,
                                              SELECTION_GAP, SELECTION_GAP), radius + SELECTION_GAP)
            outer = _rounded_rect(outer_rect, radius + SELECTION_GAP + SELECTION_WIDTH)
            ring = outer.subtracted(gap)
            gap = gap.subtracted(body)
            control = item.fold_control
            if control is not None and control.isVisible():
                # 折叠按钮与正文只相隔 4 个场景像素，低倍率时更靠近正文。按真实
                # 圆角形状及描边避让，不能用包含透明悬停通道的 boundingRect 裁剪。
                button = _rounded_rect(control.button_rect().adjusted(-0.5, -0.5, 0.5, 0.5), 10.5)
                button = control.deviceTransform(transform).map(button)
                gap = gap.subtracted(button)
                ring = ring.subtracted(button)
            painter.setOpacity(opacity * item.effectiveOpacity())
            painter.fillPath(gap, background)
            painter.fillPath(ring, QColor(item.theme.accent))
    finally:
        painter.restore()
