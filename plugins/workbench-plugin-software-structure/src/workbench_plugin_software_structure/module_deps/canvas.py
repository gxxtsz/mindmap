"""以工作台主题绘制有向依赖图，并管理单个标签的缩放和平移。"""

import math
from collections import Counter
from collections.abc import Callable
from html import escape

from PySide6.QtCore import QPoint, QPointF, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QApplication, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView

from ..document import SoftwareStructure
from .folding import find_public_dependencies
from .geometry import build_geometry
from .layout import find_cyclic_dependencies

_MAX_ZOOM = 4.0
_MODULE_ID_ROLE = 0
_VIEW_STATE_VERSION = 5


class ModuleDependencyCanvas(QGraphicsView):
    """以项目内的分层布局浏览只读依赖图，每个标签独立保存视口和折叠状态。"""

    viewport_changed = Signal()
    dependency_view_changed = Signal()

    def __init__(self, document: SoftwareStructure):
        """为已校验的文档建立场景，首次显示后再根据实际视口计算缩放。"""
        super().__init__()
        self._document = document
        self._cyclic_dependencies = find_cyclic_dependencies(document)
        self._dependencies = tuple(sorted(document.module_dependencies, key=lambda edge: (edge.source, edge.target)))
        self._public = find_public_dependencies(document, self._cyclic_dependencies)
        self._folded_counts = Counter(edge.target for edge in self._public.folded)
        self._show_all = False
        self._expanded_module: str | None = None
        self._pressed_public_module: str | None = None
        self._pending_public_module: str | None = None
        self._press_position = QPoint()
        self._theme_color: Callable[[str], QColor] | None = None
        self._nodes = {}
        self._edges = []
        self._public_group = None
        self._empty_label = None
        self._has_modules = bool(document.modules)
        self._initial_view_pending = True
        self._restored_viewport: tuple[float, QPointF] | None = None
        self._initial_view_timer = QTimer(self)
        self._initial_view_timer.setSingleShot(True)
        self._initial_view_timer.timeout.connect(self._apply_initial_view)
        self._public_click_timer = QTimer(self)
        self._public_click_timer.setSingleShot(True)
        self._public_click_timer.timeout.connect(self._apply_public_click)

        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setAccessibleName("模块依赖图")
        if self._has_modules:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self._draw_graph()
        else:
            scene = QGraphicsScene(self)
            self._empty_label = scene.addSimpleText("文件中暂无模块", self.font())
            self.setScene(scene)
            self.setAccessibleDescription(self._empty_label.text())
            self._update_content_rect()

        for scrollbar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            scrollbar.valueChanged.connect(self._notify_viewport_changed)

    def _draw_graph(self) -> None:
        """按当前可见依赖重新排版，Qt 保留节点说明、主题和点击处理。"""
        modules = {module.id: module for module in self._document.modules}
        public = frozenset() if self._show_all else frozenset(self._public.dependent_counts)
        dependencies = tuple(
            edge for edge in self._dependencies
            if self._show_all or edge not in self._public.folded or edge.target == self._expanded_module
        )
        hidden_count = len(self._dependencies) - len(dependencies)
        labels = {module.id: " ".join(module.name.splitlines()) for module in modules.values()}
        font = self.font()
        metrics = QFontMetrics(font)
        height = max(38, metrics.height() + 18)
        sizes = {}
        notes = {}
        for module_id in sorted(modules):
            note = ""
            if module_id in public:
                note = f"{self._public.dependent_counts[module_id]} 个模块依赖"
                if self._folded_counts[module_id]:
                    note += " · 收起" if module_id == self._expanded_module else " · 展开"
                notes[module_id] = note
            width = max(120, min(252, max(metrics.horizontalAdvance(labels[module_id]),
                                         metrics.horizontalAdvance(note)) + 28))
            sizes[module_id] = QSizeF(width, height + metrics.height() + 2 if note else height)

        # 先折叠再排版，隐藏的入边不会占用画布的走线空间。
        geometry = build_geometry(sizes, dependencies, public)

        # 先构造新场景。旧场景仍由视图持有，Qt 可以安全处理原有的悬停项和鼠标光标。
        scene = QGraphicsScene(self)
        nodes = {}
        edges = []
        public_group = None
        if geometry.public_bounds is not None:
            group_outline = QPainterPath()
            group_outline.addRoundedRect(geometry.public_bounds, 10, 10)
            group = scene.addPath(group_outline)
            group.setZValue(-3)
            heading = f"公共依赖 · 折叠 {hidden_count} 条入边" if hidden_count else "公共依赖"
            title = scene.addSimpleText(
                metrics.elidedText(heading, Qt.TextElideMode.ElideRight, int(geometry.public_bounds.width()) - 24),
                font,
            )
            title.setToolTip(heading)
            title.setPos(geometry.public_bounds.topLeft() + QPointF(12, 4))
            public_group = group, title
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
            lines = [escape(module.name), f"ID：{escape(module_id)}"]
            note_item = None
            top = rect.center().y() - label.boundingRect().height() / 2
            if module_id in public:
                note_item = QGraphicsSimpleTextItem(
                    metrics.elidedText(notes[module_id], Qt.TextElideMode.ElideRight, int(rect.width()) - 28), node)
                note_item.setFont(font)
                top -= (note_item.boundingRect().height() + 2) / 2
                note_item.setPos(rect.left() + 14, top + label.boundingRect().height() + 2)
                lines.append(f"公共依赖 · {self._public.dependent_counts[module_id]} 个模块直接依赖")
                if folded := self._folded_counts[module_id]:
                    action = "收起" if module_id == self._expanded_module else "展开"
                    lines.append(f"点击{action} {folded} 条入边")
                    node.setCursor(Qt.CursorShape.PointingHandCursor)
            label.setPos(rect.left() + 14, top)
            lines.extend(map(escape, module.description.splitlines()))
            node.setToolTip("<qt>" + "<br>".join(lines) + "</qt>")
            nodes[module_id] = node, label, note_item

        for dependency, (path, polygon) in geometry.edges.items():
            cyclic = dependency in self._cyclic_dependencies
            expanded = dependency.target == self._expanded_module and dependency in self._public.folded
            line = scene.addPath(path)
            arrow = scene.addPolygon(polygon)
            arrow.setPen(QPen(Qt.PenStyle.NoPen))
            line.setZValue(-1 if expanded else -2)
            arrow.setZValue(-0.5 if expanded else -1)
            label = "循环依赖" if cyclic else "模块依赖"
            tip = f"<qt>{label}：{escape(dependency.source)} → {escape(dependency.target)}</qt>"
            line.setToolTip(tip)
            arrow.setToolTip(tip)
            edges.append((line, arrow, cyclic, expanded))

        # 先脱离旧场景，再释放 Python 对旧图元的引用。交给 Qt 在事件循环中销毁整个旧场景，
        # 避免 clear() 先删除图元、Shiboken 随后又在清理包装对象时访问旧图元。
        previous_scene = self.scene()
        self.setScene(scene)
        self._nodes, self._edges, self._public_group = nodes, edges, public_group
        if previous_scene is not None:
            previous_scene.deleteLater()

        description = "滚轮缩放，拖动画布平移。虚线表示循环依赖。"
        if public and self.has_folded_dependencies:
            description += f"已折叠 {hidden_count} 条公共依赖入边。点击公共模块展开或收起入边。"
        self.setAccessibleDescription(description)
        if self._theme_color is not None:
            self.apply_theme(self._theme_color)
        self._update_content_rect()

    def _update_content_rect(self) -> None:
        """以当前场景实际可见的图形确定适配边界，并为缩放和平移预留外边距。"""
        self._content_rect = self.scene().itemsBoundingRect().adjusted(-24, -24, 24, 24)
        self.setSceneRect(self._content_rect)

    def apply_theme(self, color: Callable[[str], QColor]) -> None:
        """使用宿主的主题颜色更新场景样式，保留图形位置、缩放和平移。"""
        self._theme_color = color
        background = color("editor-bg")
        self.setBackgroundBrush(background)
        text, border, fill = color("text-default"), QPen(color("editor-border")), color("editor-header-bg")
        edge_color, cycle_color, accent = color("text-muted"), color("warning"), color("control-brand-border")
        selected_fill = color("toolbar-selected-bg-active")
        if self._public_group is not None:
            group, title = self._public_group
            group.setPen(border)
            group.setBrush(fill)
            title.setBrush(text)
        for module_id, (node, label, note) in self._nodes.items():
            selected = module_id == self._expanded_module
            node.setPen(QPen(accent, 1.5) if selected else border)
            node.setBrush(selected_fill if selected else background if note is not None else fill)
            label.setBrush(text)
            if note is not None:
                note.setBrush(accent if self._folded_counts[module_id] else edge_color)

        edge_pen, cycle_pen = QPen(edge_color, 1.4), QPen(cycle_color, 1.6, Qt.PenStyle.DashLine)
        expanded_pen = QPen(accent, 1.6)
        edge_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        cycle_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        for line, arrow, cyclic, expanded in self._edges:
            pen = cycle_pen if cyclic else expanded_pen if expanded else edge_pen
            line.setPen(pen)
            arrow.setBrush(pen.color())
        if self._empty_label is not None:
            self._empty_label.setBrush(edge_color)

    @property
    def has_folded_dependencies(self) -> bool:
        """报告总览是否有可展开的入边，供状态栏决定是否显示全量切换。"""
        return bool(self._public.folded)

    @property
    def shows_all_dependencies(self) -> bool:
        """读取本标签当前的全量开关，供分屏切换和会话恢复同步控件。"""
        return self._show_all

    def apply_public_dependency_thresholds(self, document: SoftwareStructure) -> None:
        """应用同一文件保存后的阈值快照，按新分组校验本标签的显示方式。"""
        self._document = document
        public = find_public_dependencies(document, self._cyclic_dependencies)
        if public == self._public:
            return
        self._public = public
        self._folded_counts = Counter(edge.target for edge in public.folded)
        show_all = self._show_all and self.has_folded_dependencies
        expanded = self._expanded_module if self._expanded_module in self._folded_counts else None
        # 总览的可见边或分组已变，沿用安全换场景流程。有效的全量模式继续使用原视口。
        if self._set_dependency_view(show_all, expanded, relayout=not show_all):
            self.fit_to_view()

    def set_show_all_dependencies(self, show_all: bool) -> None:
        """在默认总览与全量依赖之间切换，按新图的边界重新适配窗口。"""
        if self.has_folded_dependencies and self._set_dependency_view(show_all, None):
            self.fit_to_view()

    def _set_dependency_view(self, show_all: bool, expanded: str | None, *, relayout: bool = False) -> bool:
        """替换本标签显示的边并重建场景，待新边界就绪后再保存视口。"""
        # 切换模式、修改阈值或恢复会话时，取消旧场景上尚未完成的点选。
        self._public_click_timer.stop()
        self._pending_public_module = None
        self._pressed_public_module = None
        if not relayout and (show_all, expanded) == (self._show_all, self._expanded_module):
            return False
        self._show_all, self._expanded_module = show_all, expanded
        self._initial_view_pending = True
        self._draw_graph()
        self.dependency_view_changed.emit()
        return True

    def _public_module_at(self, position: QPoint) -> str | None:
        """从命中的节点、标题或计数文字向上查找可展开的公共模块。"""
        if self._show_all:
            return None
        item = self.itemAt(position)
        while item is not None:
            module_id = item.data(_MODULE_ID_ROLE)
            if module_id in self._folded_counts:
                return module_id
            item = item.parentItem()
        return None

    def mousePressEvent(self, event) -> None:
        """记录可能的公共模块点击，继续交给原有画布处理平移。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.position().toPoint()
            self._pressed_public_module = self._public_module_at(self._press_position)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """达到系统拖动阈值即取消点选，拖动经过公共模块时只平移画布。"""
        if self._pressed_public_module is not None:
            distance = (event.position().toPoint() - self._press_position).manhattanLength()
            if distance >= QApplication.startDragDistance():
                self._pressed_public_module = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """确认是同一模块上的点击，在 Qt 完成当前鼠标事件后再切换场景。"""
        module_id = self._pressed_public_module if event.button() == Qt.MouseButton.LeftButton else None
        self._pressed_public_module = None
        position = event.position().toPoint()
        activate = (module_id is not None
                    and (position - self._press_position).manhattanLength() < QApplication.startDragDistance()
                    and module_id == self._public_module_at(position))
        super().mouseReleaseEvent(event)
        if activate:
            self._pending_public_module = module_id
            self._public_click_timer.start(0)

    def _apply_public_click(self) -> None:
        """在下一次事件循环展开或收起指定模块，此时原鼠标事件已返回。"""
        module_id, self._pending_public_module = self._pending_public_module, None
        if module_id is None or self._show_all or module_id not in self._folded_counts:
            return
        expanded = None if module_id == self._expanded_module else module_id
        if self._set_dependency_view(False, expanded):
            self.fit_to_view()

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
        """保存缩放、中心和可选折叠状态，复制标签时保留当前的依赖显示方式。"""
        if not self._has_modules:
            return {}
        state = {}
        if self._show_all:
            state["show_all_dependencies"] = True
        if self._expanded_module is not None:
            state["expanded_public_module"] = self._expanded_module
        # 隐藏标签随文件阈值重排后，要等再次显示才能适配新图。先保存依赖显示方式。
        viewport = (self._restored_viewport if self._initial_view_pending
                    else (self.zoom_factor, self._viewport_center()))
        if viewport is not None:
            zoom, center = viewport
            state.update(zoom=zoom, center=[center.x(), center.y()])
        if state:
            state["version"] = _VIEW_STATE_VERSION
        return state

    def restore_viewport(self, state: dict | None) -> None:
        """先恢复可见依赖，再按该图的几何恢复视口。

        旧版状态可能保存了横向布局的场景坐标。若旧中心落在新图之外，则保留已恢复的
        依赖显示方式并适配整图。文档变动后，已移出公共分组的模块按普通模块显示。
        """
        self._restored_viewport = None
        self._initial_view_pending = True
        version = state.get("version") if isinstance(state, dict) else None
        if self._has_modules and type(version) is int and 1 <= version <= _VIEW_STATE_VERSION:
            show_all = self.has_folded_dependencies and state.get("show_all_dependencies") is True
            expanded = state.get("expanded_public_module")
            if show_all or not isinstance(expanded, str) or expanded not in self._folded_counts:
                expanded = None
            self._set_dependency_view(show_all, expanded)
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
        elif self._has_modules:
            self._set_dependency_view(False, None)
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
