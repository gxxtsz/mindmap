"""文本节点的视口选中装饰；仅缓存屏内外框，不进入场景、命中或图形导出。"""

from functools import lru_cache

from mindmap_qt_render.rendering import NODE_CARD_RADIUS
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QTransform

# 单位为视口逻辑像素，设备像素比由 Qt 处理；缩放导图不会改变留白及外框线宽。
SELECTION_GAP = 3.0
SELECTION_WIDTH = 2.0
_REPAINT_PADDING = SELECTION_GAP + SELECTION_WIDTH + 1.0


def _rounded_rect(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


@lru_cache(maxsize=512)
def _outline_paths(width, height, border, matrix, pixel_ratio, button):
    """按局部几何缓存路径；平移不进入键，尺寸、缩放、DPI 和折叠按钮参与键。

    所有输入均为不可变数值，不持有节点或视图；返回路径只读借用，调用方不能
    原地平移或修改。缓存有容量上限，避免滚动浏览整图后积累全部节点的路径。
    """
    mapping = QTransform(*matrix, 0, 0)
    rect = mapping.mapRect(QRectF(1, 1, width - 2, height - 2))
    half_border = border * mapping.m11() / 2 if border > 0 else 0.5 / pixel_ratio
    radius = min(NODE_CARD_RADIUS * mapping.m11(), rect.width() / 2, rect.height() / 2) + half_border
    rect.adjust(-half_border, -half_border, half_border, half_border)
    body = _rounded_rect(rect, radius)
    gap = _rounded_rect(rect.adjusted(-SELECTION_GAP, -SELECTION_GAP, SELECTION_GAP, SELECTION_GAP),
                        radius + SELECTION_GAP)
    outer = _rounded_rect(rect.adjusted(-SELECTION_GAP - SELECTION_WIDTH, -SELECTION_GAP - SELECTION_WIDTH,
                                       SELECTION_GAP + SELECTION_WIDTH, SELECTION_GAP + SELECTION_WIDTH),
                          radius + SELECTION_GAP + SELECTION_WIDTH)
    ring = outer.subtracted(gap)
    gap = gap.subtracted(body)
    if button is not None:
        # 按真实按钮圆角及描边避让，不能用含透明悬停通道的 boundingRect 裁剪。
        x, y, w, h, *transform = button
        clip = QTransform(*transform).map(_rounded_rect(QRectF(x, y, w, h), 10.5))
        gap = gap.subtracted(clip)
        ring = ring.subtracted(clip)
    bounds = rect.adjusted(-_REPAINT_PADDING, -_REPAINT_PADDING, _REPAINT_PADDING, _REPAINT_PADDING)
    return gap, ring, bounds


class SelectionDecoration:
    """合并一帧的屏内节点、路径及重绘范围；只由所属视图在 GUI 线程调用。"""

    def __init__(self, view):
        self.view = view
        self._signature = None
        self._frame = []
        self.bounds = QRect()

    def invalidate(self):
        """使屏内快照失效，保留局部路径缓存；不立即扫描或发起递归重绘。"""
        self._signature = None
        self._frame = []

    def frame(self, pixel_ratio):
        """返回本帧外框及联合范围；绘制与脏区计算复用一次屏内节点查询。

        只枚举当前实例化节点，不读取完整逻辑选择或创建屏外图元。返回项借用
        Qt 图元，生命周期限于同一 GUI 帧；场景、选择及视图变换后重新计算。
        """
        view, owner = self.view, self.view.owner
        if owner._disposed:
            self.bounds = QRect()
            return ()
        transform = view.viewportTransform()
        matrix = (transform.m11(), transform.m12(), transform.m21(), transform.m22())
        viewport = QRectF(view.viewport().rect())
        editor_id = owner.editor.node_id if owner.editor is not None else None
        signature = (matrix, transform.dx(), transform.dy(), viewport.width(), viewport.height(),
                     pixel_ratio, owner._selection_revision, editor_id)
        if signature == self._signature:
            return self._frame
        frame, bounds = [], QRectF()
        for key, item in owner.node_items.items():
            if (not owner.is_node_selected(key) and key != editor_id or item.node.is_image_node
                    or not item.isVisible() or item.effectiveOpacity() <= 0):
                continue
            mapping = item.deviceTransform(transform)
            visual = item.visual
            # 先作廉价相交判断，预取区域和固定操作节点位于屏外时不构造外框路径。
            margin = _REPAINT_PADDING + max(visual.border_width * mapping.m11() / 2, 0.5 / pixel_ratio)
            rect = mapping.mapRect(QRectF(0, 0, visual.width, visual.height))
            if not viewport.intersects(rect.adjusted(-margin, -margin, margin, margin)):
                continue
            origin = QPointF(mapping.dx(), mapping.dy())
            button = None
            control = item.fold_control
            if control is not None and control.isVisible():
                box = control.button_rect().adjusted(-0.5, -0.5, 0.5, 0.5)
                cm = control.deviceTransform(transform)
                button = (box.x(), box.y(), box.width(), box.height(), cm.m11(), cm.m12(), cm.m21(), cm.m22(),
                          cm.dx() - origin.x(), cm.dy() - origin.y())
            local = (mapping.m11(), mapping.m12(), mapping.m21(), mapping.m22())
            gap, ring, dirty = _outline_paths(visual.width, visual.height, visual.border_width,
                                              local, pixel_ratio, button)
            bounds = bounds.united(dirty.translated(origin).intersected(viewport))
            frame.append((item, origin, gap, ring))
        self._signature, self._frame = signature, frame
        self.bounds = bounds.toAlignedRect()
        return frame


def selection_bounds(view):
    """返回含抗锯齿余量且裁到视口的外框联合范围；与本帧绘制复用可见节点快照。

    空选择返回空 QRect；只读借用存活视图，仅在 GUI 线程调用，不修改文档或图元。
    调用方需同时重绘上次范围，清理移动、缩短和取消选择后遗留的外框。
    """
    view._selection_decoration.frame(view.viewport().devicePixelRatioF())
    return QRect(view._selection_decoration.bounds)


def paint_node_selection(view, painter: QPainter) -> None:
    """绘制 3 像素间隔及 2 像素外框，保持主题、编辑高亮及拖动透明度，返回 None。

    view、painter 仅在本次 GUI 绘制借用；外框在视口坐标绘制，10%～400% 缩放及
    高 DPI 均保持逻辑线宽。图片继续使用原有轮廓和手柄；结束或异常时恢复画笔。
    本接口不参与导出、不改变图元边界，只读借用缓存路径，平移时仅平移画笔。
    """
    frame = view._selection_decoration.frame(painter.device().devicePixelRatioF())
    background = view.scene().backgroundBrush()
    painter.save()
    try:
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        opacity = painter.opacity()
        for item, origin, gap, ring in frame:
            painter.setWorldTransform(QTransform.fromTranslate(origin.x(), origin.y()))
            painter.setOpacity(opacity * item.effectiveOpacity())
            painter.fillPath(gap, background)
            painter.fillPath(ring, QColor(item.theme.accent))
    finally:
        painter.restore()
