"""可选的通用展示控件；只依赖 Qt Widgets，不引入图表或数据处理运行库。"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class MetricCard(QFrame):
    """适合概览页面的数值卡片，主题来源由业务管理器注入。"""

    def __init__(self, manager, title: str, value="—", detail="", parent=None):
        super().__init__(parent)
        self.manager = manager
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(7)
        self.title = QLabel(title)
        self.value = QLabel(value)
        self.detail = QLabel(detail)
        font = QFont(self.value.font())
        font.setPixelSize(26)
        font.setBold(True)
        self.value.setFont(font)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)
        self.setMinimumHeight(116)
        manager.theme_changed.connect(self.refresh_theme)
        self.refresh_theme()

    def set_value(self, value: str, detail: str | None = None):
        """只更新显示文本，不重建控件，保留布局、焦点和外部信号连接。"""
        self.value.setText(value)
        if detail is not None:
            self.detail.setText(detail)

    def refresh_theme(self):
        theme = self.manager.theme
        self.setObjectName("mindmap_metric_card")
        self.setStyleSheet(f"QFrame#mindmap_metric_card {{border:1px solid {theme.css('tool-window-border')}; border-radius:8px; background:{theme.css('tool-window-bg')};}}")
        self.title.setStyleSheet(f"color:{theme.css('text-muted')};")
        self.detail.setStyleSheet(f"color:{theme.css('control-brand-border')};font-size:11px;")


class LineChart(QWidget):
    """轻量折线图；以有限数值序列绘制趋势，适合示例和简单仪表盘。"""

    def __init__(self, manager, *, label="趋势", parent=None):
        super().__init__(parent)
        self.manager = manager
        self.label = label
        self.values = []
        self.labels = []
        self.setMinimumHeight(210)
        self.setAccessibleName(label)
        manager.theme_changed.connect(self.update)

    def set_series(self, values, labels=None):
        """按值保存数据；无效或非有限值在修改图表前拒绝。"""
        values = [float(value) for value in values]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("图表只接受有限数值")
        self.values = values
        self.labels = list(labels or [])
        self.update()

    def paintEvent(self, event):
        theme = self.manager.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), theme.color("tool-window-bg"))
        area = QRectF(62, 35, max(1, self.width() - 90), max(1, self.height() - 80))
        painter.setPen(theme.color("text-default"))
        painter.drawText(QRectF(16, 0, self.width() - 32, 28), Qt.AlignmentFlag.AlignVCenter, self.label)
        if not self.values:
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "当前筛选条件下没有数据")
            return
        top, bottom = max(self.values), min(0.0, min(self.values))
        span = max(1.0, top - bottom)
        top += span * 0.12
        span = top - bottom
        font = QFont(self.font())
        font.setPixelSize(10)
        painter.setFont(font)
        for index in range(5):
            y = area.top() + index * area.height() / 4
            painter.setPen(QPen(theme.color("tool-window-border"), 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(theme.color("text-muted"))
            painter.drawText(QRectF(0, y - 10, 52, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{top - span * index / 4:,.0f}")
        points = [QPointF(area.left() + index * area.width() / max(1, len(self.values) - 1),
                          area.bottom() - (value - bottom) / span * area.height()) for index, value in enumerate(self.values)]
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        fill = QPainterPath(path)
        fill.lineTo(points[-1].x(), area.bottom())
        fill.lineTo(points[0].x(), area.bottom())
        fill.closeSubpath()
        shade = theme.color("control-brand-border")
        shade.setAlpha(25)
        painter.fillPath(fill, shade)
        painter.setPen(QPen(theme.color("control-brand-border"), 2.5))
        painter.drawPath(path)
        if len(points) <= 32:
            painter.setBrush(theme.color("tool-window-bg"))
            for point in points:
                painter.drawEllipse(point, 3, 3)
        painter.setPen(theme.color("text-muted"))
        for index in range(0, len(points), max(1, len(points) // 6)):
            text = str(self.labels[index]) if index < len(self.labels) else str(index + 1)
            painter.drawText(QRectF(points[index].x() - 32, area.bottom() + 10, 64, 20), Qt.AlignmentFlag.AlignCenter, text)
