"""节点图画布与静态导出共用的层级和组框。"""

from collections import defaultdict
from itertools import count

from mindmap_core.geometry import GraphGroupFrame
from mindmap_core.graph import GraphEdge, NodeGraph
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem


class GraphStacking:
    """按同层叠放顺序展开场景。每组的底板、内部连线、成员和标题占连续层级。"""

    def __init__(self, graph: NodeGraph):
        self.graph = graph
        self.frames: dict[str, int] = {}
        self.nodes: dict[str, int] = {}
        self.edges: dict[str, int] = {}
        self.edge_order: list[GraphEdge] = []
        self.objects: list[str] = []
        self.parents = graph.parent_groups()
        edges = defaultdict(list)
        for edge in graph.edges:
            edges[self.parents.get(edge.source)].append(edge)
        ranks = count()

        def place_edges(scope):
            for edge in edges[scope]:
                self.edges[edge.key] = next(ranks)
                self.edge_order.append(edge)

        place_edges(None)
        for kind, key in graph.stacking_walk():
            if kind != "title":
                self.objects.append(key)
            if kind == "frame":
                self.frames[key] = next(ranks)
                place_edges(key)
            else:
                self.nodes[key] = next(ranks)


class GraphGroupItem(QGraphicsItem):
    """以不透明底板遮挡组后方的对象，标题、成员和内部连线在底板上绘制。"""

    def __init__(self, frame: GraphGroupFrame, *, accent: str, border=None, background="#ffffff"):
        super().__init__()
        self._rect = QRectF()
        self._outline = QPainterPath()
        self._highlighted = False
        self._selected = False
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(-2)
        self.sync(frame, accent=accent, border=border, background=background)

    def sync(self, frame: GraphGroupFrame, *, accent: str, border=None, background="#ffffff") -> None:
        """按新的场景几何更新外框及主题，保留本视图的拖放高亮。"""
        box = frame.bounds
        rect = QRectF(0, 0, box.width, box.height)
        if rect != self._rect:
            self.prepareGeometryChange()
            self._rect = rect
            self._outline = QPainterPath()
            self._outline.addRoundedRect(rect, 8, 8)
        self._accent = QColor(accent)
        self._border = QColor(border or accent)
        self._background = QColor(background)
        self._background.setAlpha(255)
        self.setPos(box.x, box.y)
        self.update()

    def set_drop_highlight(self, highlighted: bool) -> None:
        """切换本视图的拖放目标样式，图、框几何及选择状态均由调用方管理。"""
        if self._highlighted != highlighted:
            self._highlighted = highlighted
            self.update()

    def set_group_selected(self, selected: bool) -> None:
        """选择跟随逻辑组节点，背景本身不抢占内容区的鼠标事件。"""
        if self._selected != selected:
            self._selected = selected
            self.update()

    def boundingRect(self) -> QRectF:
        """返回含高亮描边余量的局部绘制范围。"""
        return self._rect.adjusted(-2, -2, 2, 2)

    def shape(self) -> QPainterPath:
        """用实际圆角轮廓遮挡后方对象并接收内容区的命中查询。"""
        return self._outline

    def paint(self, painter, _option, _widget=None) -> None:
        """共用一层轻底色，选中用实线，接收拖入时用虚线。"""
        active = self._highlighted or self._selected
        stroke = self._accent if active else self._border
        color = QColor(stroke)
        color.setAlpha(24 if self._highlighted else 10)
        painter.fillPath(self._outline, self._background)
        painter.fillPath(self._outline, color)
        pen = QPen(stroke, 2 if active else 1, Qt.PenStyle.DashLine if self._highlighted else Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._outline)
