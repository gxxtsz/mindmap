"""统一图标按钮、分割器和拖放预览；尺寸均使用 Qt 逻辑像素。"""

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QSplitter, QToolButton, QWidget


class MenuButton(QToolButton):
    """文字菜单按钮用本项目 SVG 绘制箭头，避免符号字体缺字或箭头基线偏移。"""

    def __init__(self, manager, text="", parent=None, dropdown=True, leading_icon=""):
        super().__init__(parent)
        self.manager = manager
        self.dropdown = dropdown
        self.leading_icon = leading_icon
        self.setText(text)
        self.setAutoRaise(True)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 12 + (18 if self.dropdown else 0) + (22 if self.leading_icon else 0), 28)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.manager.theme
        if self.underMouse() or self.isDown():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.color("toolbar-bg-hovered"))
            painter.drawRoundedRect(self.rect(), 4, 4)
        left = 5
        if self.leading_icon:
            theme.icon(self.leading_icon, 16).paint(painter, QRect(left, (self.height() - 16) // 2, 16, 16))
            left += 22
        right = 18 if self.dropdown else 4
        painter.setPen(theme.color("text-default"))
        painter.setFont(self.font())
        painter.drawText(self.rect().adjusted(left, 0, -right, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.dropdown:
            theme.icon("chevron-down", 12).paint(painter, QRect(self.width() - 15, (self.height() - 12) // 2, 12, 12))


class IconButton(QAbstractButton):
    """保留标准按钮的键盘和无障碍语义，统一图标、文字及交互状态的绘制。

    manager 提供局部主题；Qt parent 拥有按钮，按钮不拥有管理器或业务控件。
    图标和符号沿用原有参数；setText 可增加左图右文的标签，空文字时图标居中。
    所有构造、外观更新与绘制均在 Qt 主线程执行，不改变管理器布局状态。
    """

    def __init__(self, manager, icon="", symbol="", tooltip="", parent=None):
        super().__init__(parent)
        self.manager = manager
        self.icon_name = icon
        self.symbol = symbol
        self.notification = False
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or symbol)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(28, 28)

    def sizeHint(self):
        return QSize(28, 28)

    def _background_rect(self):
        """返回按钮局部逻辑坐标中的状态背景；侧栏可覆盖，标题按钮保持原留白。"""
        return QRectF(self.rect()).adjusted(2, 2, -2, -2)

    def _background_radius(self):
        """返回状态背景圆角的逻辑像素半径。"""
        return 5

    def _icon_rect(self):
        """统一图标画布；紧凑模式必须实际绘制为 16，不能仅缩小资源请求值。"""
        size = 16 if self.manager.theme.compact else 20
        left = 8 if self.text() else (self.width() - size) // 2
        return QRect(left, (self.height() - size) // 2, size, size)

    def paintEvent(self, event):
        theme = self.manager.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._background_rect()
        radius = self._background_radius()
        tool_id = getattr(self, "tool_id", None)
        active = self.isChecked() and tool_id is not None and tool_id == self.manager.active_id
        if self.isDown() or self.isChecked():
            painter.setBrush(theme.color("toolbar-selected-bg-active" if active else "toolbar-bg-hovered"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(area, radius, radius)
        elif self.underMouse() or self.hasFocus():
            painter.setBrush(theme.color("toolbar-bg-hovered"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(area, radius, radius)
        rect = self._icon_rect()
        icon = theme.icon(self.icon_name, rect.width()) if self.icon_name else None
        if icon and not icon.isNull():
            if active:
                pixmap = icon.pixmap(rect.size(), self.devicePixelRatioF())
                tint = QPainter(pixmap)
                tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                tint.fillRect(pixmap.rect(), theme.color("ToolWindow.Button.selectedForeground", "#FFFFFF"))
                tint.end()
                painter.drawPixmap(rect, pixmap)
            else:
                icon.paint(painter, rect)
        else:
            painter.setPen(QPen(theme.color("text-muted"), 1.2))
            c = self.rect().center()
            if self.symbol == "close":
                painter.drawLine(c + QPoint(-4, -4), c + QPoint(4, 4))
                painter.drawLine(c + QPoint(4, -4), c + QPoint(-4, 4))
            elif self.symbol == "minus":
                painter.drawLine(c + QPoint(-5, 0), c + QPoint(5, 0))
            elif self.symbol == "maximize":
                painter.drawRect(QRect(c.x() - 4, c.y() - 4, 8, 8))
            elif self.symbol == "more":
                painter.setBrush(theme.color("text-muted"))
                painter.setPen(Qt.PenStyle.NoPen)
                for y in (-4, 0, 4):
                    painter.drawEllipse(QPoint(c.x(), c.y() + y), 1, 1)
            elif self.symbol == "menu":
                for y in (-5, 0, 5):
                    painter.drawLine(c + QPoint(-6, y), c + QPoint(6, y))
            else:
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.symbol or "◇")
        if self.text():
            # 通用按钮的可选标签与图标共用状态和位置计算，随紧凑布局同步调整。
            text_rect = self.rect().adjusted(rect.right() + 7, 0, -8, 0)
            label = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                                max(0, text_rect.width()))
            painter.setPen(theme.color("ToolWindow.Button.selectedForeground", "#FFFFFF")
                           if active else theme.color("text-default"))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        if self.notification:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#E55765"))
            painter.drawEllipse(QPoint(self.width() - 8, 7), 3, 3)


class DockSplitter(QSplitter):
    """分割器手柄与面板间距一致；禁止用户将可见工具窗拖至零大小。"""

    def __init__(self, orientation, manager, parent=None):
        super().__init__(orientation, parent)
        self.manager = manager
        self.setChildrenCollapsible(False)
        self.refresh_theme()

    def refresh_theme(self):
        self.setHandleWidth(self.manager.theme.gap)


class DropOverlay(QWidget):
    """预览不接收鼠标事件，所有真正的 drop 仍由宿主验证并提交。"""

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.preview = QRect()
        self.caption = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.hide()

    def present(self, rect: QRect, caption: str):
        self.setGeometry(self.host.rect())
        self.preview = rect
        self.caption = caption
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.host.manager.theme
        color = theme.color("ToolWindow.DragAndDrop.areaBackground", "#A0BDF84D" if not theme.dark else "#366ACF4D")
        painter.setBrush(color)
        painter.setPen(QPen(theme.color("control-brand-border"), 2))
        painter.drawRoundedRect(QRectF(self.preview).adjusted(2, 2, -2, -2), theme.radius, theme.radius)
        painter.setPen(theme.color("text-default"))
        painter.drawText(self.preview, Qt.AlignmentFlag.AlignCenter, self.caption)
