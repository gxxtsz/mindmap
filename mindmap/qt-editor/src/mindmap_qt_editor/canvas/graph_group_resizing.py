"""以视口手柄调整图分组的最小宽高。"""

from dataclasses import dataclass, replace

from mindmap_core import LayoutResult, MindMapError, NodeGraph
from mindmap_core.geometry import Point
from mindmap_core.layouts.graph import fit_graph_groups
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication

from .items import PageControlItem


@dataclass(frozen=True, slots=True)
class _Resize:
    """冻结按下时的页面身份、图及场景，直到提交或取消。"""

    key: str
    graph: NodeGraph
    target: tuple[int, str]
    layout: LayoutResult
    press: QPoint
    scale: float


class GraphGroupResizeController:
    """单选组节点时提供右下角手柄，实时试算尺寸及受影响的连线。

    只在画布 GUI 线程使用。鼠标位置以视口逻辑像素传入，保存值使用场景逻辑像素。
    正式图在释放前保持原值，失焦、切页、只读或文档变化由视图调用 cancel。
    """

    def __init__(self, view):
        self.view = view
        self._drag: _Resize | None = None
        self._minimum: Point | None = None

    @property
    def active(self) -> bool:
        """询问手柄是否认领本次按下，包含尚未超过系统拖动阈值的阶段。"""
        return self._drag is not None

    def _selected_key(self) -> str | None:
        owner = self.view.owner
        if (owner.graph is None or not owner.editable or owner.is_editing or owner._disposed
                or owner._applying_engine_event or owner._refreshing or owner.selection_count != 1):
            return None
        key = owner.current_node_id()
        return key if key in owner.layout_result.group_frames else None

    def _handle_rect(self) -> QRectF:
        key = self._drag.key if self._drag is not None else self._selected_key()
        frame = self.view.owner.layout_result.group_frames.get(key)
        if frame is None:
            return QRectF()
        box = frame.bounds
        corner = self.view.viewportTransform().map(QPointF(box.right, box.bottom))
        return QRectF(corner.x() - 7, corner.y() - 7, 14, 14)

    def decoration_bounds(self) -> QRect:
        """返回手柄占用的可见范围，供视口清除上一帧的选择装饰。"""
        return self._handle_rect().toAlignedRect().intersected(self.view.viewport().rect())

    def hit_test(self, position: QPoint | None) -> bool:
        """判断是否命中手柄，仍让重叠的卡片、页按钮和接线手柄响应自身手势。"""
        return (position is not None and self._handle_rect().contains(QPointF(position))
                and self.view.selected_handle_visible(position)
                and not isinstance(self.view.itemAt(position), PageControlItem)
                and self.view.graph_edge_edit.handle_at(position) is None)

    def begin(self, position: QPoint) -> None:
        """认领手柄并冻结图和缩放倍率，输入确认期间变化的命中不继续处理。"""
        owner = self.view.owner
        key = self._selected_key()
        if key is None or not self.hit_test(position):
            return
        target = owner._graph_target()
        self.view.setFocus(Qt.FocusReason.MouseFocusReason)
        owner._prepare_page_navigation()
        if key != self._selected_key() or target != owner._graph_target() or not self.hit_test(position):
            return
        self._drag = _Resize(key, owner.graph, target, owner.layout_result, QPoint(position),
                             self.view.transform().m11())

    def update(self, position: QPoint) -> None:
        """以外框左上角为基准试算宽高，缩小到内容所需尺寸时停止。"""
        drag = self._drag
        if drag is None:
            return
        delta = position - drag.press
        frame = drag.layout.group_frames[drag.key]
        base = frame.bounds.width, frame.bounds.height
        minimum = None
        if delta.manhattanLength() >= QApplication.startDragDistance():
            size = (max(frame.content_size[0], base[0] + delta.x() / drag.scale),
                    max(frame.content_size[1], base[1] + delta.y() / drag.scale))
            if size != base:
                minimum = size
        if minimum == self._minimum:
            return
        self._minimum = minimum
        result = drag.layout
        if minimum is not None:
            groups = tuple(replace(group, min_size=minimum) if group.id == drag.key else group
                           for group in drag.graph.groups)
            positions = {key: (rect.x, rect.y) for key, rect in result.nodes.items()}
            result = fit_graph_groups(result, drag.graph.edges, groups, positions)
        self.view.owner._show_edit_layout(result)
        self.view.viewport().update()

    def finish(self, position: QPoint) -> None:
        """结束预览，并仅在尺寸变化且原图仍有效时写入一次事务。"""
        self.update(position)
        drag, minimum = self._drag, self._minimum
        self.cancel()
        if drag is not None and minimum is not None:
            owner = self.view.owner
            if not all(guard() for guard in tuple(owner._navigation_guards)):
                raise MindMapError("请先完成输入法候选，再调整组框")
            owner._set_graph_group_size(drag.key, minimum, target=drag.target, expected=drag.graph)

    def cancel(self) -> None:
        """丢弃尺寸草稿并恢复正式组框和连线，重复调用安全。"""
        drag, self._drag = self._drag, None
        minimum, self._minimum = self._minimum, None
        if drag is not None and minimum is not None and not self.view.owner._disposed:
            self.view.owner._show_edit_layout(drag.layout)
            self.view.viewport().update()

    def paint(self, painter: QPainter) -> None:
        """在视口绘制固定屏幕尺寸的角手柄，导出场景无需此装饰。"""
        handle = self._handle_rect()
        if handle.isEmpty():
            return
        painter.save()
        try:
            self.view.clip_graph_decoration(painter, handle)
            painter.resetTransform()
            painter.setPen(QPen(QColor(self.view.owner.ui_colors.accent), 1.5))
            painter.setBrush(self.view.scene().backgroundBrush())
            painter.drawRoundedRect(handle.adjusted(3, 3, -3, -3), 2, 2)
        finally:
            painter.restore()
