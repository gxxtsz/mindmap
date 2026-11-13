"""图页的选线、拖线和高亮。前景装饰属于视口，正式连线由场景和引擎管理。"""

from collections.abc import Iterable
from dataclasses import dataclass
from math import hypot, sqrt

from mindmap_core import GraphEdge, NodeGraph
from mindmap_qt_render.rendering import branch_arrow_path
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QRubberBand

from ..theme import graph_edge_colors
from .items import NodeItem, PageControlItem

# 使用视口逻辑像素。命中区略大于圆点，节点和把手之间留连续的悬停通道。
_RADIUS = 5.0
_HIT_RADIUS = 7.0
_OUTLET_OFFSET = 12.0
_ENDPOINT_GAP = 2 * _HIT_RADIUS + 4


@dataclass(frozen=True, slots=True)
class _Handle:
    """把手的视口中心和原始锚点，以及拖动时留在原处的节点。"""

    center: QPointF
    anchor: QPointF
    fixed_node: str
    original: GraphEdge | None = None
    moving_source: bool = False


@dataclass(frozen=True, slots=True)
class _EdgeDrag:
    """冻结按下时的图及页面身份。移动期间只试算一条候选边，不写引擎。"""

    handle: _Handle
    target: tuple[int, str]
    graph: NodeGraph
    edges: frozenset[GraphEdge]
    press: QPoint


def _border_toward(rect: QRectF, point: QPointF) -> QPointF:
    """沿矩形中心到指针的方向取边界交点，预览箭头贴在卡片外沿。"""
    center = rect.center()
    delta = point - center
    ratio = max(abs(delta.x()) / rect.width(), abs(delta.y()) / rect.height())
    return center + delta / (2 * ratio) if ratio else QPointF(rect.right(), center.y())


