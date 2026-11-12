"""以工作台主题绘制有向依赖图，并管理单个标签的视口和模块选中状态。"""

import math
from collections.abc import Callable
from html import escape

from PySide6.QtCore import QEvent, QPoint, QPointF, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFocusEvent,
    QFontMetrics,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ..document import ModuleDependency, SoftwareStructure
from .geometry import build_geometry
from .layout import find_cyclic_dependencies

_MAX_ZOOM = 4.0
_MODULE_ID_ROLE = 0
_VIEW_STATE_VERSION = 6


class ModuleDependencyCanvas(QGraphicsView):
    """在固定布局中浏览依赖图，每个标签独立管理视口、临时预览和选中状态。"""

    viewport_changed = Signal()
    # 筛选可用性或临时预览变更后发送，供活动标签的状态栏同步图标。
    dependency_view_changed = Signal()

    def __init__(self, document: SoftwareStructure):
        """先布置完整依赖图，再应用文件筛选。首次显示时按实际视口计算缩放。"""
        super().__init__()
        self._cyclic_dependencies = find_cyclic_dependencies(document)
        self._hidden_dependencies: frozenset[ModuleDependency] = frozenset()
        self._show_all = False
        self._selected_module: str | None = None
        self._gesture_button = Qt.MouseButton.NoButton
        self._press_position: QPoint | None = None
        self._pressed_module: str | None = None
        self._pan_position: QPoint | None = None
        self._theme_color: Callable[[str], QColor] | None = None
        self._nodes = {}
        self._edges: dict[ModuleDependency, tuple[QGraphicsPathItem, QGraphicsPolygonItem]] = {}
        self._empty_label = None
        self._has_modules = bool(document.modules)
        self._initial_view_pending = True
        self._restored_viewport: tuple[float, QPointF] | None = None
        self._initial_view_timer = QTimer(self)
        self._initial_view_timer.setSingleShot(True)
        self._initial_view_timer.timeout.connect(self._apply_initial_view)

        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setAccessibleName("模块依赖图")
        scene = QGraphicsScene(self)
        self.setScene(scene)
        if self._has_modules:
            self._draw_graph(scene, document)
            self.setAccessibleDescription(
                "滚轮缩放，右键拖动平移。左键选中模块并高亮可见的直接依赖线，点击空白处清除选中。"
                "虚线表示循环依赖。可在右侧「连线筛选」面板隐藏与模块相连的依赖线。"
            )
        else:
            self._empty_label = scene.addSimpleText("文件中暂无模块", self.font())
            self.setAccessibleDescription(self._empty_label.text())
        # 在隐藏图元前固定完整图的边界，适配窗口和视口导航始终采用同一套坐标。
        self._content_rect = scene.itemsBoundingRect().adjusted(-24, -24, 24, 24)
        self.setSceneRect(self._content_rect)
        self.apply_edge_filter(document)

        for scrollbar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            scrollbar.valueChanged.connect(self._notify_viewport_changed)

    def _draw_graph(self, scene: QGraphicsScene, document: SoftwareStructure) -> None:
        """用完整依赖布局，创建此标签复用的卡片、曲线和箭头图元。"""
        modules = {module.id: module for module in document.modules}
        labels = {module.id: " ".join(module.name.splitlines()) for module in modules.values()}
        font = self.font()
        metrics = QFontMetrics(font)
        width_metrics = QFontMetricsF(font)
        height = max(38, metrics.height() + 18)
        sizes = {}
        for module_id, label in labels.items():
            # 省略处理按带小数的字宽判断是否放得下。先向上取整，再加左右留白。
            width = max(120, min(252, math.ceil(width_metrics.horizontalAdvance(label)) + 28))
            sizes[module_id] = QSizeF(width, height)

        geometry = build_geometry(sizes, document.module_dependencies)
        for module_id, rect in geometry.boxes.items():
            module = modules[module_id]
            outline = QPainterPath()
            outline.addRoundedRect(rect, 8, 8)
            node = scene.addPath(outline)
            node.setData(_MODULE_ID_ROLE, module_id)
            # 子文字直接以节点为父项创建，避免在场景和节点之间转移所有权。
            label = QGraphicsSimpleTextItem(
                metrics.elidedText(labels[module_id], Qt.TextElideMode.ElideRight, int(rect.width()) - 28), node)
            label.setFont(font)
            label.setPos(rect.left() + 14, rect.center().y() - label.boundingRect().height() / 2)
            lines = [escape(module.name), f"ID：{escape(module_id)}", *map(escape, module.description.splitlines())]
            tooltip = "<br>".join(lines)
            node.setToolTip(f"<qt>{tooltip}</qt>")
            self._nodes[module_id] = node, label

        for dependency, (path, polygon) in geometry.edges.items():
            line = scene.addPath(path)
            arrow = scene.addPolygon(polygon)
            arrow.setPen(QPen(Qt.PenStyle.NoPen))
            # 高亮时从箭头尖端放大，继续对准原有的模块连接点。
            arrow.setTransformOriginPoint(polygon[0])
            line.setZValue(-2)
            arrow.setZValue(-1)
            label = "循环依赖" if dependency in self._cyclic_dependencies else "模块依赖"
            tip = f"<qt>{label}：{escape(dependency.source)} → {escape(dependency.target)}</qt>"
            line.setToolTip(tip)
            arrow.setToolTip(tip)
            self._edges[dependency] = line, arrow

    def apply_theme(self, color: Callable[[str], QColor]) -> None:
        """按宿主主题绘制当前场景，原地更新选中模块和直接相连的可见依赖。

        普通依赖使用主题强调色突出，循环依赖通过加粗警示色虚线突出。隐藏的图元
        同步更新样式，恢复显示时立即反映当前主题和选中模块。
        """
        self._theme_color = color
        background = color("editor-bg")
        self.setBackgroundBrush(background)
        text, border, fill = color("text-default"), QPen(color("editor-border")), color("editor-header-bg")
        edge_color, cycle_color, accent = color("text-muted"), color("warning"), color("control-brand-border")
        selected_fill = color("toolbar-selected-bg-active")
        selected_border = QPen(accent, 1.5)
        for module_id, (node, label) in self._nodes.items():
            selected = module_id == self._selected_module
            node.setPen(selected_border if selected else border)
            node.setBrush(selected_fill if selected else fill)
            label.setBrush(text)

        edge_pen, cycle_pen = QPen(edge_color, 1.4), QPen(cycle_color, 1.6, Qt.PenStyle.DashLine)
        edge_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        cycle_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        highlighted_pen = QPen(edge_pen)
        highlighted_pen.setColor(accent)
        highlighted_pen.setWidthF(2.6)
        highlighted_cycle_pen = QPen(cycle_pen)
        highlighted_cycle_pen.setWidthF(2.6)
        for dependency, (line, arrow) in self._edges.items():
            cyclic = dependency in self._cyclic_dependencies
            highlighted = self._selected_module in (dependency.source, dependency.target)
            if highlighted:
                pen = highlighted_cycle_pen if cyclic else highlighted_pen
                line_z, arrow_z = -0.75, -0.25
            else:
                pen = cycle_pen if cyclic else edge_pen
                line_z, arrow_z = -2, -1
            line.setPen(pen)
            arrow.setBrush(pen.color())
            arrow.setScale(1.2 if highlighted else 1.0)
            line.setZValue(line_z)
            arrow.setZValue(arrow_z)
        if self._empty_label is not None:
            self._empty_label.setBrush(edge_color)

    @property
    def has_hidden_dependencies(self) -> bool:
        """报告文件筛选能否隐藏至少一条依赖，临时预览时也报告同样的可用性。"""
        return bool(self._hidden_dependencies)

    @property
    def shows_all_dependencies(self) -> bool:
        """报告本标签是否正在临时预览全部连线，供状态栏同步按钮。"""
        return self._show_all

    def apply_edge_filter(self, document: SoftwareStructure) -> None:
        """接收共享文档已保存的筛选设置，找出两端至少一端被勾选的连线。

        自依赖和循环依赖采用同一规则。每个标签继续使用自己的预览状态，活动侧栏
        在成功保存后单独结束发起标签的预览。
        """
        hidden_ids = document.hidden_edge_module_ids
        hidden = frozenset(edge for edge in self._edges if edge.source in hidden_ids or edge.target in hidden_ids)
        if hidden == self._hidden_dependencies:
            return
        self._hidden_dependencies = hidden
        self._sync_dependency_visibility()

    def set_show_all_dependencies(self, show_all: bool) -> None:
        """在当前标签临时显示全部连线（True）或恢复文件筛选（False）。

        此操作只更新图元可见性。画布沿用当前场景、选中模块及视口，文件的勾选由
        共享文档管理。存在可隐藏的连线时才可以开启预览。
        """
        if show_all == self._show_all or (show_all and not self.has_hidden_dependencies):
            return
        self._show_all = show_all
        self._sync_dependency_visibility()

    def _sync_dependency_visibility(self) -> None:
        """原地同步每条连线的线身和箭头，全部更新后通知状态栏。"""
        for dependency, (line, arrow) in self._edges.items():
            visible = self._show_all or dependency not in self._hidden_dependencies
            line.setVisible(visible)
            arrow.setVisible(visible)
        self.dependency_view_changed.emit()

    def _module_at(self, position: QPoint) -> str | None:
        """从视口位置查找卡片标识，标题等子图元归到其父卡片。"""
        item = self.itemAt(position)
        while item is not None:
            module_id = item.data(_MODULE_ID_ROLE)
            if module_id is not None:
                return module_id
            item = item.parentItem()
        return None

    def _cancel_pointer_gesture(self) -> None:
        """清理当前按键和点击目标，并在结束平移时归还视口的光标控制。"""
        self._gesture_button = Qt.MouseButton.NoButton
        self._press_position = None
        self._pressed_module = None
        if self._pan_position is not None:
            self._pan_position = None
            self.viewport().unsetCursor()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        """切换焦点时结束进行中的拖动或点选，保留已经选中的模块。"""
        self._cancel_pointer_gesture()
        super().focusOutEvent(event)

    def viewportEvent(self, event: QEvent) -> bool:
        """鼠标捕获被系统收回时立即复位手势，再由 Qt 处理视口事件。"""
        if event.type() == QEvent.Type.UngrabMouse:
            self._cancel_pointer_gesture()
        return super().viewportEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """由最先按下的键认领手势。左右键记录起点，左键同时记录命中的卡片。"""
        if self._gesture_button != Qt.MouseButton.NoButton or event.buttons() != event.button():
            event.accept()
            return
        button = event.button()
        if not self._has_modules or button not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            super().mousePressEvent(event)
            return
        self._gesture_button = button
        self._press_position = event.position().toPoint()
        if button == Qt.MouseButton.LeftButton:
            self._pressed_module = self._module_at(self._press_position)
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """把左右键双击中的第二次按下交给同一套手势处理。"""
        if self._has_modules and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self.mousePressEvent(event)
        else:
            super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """越过系统拖动阈值后取消左键点击，或从按下位置累计右键平移量。"""
        if self._gesture_button == Qt.MouseButton.NoButton:
            super().mouseMoveEvent(event)
            return
        event.accept()
        if not event.buttons() & self._gesture_button:
            self._cancel_pointer_gesture()
            return
        start = self._press_position
        if start is None:
            return
        position = event.position().toPoint()
        distance = (position - start).manhattanLength()
        if self._gesture_button == Qt.MouseButton.LeftButton:
            if distance >= QApplication.startDragDistance():
                self._press_position = None
            return
        if self._pan_position is None:
            if distance < QApplication.startDragDistance():
                return
            self._pan_position = start
        delta = position - self._pan_position
        self._pan_position = position
        horizontal, vertical = self.horizontalScrollBar(), self.verticalScrollBar()
        horizontal.setValue(horizontal.value() - delta.x())
        vertical.setValue(vertical.value() - delta.y())
        # Qt 滚动时可能更新悬停光标，平移仍在进行时以闭手光标覆盖。
        if self._pan_position is not None:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """结束对应按键的手势，在系统阈值内提交命中同一目标的左键点击。

        点击卡片选中模块，点击空白处清除选中。右键松开时结束平移并恢复光标。
        """
        if self._gesture_button == Qt.MouseButton.NoButton:
            super().mouseReleaseEvent(event)
            return
        event.accept()
        if event.button() != self._gesture_button:
            return
        start, module_id = self._press_position, self._pressed_module
        position = event.position().toPoint()
        self._cancel_pointer_gesture()
        if (event.button() != Qt.MouseButton.LeftButton or start is None
                or (position - start).manhattanLength() >= QApplication.startDragDistance()
                or module_id != self._module_at(position)):
            return
        if module_id != self._selected_module:
            self._selected_module = module_id
            if self._theme_color is not None:
                self.apply_theme(self._theme_color)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """处理系统伴随右键手势产生的鼠标菜单事件，键盘菜单事件沿用 Qt 路由。"""
        if event.reason() == QContextMenuEvent.Reason.Mouse:
            event.accept()
            return
        super().contextMenuEvent(event)

    @property
    def zoom_factor(self) -> float:
        """读取当前视口的实际倍率，1.0 表示 100%。状态栏配合视口变化信号刷新读数。"""
        return self.transform().m11()

    def reset_zoom(self) -> None:
        """以当前查看位置为中心恢复 100%，并沿用画布的平移边界和状态通知。"""
        if not self._has_modules or self._initial_view_pending or self.zoom_factor == 1.0:
            return
        # 取完整视口的浮点中心，避免极小倍率将像素取整误差放大成明显的场景偏移。
        center = self.mapToScene(self.viewport().rect()).boundingRect().center()
        self._set_viewport(1.0, center)

    def fit_to_view(self) -> None:
        """清除待恢复的视口，居中显示整张图，最多放大到原始大小。"""
        self._restored_viewport = None
        self._initial_view_pending = True
        self._apply_initial_view()

    def capture_viewport(self) -> dict:
        """返回可序列化的缩放和中心坐标，供复制标签和会话恢复使用。"""
        if not self._has_modules:
            return {}
        viewport = (self._restored_viewport if self._initial_view_pending
                    else (self.zoom_factor, self._viewport_center()))
        if viewport is None:
            return {}
        zoom, center = viewport
        return {"version": _VIEW_STATE_VERSION, "zoom": zoom, "center": [center.x(), center.y()]}

    def restore_viewport(self, state: dict | None) -> None:
        """按文件筛选开始浏览，并在具备实际尺寸时恢复视口。

        只读取旧会话的缩放和中心。旧版场景可能采用不同的布局，中心位于完整图边界内时
        才能恢复，否则按当前窗口尺寸适配完整图。
        """
        self.set_show_all_dependencies(False)
        self._restored_viewport = None
        self._initial_view_pending = True
        version = state.get("version") if isinstance(state, dict) else None
        if self._has_modules and type(version) is int and 1 <= version <= _VIEW_STATE_VERSION:
            zoom, center = state.get("zoom"), state.get("center")
            if (isinstance(center, list) and len(center) == 2
                    and all(type(value) in (int, float) for value in (zoom, *center))):
                try:
                    zoom, x, y = map(float, (zoom, *center))
                except OverflowError:
                    pass
                else:
                    if 0 < zoom <= _MAX_ZOOM and math.isfinite(x) and math.isfinite(y):
                        center_point = QPointF(x, y)
                        if version == _VIEW_STATE_VERSION or self._content_rect.contains(center_point):
                            self._restored_viewport = zoom, center_point
        self._apply_initial_view()

    def _viewport_center(self) -> QPointF:
        return self.mapToScene(self.viewport().rect().center())

    def _fit_zoom(self) -> float:
        return min(1.0, self.viewport().width() / self._content_rect.width(),
                   self.viewport().height() / self._content_rect.height())

    def _update_scroll_area(self) -> None:
        # 预留半个视口，图形较小或处于适配比例时也能拖动。
        zoom = self.zoom_factor
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
        previous = self.zoom_factor
        steps = max(-10, min(10, delta / 120))
        zoom = min(_MAX_ZOOM, max(min(0.1, self._fit_zoom()), previous * 1.2 ** steps))
        anchor = self.mapToScene(event.position().toPoint())
        self._set_viewport(zoom, anchor + (self._viewport_center() - anchor) * (previous / zoom))
        event.accept()
