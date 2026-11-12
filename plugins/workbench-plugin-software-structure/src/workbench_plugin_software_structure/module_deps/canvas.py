"""以工作台主题绘制有向依赖图，并管理单个标签的缩放和平移。"""

import math
from collections import defaultdict
from collections.abc import Callable
from html import escape

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from ..document import ModuleDependency, SoftwareStructure
from .layout import build_layout

_COLUMN_GAP = 132.0
_ROW_GAP = 26.0
_MAX_ZOOM = 4.0


def _edge_endpoints(
    boxes: dict[str, QRectF], dependencies: list[ModuleDependency], cycles: frozenset[ModuleDependency],
) -> dict[tuple[ModuleDependency, bool], QPointF]:
    """按相邻模块的位置分配节点边缘的连接点，错开汇入同侧的箭头。"""
    sides = defaultdict(list)
    for edge in dependencies:
        source_y, target_y = boxes[edge.source].center().y(), boxes[edge.target].center().y()
        source_side, target_side = 1, -1
        if edge in cycles:
            source_side = target_side = 1 if source_y <= target_y else -1
        sides[edge.source, source_side].append((target_y, edge, False))
        sides[edge.target, target_side].append((source_y, edge, True))

    endpoints = {}
    for (module_id, side), entries in sides.items():
        box = boxes[module_id]
        x = box.right() if side > 0 else box.left()
        span = len(entries) - 1
        step = min(12, (box.height() - 18) / max(1, span))
        ordered = sorted(entries, key=lambda entry: (entry[0], entry[1].source, entry[1].target, entry[2]))
        for index, (_, edge, is_target) in enumerate(ordered):
            endpoints[edge, is_target] = QPointF(x, box.center().y() + (index - span / 2) * step)
    return endpoints


def _forward_path(start: QPointF, end: QPointF, overhead_lane: int | None) -> tuple[QPainterPath, QPointF]:
    """相邻列在间隙内连线，跨列的边绕过中间节点，返回终点处的箭头方向。"""
    path = QPainterPath(start)
    if overhead_lane is None:
        middle = (start.x() + end.x()) / 2
        before_end = QPointF(middle, end.y())
        path.cubicTo(QPointF(middle, start.y()), before_end, end)
    else:
        # 两段竖线处于列间，横向线段从节点上方通过。
        overhead_y = -_ROW_GAP - overhead_lane * 12
        gutter = _COLUMN_GAP / 4 + overhead_lane % 5 * 7
        source_x = start.x() + gutter
        target_x = end.x() - gutter
        before_end = QPointF(target_x, end.y())
        path.lineTo(source_x, start.y())
        path.lineTo(source_x, overhead_y)
        path.lineTo(target_x, overhead_y)
        path.lineTo(before_end)
        path.lineTo(end)
    return path, end - before_end


def _cycle_path(start: QPointF, end: QPointF, side: int, *, loop: bool) -> tuple[QPainterPath, QPointF]:
    """沿同一列的两侧画环内依赖。互逆边分居两侧，自环使用同侧两个端点。"""
    if loop:
        after_start = start + QPointF(58, -30)
        before_end = end + QPointF(58, 30)
    else:
        outside = QPointF(side * 52, 0)
        after_start = start + outside
        before_end = end + outside
    path = QPainterPath(start)
    path.cubicTo(after_start, before_end, end)
    return path, end - before_end


def _arrowhead(tip: QPointF, direction: QPointF) -> QPolygonF:
    length = math.hypot(direction.x(), direction.y())
    unit = direction / length
    base = tip - unit * 10
    side = QPointF(-unit.y(), unit.x()) * 4.5
    return QPolygonF([tip, base + side, base - side])


