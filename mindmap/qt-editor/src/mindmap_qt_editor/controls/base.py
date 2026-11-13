"""画布和属性面板复用的基础控件，由调用方在 Qt GUI 线程创建并管理。"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QVBoxLayout, QWidget

from ..theme import CARD_INSET, CARD_RADIUS


class ComboBox(QComboBox):
    """仅补充下拉箭头的组合框，原生弹出、输入、选择及无障碍语义保持不变。

    Windows 的原生箭头在 QSS 自定义下拉区域后可能消失或沿用黑色；使用实际子控件
    区域和当前调色板绘制矢量箭头，保证明暗、高 DPI 及从右到左排列均正确。
    """

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxArrow, self
        )
        center = rect.center()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        painter.setPen(
            QPen(
                self.palette().color(group, QPalette.ColorRole.Text),
                1.5,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(center.x() - 3, center.y() - 1.5),
                    QPointF(center.x(), center.y() + 1.5),
                    QPointF(center.x() + 3, center.y() - 1.5),
                ]
            )
        )


class CardFrame(QWidget):
    """画布承载层；独立使用时留出圆角半径，嵌入编辑器时由宿主管理外轮廓。

    不对 QGraphicsScene 使用遮罩、特效或样式表。卡片仅在普通 QWidget 中绘制，
    因而不改变节点测量、图形边界和导出；内边距由 Qt 统一处理高 DPI 缩放。
    """

    def __init__(self, content: QWidget, parent=None, *, rounded=True):
        """rounded=False 时使用直角和零内边距，让视口与宿主标签栏直接衔接。"""
        super().__init__(parent)
        self.setObjectName("mindmap_canvas_card")
        self.background = "#ffffff"
        self.rounded = rounded
        layout = QVBoxLayout(self)
        inset = CARD_INSET if rounded else 0
        layout.setContentsMargins(inset, inset, inset, inset)
        layout.setSpacing(0)
        layout.addWidget(content)

    def set_background(self, color: str) -> None:
        """仅更新承载层的背景；不触碰视口状态、输入焦点或文档。"""
        if self.background != color:
            self.background = color
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.background))
        radius = CARD_RADIUS if self.rounded else 0
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
