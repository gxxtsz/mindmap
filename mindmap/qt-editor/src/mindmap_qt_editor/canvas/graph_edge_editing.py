"""图页的选线和拖线手势。前景装饰仅属于视口，正式连线仍由场景和引擎管理。"""

from dataclasses import dataclass
from math import hypot, sqrt

from mindmap_core import GraphEdge, NodeGraph
from mindmap_qt_render.rendering import branch_arrow_path
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QApplication

from .items import NodeItem

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
        self.selected_edge: GraphEdge | None = None
        self._outlet_node: str | None = None
        self._drag: _EdgeDrag | None = None
        self._pointer = QPoint()
        self._moved = False
        self.owns_left_button = False

    @property
    def active(self) -> bool:
        """按下把手到松手之间为活动手势，包含尚未超过拖动阈值的阶段。"""
        return self._drag is not None

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

    def select_edge(self, edge: GraphEdge) -> None:
        """选中当前可见边，先清理节点逻辑选择，再同步 Delete 和视口前景。"""
        if edge.key not in self.owner.branch_items or edge == self.selected_edge:
            return
        if self.owner.selection_count:
            self.owner.select_nodes([])
        self.selected_edge = edge
        self.owner._update_actions()
        self.view.viewport().update()

    def clear_selection(self) -> None:
        """丢弃本视图的选线，其他标签及文档历史不受影响。"""
        if self.selected_edge is not None:
            self.selected_edge = None
            self.owner._update_actions()
            self.view.viewport().update()

    def sync_visibility(self) -> None:
        """正式布局及筛选更新后清除已删除或已隐藏的选择。"""
        if self.selected_edge is not None:
            if self.selected_edge.key not in self.owner.branch_items:
                self.clear_selection()
            else:
                self.view.viewport().update()

    def press_left(self, position) -> bool:
        """先处理把手，再处理节点之外的可见边；其他点击交回原有节点和框选流程。"""
        self.owns_left_button = False
        if self.owner.graph is None:
            return False
        handle = self.handle_at(position)
        if handle is not None:
            graph, target = self.owner.graph, self.owner._graph_target()
            self.view.setFocus(Qt.FocusReason.MouseFocusReason)
            # 分屏抢到焦点时，旧标签可能先提交文字草稿。放弃这次过期命中，仍接收配对释放。
            if self._can_edit() and self.owner.graph is graph:
                self._drag = _EdgeDrag(handle, target, graph, frozenset(graph.edges), QPoint(position))
                self._pointer = QPoint(position)
                self._moved = False
        else:
            item = self.view.itemAt(position)
            edge = None if isinstance(item, NodeItem) else self.owner.graph_edge_at(position)
            if edge is None:
                self.clear_selection()
                return False
            self.view.setFocus(Qt.FocusReason.MouseFocusReason)
            self.select_edge(edge)
        self.owns_left_button = True
        return True

    def move(self, position) -> None:
        """更新虚线的指针位置；手势经过系统阈值后才允许产生一次落点。"""
        if self._drag is not None:
            self._pointer = QPoint(position)
            self._moved |= (position - self._drag.press).manhattanLength() >= QApplication.startDragDistance()
            self.view.viewport().update()

    def _candidate(self, drag: _EdgeDrag, position) -> tuple[str, GraphEdge] | None:
        if not self._moved or not self.view.viewport().rect().contains(position):
            return None
        item = self.view.itemAt(position)
        if not isinstance(item, NodeItem):
            return None
        key = item.node.id
        handle = drag.handle
        edge = (GraphEdge(key, handle.fixed_node) if handle.moving_source else GraphEdge(handle.fixed_node, key))
        # 已筛掉的边仍在正式数据中，落到相同有序端点也必须视为重复。
        return (key, edge) if edge not in drag.edges else None

    def release(self, position) -> None:
        """以松手坐标重做命中，先清除预览，再校验并提交一次引擎命令。"""
        self.owns_left_button = False
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
        """丢弃预览及悬停。已认领的左键释放仍由视图消费，避免转成节点点击。"""
        redraw = self.active or self._outlet_node is not None or self.selected_edge is not None
        self._drag = self._outlet_node = None
        self._moved = False
        if redraw:
            self.view.viewport().update()

    def paint(self, painter) -> None:
        """绘制本标签的高亮、把手和临时箭头。坐标映射与命中一致，导出场景不包含装饰。"""
        edge, drag = self.selected_edge, self._drag
        handles = self._handles()
        if self.owner._disposed or (edge is None and drag is None and not handles):
            return
        accent = QColor(self.owner.ui_colors.accent)
        background = self.view.scene().backgroundBrush()
        painter.save()
        try:
            painter.resetTransform()
            if drag is not None:
                if self._moved:
                    self._paint_preview(painter, drag, accent)
                return
            if edge is not None and (item := self.owner.branch_items.get(edge.key)) is not None:
                transform = item.deviceTransform(self.view.viewportTransform())
                path = transform.map(item.path())
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(background, 5))
                painter.drawPath(path)
                painter.setPen(QPen(accent, 3))
                painter.drawPath(path)
                branch = self.owner.layout_result.branches[edge.key]
                painter.fillPath(transform.map(branch_arrow_path(branch)), accent)
            painter.setPen(QPen(accent, 1.5))
            for handle in handles:
                center = handle.center
                painter.drawLine(handle.anchor, center)
                # 空心表示起点，实心表示箭头端；出线把手另绘加号。
                painter.setBrush(accent if handle.original is not None and not handle.moving_source else background)
                painter.drawEllipse(center, _RADIUS, _RADIUS)
                if handle.original is None:
                    x, y = QPointF(2, 0), QPointF(0, 2)
                    painter.drawLine(center - x, center + x)
                    painter.drawLine(center - y, center + y)
        finally:
            painter.restore()

    def _paint_preview(self, painter, drag: _EdgeDrag, accent: QColor) -> None:
        """绘制指针跟随的虚线，命中合法节点时贴边，并按实际方向绘制箭头。"""
        fixed = self._node_rect(drag.handle.fixed_node)
        candidate = self._candidate(drag, self._pointer)
        moving = self._node_rect(candidate[0]) if candidate is not None else None
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if moving is not None:
            painter.setPen(QPen(accent, 2))
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
        painter.setPen(QPen(accent, 2, Qt.PenStyle.DashLine))
        painter.drawPath(path)
        direction = end - path.pointAtPercent(0.95)
        length = hypot(direction.x(), direction.y())
        if length > 0:
            unit = direction / length
            base = end - unit * min(9, path.length())
            side = QPointF(-unit.y(), unit.x()) * 3.5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawPolygon(QPolygonF([end, base + side, base - side]))
