"""供节点图画布和静态导出共用的分组外框。"""

from mindmap_core.geometry import GraphGroupFrame
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem


class GraphGroupItem(QGraphicsItem):
    """在节点及连线下方绘制半透明分组框。

    标题卡片由普通节点图元绘制，组框委托画布处理落点、高亮和尺寸手柄。
    图元由画布或导出场景持有，空命中形状让框内空白仍可用于框选。
    """

    def __init__(self, frame: GraphGroupFrame, *, accent: str):
        super().__init__()
        self._rect = QRectF()
        self._header = QRectF()
        self._highlighted = False
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(-2)
        self.sync(frame, accent=accent)

    def sync(self, frame: GraphGroupFrame, *, accent: str) -> None:
        """按新的场景几何更新外框及主题，保留本视图的拖放高亮。"""
        box = frame.bounds
        rect = QRectF(0, 0, box.width, box.height)
        header = QRectF(0, 0, box.width, frame.header_height)
        if (rect, header) != (self._rect, self._header):
            self.prepareGeometryChange()
            self._rect, self._header = rect, header
            self._outline = QPainterPath()
            self._outline.addRoundedRect(rect, 8, 8)
            header_path = QPainterPath()
            header_path.addRect(header)
            self._header_fill = self._outline.intersected(header_path)
        self._accent = QColor(accent)
        self.setPos(box.x, box.y)
        self.update()

    def set_drop_highlight(self, highlighted: bool) -> None:
        """切换本视图的拖放目标样式，图、框几何及选择状态均由调用方管理。"""
        if self._highlighted != highlighted:
            self._highlighted = highlighted
            self.update()

    def boundingRect(self) -> QRectF:
        """返回含高亮描边余量的局部绘制范围。"""
        return self._rect.adjusted(-2, -2, 2, 2)

    def shape(self) -> QPainterPath:
        """组框只参与绘制，成员投放和尺寸手柄使用布局几何独立命中。"""
        return QPainterPath()

    def paint(self, painter, _option, _widget=None) -> None:
        """绘制内容区、标题区底色和轮廓，同样适用于矢量导出。"""
        color = QColor(self._accent)
        color.setAlpha(38 if self._highlighted else 16)
        painter.fillPath(self._outline, color)
        color.setAlpha(100 if self._highlighted else 33)
        painter.fillPath(self._header_fill, color)
        color.setAlpha(255 if self._highlighted else 154)
        painter.setPen(QPen(color, 2 if self._highlighted else 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._outline)