class ModuleDependencyCanvas(QGraphicsView):
    """软件结构文档中的只读画布。缩放和平移属于视图，图数据由文档共享。"""

    viewport_changed = Signal()

    def __init__(self, document: SoftwareStructure):
        """为已校验的文档建立场景，首次显示后再根据实际视口计算缩放。"""
        super().__init__()
        self._graph_scene = QGraphicsScene(self)
        self._nodes = []
        self._edges = []
        self._empty_label = None
        self._has_modules = bool(document.modules)
        self._initial_view_pending = True
        self._restored_viewport: tuple[float, QPointF] | None = None
        self._initial_view_timer = QTimer(self)
        self._initial_view_timer.setSingleShot(True)
        self._initial_view_timer.timeout.connect(self._apply_initial_view)

        self.setScene(self._graph_scene)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setAccessibleName("模块依赖图")
        if self._has_modules:
            self.setAccessibleDescription("滚轮缩放，拖动画布平移。虚线表示循环依赖。")
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self._draw_graph(document)
        else:
            self._empty_label = self._graph_scene.addSimpleText("文件中暂无模块", self.font())
            self.setAccessibleDescription(self._empty_label.text())
        self._content_rect = self._graph_scene.itemsBoundingRect().adjusted(-24, -24, 24, 24)
        self.setSceneRect(self._content_rect)

        for scrollbar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            scrollbar.valueChanged.connect(self._notify_viewport_changed)

    def _draw_graph(self, document: SoftwareStructure) -> None:
        layout = build_layout(document)
        modules = {module.id: module for module in document.modules}
        labels = {module.id: " ".join(module.name.splitlines()) for module in document.modules}
        font = self.font()
        metrics = QFontMetrics(font)
        width = max(120, min(252, max(map(metrics.horizontalAdvance, labels.values())) + 30))
        height = max(38, metrics.height() + 18)
        pitch = height + _ROW_GAP
        max_rows = max(map(len, layout.layers))
        boxes = {}
        for column, layer in enumerate(layout.layers):
            y_offset = (max_rows - len(layer)) * pitch / 2
            for row, module_id in enumerate(layer):
                module = modules[module_id]
                rect = QRectF(column * (width + _COLUMN_GAP), y_offset + row * pitch, width, height)
                boxes[module_id] = rect
                outline = QPainterPath()
                outline.addRoundedRect(rect, 8, 8)
                node = self._graph_scene.addPath(outline)
                label = self._graph_scene.addSimpleText(
                    metrics.elidedText(labels[module_id], Qt.TextElideMode.ElideRight, width - 30), font)
                label.setParentItem(node)
                label.setPos(rect.left() + 15, rect.center().y() - label.boundingRect().height() / 2)
                lines = [escape(module.name), f"ID：{escape(module.id)}", *map(escape, module.description.splitlines())]
                node.setToolTip("<qt>" + "<br>".join(lines) + "</qt>")
                self._nodes.append((node, label))

        dependencies = sorted(document.module_dependencies, key=lambda edge: (edge.source, edge.target))
        endpoints = _edge_endpoints(boxes, dependencies, layout.cyclic_dependencies)
        next_lane = 0
        for dependency in dependencies:
            source, target = boxes[dependency.source], boxes[dependency.target]
            start, end = endpoints[dependency, False], endpoints[dependency, True]
            cyclic = dependency in layout.cyclic_dependencies
            if cyclic:
                side = 1 if start.x() > source.center().x() else -1
                path, direction = _cycle_path(start, end, side, loop=dependency.source == dependency.target)
            else:
                overhead_lane = None
                if target.left() - source.right() > _COLUMN_GAP:
                    overhead_lane = next_lane
                    next_lane += 1
                path, direction = _forward_path(start, end, overhead_lane)
            line = self._graph_scene.addPath(path)
            arrow = self._graph_scene.addPolygon(_arrowhead(path.currentPosition(), direction))
            arrow.setPen(QPen(Qt.PenStyle.NoPen))
            line.setZValue(-2)
            arrow.setZValue(-1)
            label = "循环依赖" if cyclic else "模块依赖"
            tip = f"<qt>{label}：{escape(dependency.source)} → {escape(dependency.target)}</qt>"
            line.setToolTip(tip)
            arrow.setToolTip(tip)
            self._edges.append((line, arrow, cyclic))

    def apply_theme(self, color: Callable[[str], QColor]) -> None:
        """使用宿主的主题颜色更新场景样式，保留图形位置、缩放和平移。"""
        self.setBackgroundBrush(color("editor-bg"))
        text, border, fill = color("text-default"), QPen(color("editor-border")), color("editor-header-bg")
        for node, label in self._nodes:
            node.setPen(border)
            node.setBrush(fill)
            label.setBrush(text)

        edge_color, cycle_color = color("text-muted"), color("warning")
        edge_pen, cycle_pen = QPen(edge_color, 1.4), QPen(cycle_color, 1.6, Qt.PenStyle.DashLine)
        edge_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        cycle_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        for line, arrow, cyclic in self._edges:
            line.setPen(cycle_pen if cyclic else edge_pen)
            arrow.setBrush(cycle_color if cyclic else edge_color)
        if self._empty_label is not None:
            self._empty_label.setBrush(edge_color)

    def fit_to_view(self) -> None:
        """清除待恢复的视口，居中显示整张图，最多放大到原始大小。"""
        self._restored_viewport = None
        self._initial_view_pending = True
        self._apply_initial_view()

    def capture_viewport(self) -> dict:
        """返回宿主可序列化的缩放和场景中心，隐藏标签也可保留待恢复的位置。"""
        if not self._has_modules:
            return {}
        if self._initial_view_pending:
            if self._restored_viewport is None:
                return {}
            zoom, center = self._restored_viewport
        else:
            zoom, center = self.transform().m11(), self._viewport_center()
        return {"version": 1, "zoom": zoom, "center": [center.x(), center.y()]}

    def restore_viewport(self, state: dict | None) -> None:
        """恢复版本 1 的视口。缺失、无效或未知版本的状态回退到全图适配。"""
        self._restored_viewport = None
        self._initial_view_pending = True
        if (self._has_modules and isinstance(state, dict)
                and type(state.get("version")) is int and state["version"] == 1):
            zoom, center = state.get("zoom"), state.get("center")
            if (isinstance(center, list) and len(center) == 2
                    and all(type(value) in (int, float) for value in (zoom, *center))):
                try:
                    zoom, x, y = map(float, (zoom, *center))
                except OverflowError:
                    pass
                else:
                    if 0 < zoom <= _MAX_ZOOM and math.isfinite(x) and math.isfinite(y):
                        self._restored_viewport = zoom, QPointF(x, y)
        self._apply_initial_view()

    def _viewport_center(self) -> QPointF:
        return self.mapToScene(self.viewport().rect().center())

    def _fit_zoom(self) -> float:
        return min(1.0, self.viewport().width() / self._content_rect.width(),
                   self.viewport().height() / self._content_rect.height())

    def _update_scroll_area(self) -> None:
        # 预留半个视口，图形较小或处于适配比例时也能拖动。
        zoom = self.transform().m11()
        margin_x = self.viewport().width() / (2 * zoom)
        margin_y = self.viewport().height() / (2 * zoom)
        self.setSceneRect(self._content_rect.adjusted(-margin_x, -margin_y, margin_x, margin_y))

    def _set_viewport(self, zoom: float, center: QPointF) -> None:
        self.setTransform(QTransform.fromScale(zoom, zoom))
        if self._has_modules:
            self._update_scroll_area()
        bounds = self._content_rect
        self.centerOn(min(bounds.right(), max(bounds.left(), center.x())),
                      min(bounds.bottom(), max(bounds.top(), center.y())))
        self._notify_viewport_changed()

    def _apply_initial_view(self) -> None:
        if not self._initial_view_pending or not self.isVisible() or min(self.viewport().size().toTuple()) < 2:
            return
        self._initial_view_pending = False
        if self._restored_viewport is None:
            self._set_viewport(self._fit_zoom(), self._content_rect.center())
        else:
            zoom, center = self._restored_viewport
            self._restored_viewport = None
            self._set_viewport(max(zoom, min(0.1, self._fit_zoom())), center)

    def _notify_viewport_changed(self) -> None:
        if self._has_modules and not self._initial_view_pending:
            self.viewport_changed.emit()

    def showEvent(self, event) -> None:
        """标签显示后延迟一个事件循环，等分屏布局给出实际视口尺寸。"""
        super().showEvent(event)
        if self._initial_view_pending:
            self._initial_view_timer.start(0)

    def resizeEvent(self, event) -> None:
        """按新尺寸更新平移边界，已初始化的视图保留场景中心和比例。"""
        super().resizeEvent(event)
        if self._initial_view_pending:
            self._initial_view_timer.start(0)
        elif self._has_modules:
            center = self._viewport_center()
            self._update_scroll_area()
            self.centerOn(center)

    def wheelEvent(self, event) -> None:
        """以鼠标指向的场景坐标为锚点缩放，保留全图适配所需的缩小范围。"""
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not self._has_modules or self._initial_view_pending or not delta:
            event.ignore()
            return
        previous = self.transform().m11()
        steps = max(-10, min(10, delta / 120))
        zoom = min(_MAX_ZOOM, max(min(0.1, self._fit_zoom()), previous * 1.2 ** steps))
        anchor = self.mapToScene(event.position().toPoint())
        self._set_viewport(zoom, anchor + (self._viewport_center() - anchor) * (previous / zoom))
        event.accept()
