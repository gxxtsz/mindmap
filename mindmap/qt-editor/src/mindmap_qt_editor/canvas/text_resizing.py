"""文字节点的水平调宽手势。临时卡片由当前视图绘制，正式几何在松手后更新。"""

from dataclasses import replace
from math import isclose

from mindmap_core import MindMapError
from mindmap_core.validation import MAX_FIXED_WIDTH, MIN_FIXED_WIDTH
from mindmap_qt_render.rendering import NODE_HORIZONTAL_PADDING, NodeVisual
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from .items import FoldControlItem, PageControlItem


class TextWidthResizeController:
    """为所属画布提供单选文字节点的宽度手柄、独立预览及一次性提交。

    本对象只在视图所属 GUI 线程使用。鼠标位置使用视口逻辑像素，文档宽度使用
    场景逻辑像素。视图销毁、切页或捕获中断前调用 cancel() 归还原图元。
    """

    def __init__(self, view):
        self.view = view
        self._item = None
        self._preview: NodeVisual | None = None

    @property
    def active(self) -> bool:
        """返回手柄是否持有本次按下，包含尚未达到拖动阈值的阶段。"""
        return self._item is not None

    def _selected_item(self):
        owner = self.view.owner
        if (owner._disposed or not owner.editable or owner.editor is not None
                or owner._applying_engine_event or owner._refreshing):
            return None
        node = owner._selected_text_node()
        return owner.node_items.get(node.id) if node is not None and node.id not in owner.graph_group_items else None

    def _viewport_rect(self, item, visual: NodeVisual) -> QRectF:
        rect = item.mapRectToScene(QRectF(0, 0, visual.width, visual.height))
        return self.view.viewportTransform().mapRect(rect)

    @staticmethod
    def _handle_rect(rect: QRectF) -> QRectF:
        # 手柄紧贴右下角外侧，给卡片正文和右侧中线的出线把手留出各自的热区。
        x = rect.right() - min(7.0, rect.width() / 4)
        return QRectF(x - 7, rect.bottom() + 2, 14, 12)

    def _current_handle_rect(self) -> QRectF:
        item = self._item if self.active else self._selected_item()
        if item is None or not isValid(item):
            return QRectF()
        return self._handle_rect(self._viewport_rect(item, self._preview or item.visual))

    def decoration_bounds(self) -> QRect:
        """返回覆盖可见手柄的视口重绘矩形，供选择装饰清理上一帧。"""
        return self._current_handle_rect().toAlignedRect().intersected(self.view.viewport().rect())

    def hit_test(self, position: QPoint | None) -> bool:
        """查询视口点是否命中宽度手柄，附页、折叠和出线把手优先各自响应。"""
        if (position is None or not self._current_handle_rect().contains(QPointF(position))
                or not self.view.selected_handle_visible(position)):
            return False
        hit = self.view.itemAt(position)
        if isinstance(hit, PageControlItem) or self.view.graph_edge_edit.handle_at(position) is not None:
            return False
        return not (isinstance(hit, FoldControlItem) and hit.contains_button(self.view.mapToScene(position)))

    def begin(self, position: QPoint) -> None:
        """认领命中手柄的左键，冻结正式视觉与版本，等横向位移达到系统拖动阈值。

        先转移焦点并提交有效的侧栏草稿。期间选择或命中几何发生变化时放弃开始。
        权限及输入法错误由画布统一报告。开始阶段不改变节点或绘制原卡片的透明度。
        """
        owner = self.view.owner
        item = self._selected_item()
        if item is None:
            return
        target = (owner.engine.document_generation, owner.current_page_id, item.node.id)
        self.view.setFocus(Qt.FocusReason.MouseFocusReason)
        node = owner._prepare_text_appearance()
        item = self._selected_item()
        if (item is None or target != (owner.engine.document_generation, owner.current_page_id, node.id)
                or not self.hit_test(position)):
            return
        self._item = item
        self._target = target
        self._revision = owner.engine.revision
        self._press = QPoint(position)
        self._zoom = self.view.transform().m11()
        self._base_width = item.visual.width - item.visual.page_controls_width - 2 * NODE_HORIZONTAL_PADDING
        self._opacity = item.opacity()
        self._moved = False
        self._candidate = None

    def update(self, position: QPoint) -> None:
        """用相对起点的横向场景位移试算一张卡片，保持正式布局和文档不变。

        位置失效、页面或文档修订号变化时取消。每个候选宽度只测一次，预览结果
        不进入正式视觉缓存。高度按正文、标签和附件的实际排版计算。
        """
        owner = self.view.owner
        item = self._item
        if (item is None or not isValid(item) or owner._disposed or not owner.editable
                or owner.editor is not None or owner.engine.revision != self._revision
                or self.view.transform().m11() != self._zoom
                or self._target != (owner.engine.document_generation, owner.current_page_id, item.node.id)):
            self.cancel()
            return
        if not self._moved:
            self._moved = abs(position.x() - self._press.x()) >= QApplication.startDragDistance()
            if not self._moved:
                return
        delta = (position.x() - self._press.x()) / self._zoom
        width = max(MIN_FIXED_WIDTH, min(MAX_FIXED_WIDTH, round(self._base_width + delta)))
        # 自动卡片可能已经窄于手动最小值。向这种边界继续拖动时保留原卡片。
        candidate = width if (width - self._base_width) * delta > 0 else None
        if candidate == self._candidate:
            return
        self._candidate = candidate
        preview = None
        if candidate is not None:
            document = owner._document
            node = replace(item.node, style={**item.node.style, "fixed_width": candidate})
            in_graph = owner.graph is not None
            preview = owner.factory.visual(
                node, document.theme, document, is_root=not in_graph and node.id in document.root_ids,
                detail_page_resolver=None if in_graph else owner.engine.resolve_detail_page, cache=False,
            )
            if isclose(preview.width, item.visual.width, abs_tol=0.5):
                preview = None
        dirty = self._dirty_rect(item, self._preview, preview)
        self._preview = preview
        item.setOpacity(0 if preview is not None else self._opacity)
        self.view.viewport().update(dirty)

    def finish(self, position: QPoint) -> None:
        """按最终视口坐标结束手势，有有效变化时向引擎提交一次宽度修改。

        先清理临时视觉，再写入树节点或当前图节点。点击、回到起点或触及无法再
        调整的边界都不产生历史。输入法候选未确认时取消本次宽度修改。
        """
        self.update(position)
        if self._preview is None:
            self.cancel()
            return
        node_id, width = self._item.node.id, self._candidate
        self.cancel()
        owner = self.view.owner
        if not all(guard() for guard in tuple(owner._navigation_guards)):
            raise MindMapError("请先完成输入法候选，再调整节点宽度")
        owner._set_node_width(node_id, width)

    def _dirty_rect(self, item, *previews) -> QRect:
        rect = self._viewport_rect(item, item.visual)
        for visual in previews:
            if visual is not None:
                rect = rect.united(self._viewport_rect(item, visual))
        return rect.adjusted(-16, -16, 16, 16).toAlignedRect()

    def cancel(self) -> None:
        """幂等归还图元并清除预览，不提交宽度、不改变选择或撤销历史。"""
        item, self._item = self._item, None
        preview, self._preview = self._preview, None
        if preview is not None:
            if isValid(item):
                item.setOpacity(self._opacity)
                self.view.viewport().update(self._dirty_rect(item, preview))
            else:
                self.view.viewport().update()

    def paint(self, painter: QPainter) -> None:
        """在当前视图前景绘制已选卡片的手柄及实时预览，恢复借用画笔的状态。

        正文按场景坐标绘制，手柄按视口逻辑像素绘制。绘制过程不改动正式场景，
        因此其他视图和图形导出读取的仍是引擎已提交的节点状态。
        """
        handle = self._current_handle_rect()
        if handle.isEmpty():
            return
        painter.save()
        try:
            bounds = handle if self._preview is None else handle.united(self._viewport_rect(self._item, self._preview))
            self.view.clip_graph_decoration(painter, bounds.adjusted(-2, -2, 2, 2))
            accent = self.view.owner._document.theme.accent
            if self._preview is not None:
                item = self._item
                painter.save()
                try:
                    painter.translate(item.scenePos())
                    self._preview.paint(painter, item.node, selected=True, controls=False, accent=accent)
                finally:
                    painter.restore()
            painter.resetTransform()
            painter.setPen(QPen(QColor(accent), 1.5))
            painter.setBrush(self.view.scene().backgroundBrush())
            painter.drawRoundedRect(handle.adjusted(2, 3, -2, -3), 2, 2)
        finally:
            painter.restore()