class GraphEdgeEditController:
    """每个共用视图拥有独立的连线选择、把手和预览，只在 GUI 线程读写。"""

    def __init__(self, view):
        self.view = view
        self.owner = view.owner
        self._selected_edges: frozenset[GraphEdge] = frozenset()
        self._related_edges: tuple[GraphEdge, ...] = ()
        self._related_edges_source: tuple[GraphEdge, ...] | None = None
        self._related_selection_revision = -1
        self._outlet_node: str | None = None
        self._drag: _EdgeDrag | None = None
        self._box_origin: QPoint | None = None
        self._box_additive = False
        self._rubber_band: QRubberBand | None = None
        self._pointer = QPoint()
        self._moved = False
        self.owns_left_button = False

    @property
    def selected_edges(self) -> frozenset[GraphEdge]:
        """借用本标签明确选中的不可变连线集合，供动作、菜单和前景绘制查询。"""
        return self._selected_edges

    @property
    def selected_edge(self) -> GraphEdge | None:
        """仅在选中一条连线时返回该边，供改接把手使用。"""
        return next(iter(self._selected_edges)) if len(self._selected_edges) == 1 else None

    @property
    def active(self) -> bool:
        """按下改接把手或开始框选后即为活动手势，包含尚未超过拖动阈值的阶段。"""
        return self._drag is not None or self._box_origin is not None

    def _can_edit(self) -> bool:
        owner = self.owner
        return (owner.graph is not None and not owner._disposed and owner.editable and owner.editor is None
                and not owner._refreshing)

    def _node_rect(self, key: str) -> QRectF:
        rect = self.owner.layout_result.nodes[key]
        return self.view.viewportTransform().mapRect(QRectF(rect.x, rect.y, rect.width, rect.height))

    def _outlet(self) -> _Handle | None:
        key = self._outlet_node
        if key is None or key not in self.owner.layout_result.nodes:
            return None
        rect = self._node_rect(key)
        anchor = QPointF(rect.right(), rect.center().y())
        return _Handle(anchor + QPointF(_OUTLET_OFFSET, 0), anchor, key)

    def _handles(self) -> tuple[_Handle, ...]:
        """以视口坐标返回可操作把手。短边的把手移到连线两侧，避开节点正文。"""
        if not self._can_edit() or self.active:
            return ()
        edge = self.selected_edge
        handles = ()
        if edge is not None:
            branch = self.owner.layout_result.branches.get(edge.key)
            if branch is not None:
                transform = self.view.viewportTransform()
                start = transform.map(QPointF(*branch.start))
                end = transform.map(QPointF(*branch.end))
                delta = end - start
                length = hypot(delta.x(), delta.y())
                offset = QPointF()
                down = QPointF()
                if length < _ENDPOINT_GAP:
                    # 沿锚点连线的垂线错开。在小倍率下沿原方向延伸容易挡住卡片正文。
                    perpendicular = QPointF(-delta.y(), delta.x()) / length if length else QPointF(1, 0)
                    offset = perpendicular * (sqrt(_ENDPOINT_GAP ** 2 - length ** 2) / 2)
                    if edge.source == edge.target:
                        # 自环的两个锚点都在卡片下沿。一起下移，避免一个把手进入卡片。
                        down = QPointF(0, abs(offset.y()))
                handles = (_Handle(start - offset + down, start, edge.target, edge, True),
                           _Handle(end + offset + down, end, edge.source, edge))
        outlet = self._outlet()
        return (*handles, outlet) if outlet is not None else handles

    def handle_at(self, position) -> _Handle | None:
        """命中视口前景中的圆形热区。已选连线的端点优先于节点出线把手。"""
        if position is None or not self.view.viewport().rect().contains(position):
            return None
        for handle in self._handles():
            delta = QPointF(position) - handle.center
            if delta.x() ** 2 + delta.y() ** 2 <= _HIT_RADIUS ** 2:
                return handle
        return None

    def update_hover(self, position, item, *, blocked: bool):
        """节点正文、连接短线及把手共享悬停范围；离开后回退到单选节点。"""
        previous = self._outlet_node
        if blocked or self.owns_left_button or not self._can_edit():
            self._outlet_node = None
        else:
            outlet = self._outlet()
            corridor = QRectF()
            if outlet is not None:
                corridor = QRectF(outlet.anchor.x() - 1, outlet.anchor.y() - _HIT_RADIUS,
                                  _OUTLET_OFFSET + _HIT_RADIUS + 1, 2 * _HIT_RADIUS)
            if position is None or not corridor.contains(QPointF(position)):
                self._outlet_node = (item.node.id if isinstance(item, NodeItem) else
                                     self.owner.current_node_id() if self.owner.selection_count == 1 else None)
        if self._outlet_node != previous:
            self.view.viewport().update()
        if self.active or (not blocked and self.handle_at(position) is not None):
            return Qt.CursorShape.CrossCursor
        return None

    def select_edge(self, edge: GraphEdge, *, toggle: bool = False) -> None:
        """单选当前图中的可见边；toggle 为真时增减这一条边。

        新选择按当前场景过滤，实际选线会清空节点选择。选线只更新本标签和动作状态。
        """
        edges = self._selected_edges ^ {edge} if toggle else (edge,)
        self._select_edges(edges)

    def _select_edges(self, edges: Iterable[GraphEdge], *, clear_nodes: bool = False) -> None:
        selected = frozenset(edge for edge in edges if edge.key in self.owner.branch_items)
        if (selected or clear_nodes) and self.owner.selection_count:
            self.owner.select_nodes([])
        if selected == self._selected_edges:
            return
        self._selected_edges = selected
        self.owner._update_actions()
        self.view.viewport().update()
        self.owner.graph_edge_selection_changed.emit(len(selected))

    def clear_selection(self) -> None:
        """丢弃本视图的选线，其他标签及文档历史不受影响。"""
        if self._selected_edges:
            self._select_edges(())

    def sync_visibility(self) -> None:
        """更新布局后只剔除已删除的选择，并请求按当前几何重绘高亮。"""
        if self._selected_edges:
            self._select_edges(self._selected_edges)
        if self.owner.graph is not None and (self._selected_edges or self.owner.selection_count):
            self.view.viewport().update()

    def press_left(self, position: QPoint, modifiers: Qt.KeyboardModifier) -> bool:
        """认领按下的改接、Ctrl 点选或 Shift 框选手势，返回是否消费左键。

        普通点击优先命中把手和节点，Ctrl 点选跳过改接把手，Shift 在空白处按下
        进入选线框。其他点击交给原有节点选择和节点框选。
        """
        self.owns_left_button = False
        if self.owner.graph is None or self.owner.is_editing:
            return False
        item = self.view.itemAt(position)
        if isinstance(item, PageControlItem):
            return False
        toggle = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        box = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        handle = None if toggle or box else self.handle_at(position)
        if handle is not None:
            graph, target = self.owner.graph, self.owner._graph_target()
            self.view.setFocus(Qt.FocusReason.MouseFocusReason)
            # 分屏抢到焦点时，旧标签可能先提交文字草稿。放弃这次过期命中，仍接收配对释放。
            if self._can_edit() and self.owner.graph is graph:
                self._drag = _EdgeDrag(handle, target, graph, frozenset(graph.edges), QPoint(position))
                self._pointer = QPoint(position)
                self._moved = False
        else:
            edge = None if isinstance(item, NodeItem) else self.owner.graph_edge_at(position)
            if edge is None:
                if not box or isinstance(item, NodeItem) or self.handle_at(position) is not None:
                    self.clear_selection()
                    return False
                self.view.setFocus(Qt.FocusReason.MouseFocusReason)
                self._box_origin = QPoint(position)
                self._box_additive = toggle
                self._moved = False
            else:
                self.view.setFocus(Qt.FocusReason.MouseFocusReason)
                self.select_edge(edge, toggle=toggle)
        self.owns_left_button = True
        return True

    def _box_rect(self, position: QPoint) -> QRect:
        """取视口内的规范矩形，拖到视口外时以可见区域为界。"""
        return QRect(self._box_origin, position).normalized().intersected(self.view.viewport().rect())

    def move(self, position: QPoint) -> None:
        """越过系统拖动阈值后更新临时虚线或选线框，松手前保持原选择。"""
        if self._drag is not None:
            self._pointer = QPoint(position)
            self._moved |= (position - self._drag.press).manhattanLength() >= QApplication.startDragDistance()
            self.view.viewport().update()
        elif self._box_origin is not None:
            self._moved |= (position - self._box_origin).manhattanLength() >= QApplication.startDragDistance()
            if self._moved:
                if self._rubber_band is None:
                    self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.view.viewport())
                    self._rubber_band.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                self._rubber_band.setGeometry(self._box_rect(position))
                self._rubber_band.show()

    def _candidate(self, drag: _EdgeDrag, position) -> tuple[str, GraphEdge] | None:
        if not self._moved or not self.view.viewport().rect().contains(position):
            return None
        item = self.view.itemAt(position)
        # 入口图标属于卡片。拖线已经认领手势，松手时将图标区域也视为目标节点。
        if isinstance(item, PageControlItem):
            item = item.node_item
        if not isinstance(item, NodeItem):
            return None
        key = item.node.id
        handle = drag.handle
        edge = (GraphEdge(key, handle.fixed_node) if handle.moving_source else GraphEdge(handle.fixed_node, key))
        return (key, edge) if edge not in drag.edges else None

    def release(self, position: QPoint) -> None:
        """以松手坐标重做命中，清除预览后更新选线或提交一次改接。"""
        self.owns_left_button = False
        if self._box_origin is not None:
            self.move(position)
            rect = self._box_rect(position) if self._moved else QRect()
            additive = self._box_additive
            self.cancel()
            if not rect.isEmpty():
                edges = self.owner._graph_edges_in_rect(rect)
                if additive:
                    edges |= self._selected_edges
                self._select_edges(edges, clear_nodes=not additive)
            return
        drag = self._drag
        if drag is None:
            return
        self.move(position)
        candidate = self._candidate(drag, position)
        self.cancel()
        if candidate is not None and self._can_edit():
            _, edge = candidate
            if self.owner._commit_graph_edge(edge, original=drag.handle.original, target=drag.target,
                                             expected=drag.graph):
                self.select_edge(edge)

    def cancel(self) -> None:
        """丢弃预览、框选和悬停。已认领的左键释放仍由视图消费，保留已完成的选择。"""
        redraw = self._drag is not None or self._outlet_node is not None
        self._drag = self._outlet_node = None
        self._box_origin = None
        if self._rubber_band is not None:
            self._rubber_band.hide()
        self._moved = False
        if redraw:
            self.view.viewport().update()

    def _edges_to_highlight(self) -> Iterable[GraphEdge]:
        """仅在拓扑或节点选择变化时查找关联边，平移和缩放复用结果。"""
        if self._selected_edges:
            return self._selected_edges
        owner = self.owner
        if not owner.selection_count:
            return ()
        edges = owner.graph.edges
        revision = owner._selection_revision
        if edges is not self._related_edges_source or revision != self._related_selection_revision:
            self._related_edges = tuple(edge for edge in edges if owner.is_node_selected(edge.source)
                                        or owner.is_node_selected(edge.target))
            self._related_edges_source = edges
            self._related_selection_revision = revision
        return self._related_edges

    def _paint_highlights(self, painter: QPainter, color: QColor, *, outline: QColor | None = None) -> None:
        """在视口坐标中绘制高亮线身和箭头，并裁去被卡片或编辑器遮住的部分。

        裁剪使用卡片实际绘制的圆角与边框，端口外侧的线身和箭头尖端均能完整上色。
        节点关联边只叠加主题色。直接选线使用更粗的描边，并先画一圈画布底色，
        让选中的线在交叉处与普通线分开。整组选线共用两次绘制顺序，先画完外缘再
        画所有线身和箭头，避免一条选线的外缘截断另一条选线。
        """
        edges = self._edges_to_highlight()
        if not edges:
            return
        viewport = QRectF(self.view.viewport().rect())
        view_transform = self.view.viewportTransform()
        zoom = self.owner.zoom_factor
        minimum, extra = (4.5, 3.0) if outline is not None else (2.75, 1.25)
        outline_width = 2.5 if outline is not None else 0.0
        visible = []
        for edge in edges:
            item = self.owner.branch_items.get(edge.key)
            if item is None:
                continue
            branch = self.owner.layout_result.branches[edge.key]
            width = max(minimum, branch.width * zoom + extra)
            transform = item.deviceTransform(view_transform)
            margin = (width + outline_width) / 2 + 1
            bounds = transform.mapRect(item.boundingRect()).adjusted(-margin, -margin, margin, margin)
            if bounds.intersects(viewport):
                visible.append((transform.map(item.path()), transform.map(branch_arrow_path(branch)), width))
        if not visible:
            return
        cover = QPainterPath()
        # 卡片可能互相叠放，交叠部分也必须遮住底下的连线。
        cover.setFillRule(Qt.FillRule.WindingFill)
        pixel_size = 1 / (zoom * painter.device().devicePixelRatioF())
        for item in self.owner.node_items.values():
            if view_transform.mapRect(item.sceneBoundingRect()).intersects(viewport):
                transform = item.deviceTransform(view_transform)
                cover.addPath(transform.map(item.visual.card_outline(pixel_size=pixel_size)))
        if self.owner.editor is not None:
            bounds = view_transform.mapRect(self.owner.editor.sceneBoundingRect()).intersected(viewport)
            if not bounds.isEmpty():
                cover.addRect(bounds)
        painter.save()
        try:
            clip = QPainterPath()
            clip.addRect(viewport)
            painter.setClipPath(clip.subtracted(cover), Qt.ClipOperation.IntersectClip)
            stroke = QPen(color, 1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            if outline is not None:
                stroke.setColor(outline)
                arrow_outline = QPen(stroke)
                arrow_outline.setWidthF(outline_width)
                for path, arrow, width in visible:
                    stroke.setWidthF(width + outline_width)
                    painter.strokePath(path, stroke)
                    painter.strokePath(arrow, arrow_outline)
                stroke.setColor(color)
            for path, arrow, width in visible:
                stroke.setWidthF(width)
                painter.strokePath(path, stroke)
                painter.fillPath(arrow, color)
        finally:
            painter.restore()

    def paint(self, painter: QPainter) -> None:
        """在当前视口绘制关联边、直接选线及把手，颜色与线宽共同表达选择状态。

        出线把手和新建预览使用界面强调色，改接把手和预览随直接选中的线使用琥珀色。
        前景装饰只用于屏幕交互。借用本次 GUI 绘制的画笔，返回时恢复画笔状态。
        """
        graph = self.owner.graph
        if self.owner._disposed or graph is None:
            return
        drag = self._drag
        handles = self._handles()
        if not (self._selected_edges or self.owner.selection_count or drag or handles):
            return
        accent = QColor(self.owner.ui_colors.accent)
        background = self.view.scene().backgroundBrush().color()
        _, selected = graph_edge_colors(background)
        selected_color = QColor(selected)
        painter.save()
        try:
            painter.resetTransform()
            if self._selected_edges:
                self._paint_highlights(painter, selected_color, outline=background)
            else:
                self._paint_highlights(painter, accent)
            if drag is not None:
                if self._moved:
                    color = selected_color if drag.handle.original is not None else accent
                    self._paint_preview(painter, drag, color)
                return
            for handle in handles:
                color = selected_color if handle.original is not None else accent
                painter.setPen(QPen(color, 1.5))
                center = handle.center
                painter.drawLine(handle.anchor, center)
                # 空心表示起点，实心表示箭头端；出线把手另绘加号。
                painter.setBrush(color if handle.original is not None and not handle.moving_source else background)
                painter.drawEllipse(center, _RADIUS, _RADIUS)
                if handle.original is None:
                    x, y = QPointF(2, 0), QPointF(0, 2)
                    painter.drawLine(center - x, center + x)
                    painter.drawLine(center - y, center + y)
        finally:
            painter.restore()

    def _paint_preview(self, painter: QPainter, drag: _EdgeDrag, color: QColor) -> None:
        """用操作对应的颜色绘制跟随指针的虚线，命中合法节点时贴边并指向实际终点。"""
        fixed = self._node_rect(drag.handle.fixed_node)
        candidate = self._candidate(drag, self._pointer)
        moving = self._node_rect(candidate[0]) if candidate is not None else None
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if moving is not None:
            painter.setPen(QPen(color, 2))
            painter.drawRoundedRect(moving.adjusted(-3, -3, 3, 3), 6, 6)
        if candidate is not None and candidate[0] == drag.handle.fixed_node:
            # 自环预览从节点右侧绕回，同一节点仍能看清箭头和方向。
            x, y = fixed.right(), fixed.center().y()
            offset = min(9, fixed.height() / 3)
            start, end = QPointF(x, y - offset), QPointF(x, y + offset)
            path = QPainterPath(start)
            path.cubicTo(QPointF(x + 36, start.y()), QPointF(x + 36, end.y()), end)
        else:
            handle = drag.handle
            if handle.original is None:
                start = QPointF(fixed.right(), fixed.center().y())
            else:
                # 改接只移动被拖动的一端，虚线另一端继续从正式连线的原锚点出发。
                branch = self.owner.layout_result.branches[handle.original.key]
                anchor = branch.end if handle.moving_source else branch.start
                start = self.view.viewportTransform().map(QPointF(*anchor))
            end = _border_toward(moving, start) if moving is not None else QPointF(self._pointer)
            if handle.moving_source:
                start, end = end, start
            path = QPainterPath(start)
            path.lineTo(end)
        painter.setPen(QPen(color, 2, Qt.PenStyle.DashLine))
        painter.drawPath(path)
        direction = end - path.pointAtPercent(0.95)
        length = hypot(direction.x(), direction.y())
        if length > 0:
            unit = direction / length
            base = end - unit * min(9, path.length())
            side = QPointF(-unit.y(), unit.x()) * 3.5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([end, base + side, base - side]))
