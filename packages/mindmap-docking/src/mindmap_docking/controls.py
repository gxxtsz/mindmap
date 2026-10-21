"""统一图标按钮、分割器和拖放预览；尺寸均使用 Qt 逻辑像素。"""

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QSplitter, QToolButton, QWidget


class _CardCorner(QWidget):
    """覆盖一个卡片转角外侧的小控件；用背景色平滑遮住矩形子控件的角。

    仅绘制圆弧外侧，不渲染或缓存业务内容，也不截获鼠标和键盘事件。
    路径保持浮点坐标，Qt 在当前设备像素比下计算抗锯齿，避免 QRegion 的硬裁切。
    """

    def __init__(self, owner, right, bottom):
        super().__init__(owner.parent())
        self.owner = owner
        self.right, self.bottom = right, bottom
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        radius = self.width()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(radius if self.right else 0, radius if self.bottom else 0)
        painter.scale(-1 if self.right else 1, -1 if self.bottom else 1)
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(radius, 0)
        path.arcTo(QRectF(0, 0, radius * 2, radius * 2), 90, 90)
        path.closeSubpath()
        painter.fillPath(path, self.owner.manager.theme.color("main-window-bg"))


class _CardCorners(QObject):
    """为工具卡片和文档卡片共用四角绘制，保持布局尺寸与业务控件实例不变。

    parent 为实际卡片，manager 提供主题圆角和卡片外侧工作区颜色。四个覆盖控件
    由卡片拥有，始终位于内容上方；仅占圆角区域，不创建覆盖整个画布的绘制层。
    调用 refresh 可同步主题；尺寸、显示及新增子控件由事件过滤器自动同步。
    """

    def __init__(self, parent, manager):
        super().__init__(parent)
        self.manager = manager
        self.corners = [_CardCorner(self, right, bottom)
                        for right, bottom in ((False, False), (True, False), (False, True), (True, True))]
        parent.installEventFilter(self)
        self.refresh()

    def refresh(self):
        parent = self.parent()
        radius = max(0, min(self.manager.theme.radius, parent.width() // 2, parent.height() // 2))
        for corner in self.corners:
            corner.setGeometry(parent.width() - radius if corner.right else 0,
                               parent.height() - radius if corner.bottom else 0, radius, radius)
            corner.setVisible(radius > 0)
            corner.raise_()
            corner.update()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.ChildPolished):
            self.refresh()
        return super().eventFilter(watched, event)


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
        # Qt 在窗口首次显示时可能自动聚焦第一个可 Tab 访问的按钮。
        # 单独记录键盘导航来源，避免把这种自动焦点误画成悬停或用户选中。
        self._keyboard_focus = False
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

    def _focus_rect(self):
        """返回键盘焦点描边的中心线矩形，默认将两像素描边收在背景区域内。

        标题栏小按钮的外侧留白有限，因此默认内缩一像素；侧栏入口可覆盖此接口，
        将焦点圈放在状态背景外。返回值只参与绘制，不改变布局、点击范围或焦点链。
        """
        return self._background_rect().adjusted(1, 1, -1, -1)

    def _icon_rect(self):
        """统一图标画布；紧凑模式必须实际绘制为 16，不能仅缩小资源请求值。"""
        size = 16 if self.manager.theme.compact else 20
        left = 8 if self.text() else (self.width() - size) // 2
        return QRect(left, (self.height() - size) // 2, size, size)

    def enterEvent(self, event):
        """鼠标进入后重绘自绘背景，不依赖宿主是否安装了 Qt 悬停样式。"""
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        """鼠标移开立即撤销悬停底色；选中状态继续由标准按钮状态决定。"""
        super().leaveEvent(event)
        self.update()

    def focusInEvent(self, event):
        """仅键盘导航显示焦点描边，弹出菜单或切换窗口后保留原来的来源。"""
        reason = event.reason()
        if reason in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                      Qt.FocusReason.ShortcutFocusReason):
            self._keyboard_focus = True
        elif reason not in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
            self._keyboard_focus = False
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        """焦点离开时撤销描边，保留 Qt 标准焦点链与键盘激活行为。"""
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, event):
        theme = self.manager.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._background_rect()
        radius = self._background_radius()
        tool_id = getattr(self, "tool_id", None)
        selected = self.isChecked() and tool_id is not None
        active = selected and tool_id == self.manager.active_id
        foreground = theme.color("text-muted")
        background_key = None
        # 工具入口的打开状态与面板活动状态分别着色，避免活动蓝底扩散到树行、菜单等
        # 共用 toolbar-selected-bg-active 的控件；悬停也不能覆盖已打开工具的状态。
        if selected:
            background_key = ("ToolWindow.Button.selectedBackground" if active
                              else "ToolWindow.Button.inactiveBackground")
            foreground = theme.color("ToolWindow.Button.selectedForeground" if active
                                     else "ToolWindow.Button.inactiveForeground")
        elif self.isDown() or self.isChecked() or self.underMouse():
            background_key = "toolbar-bg-hovered"
        if background_key is not None:
            painter.setBrush(theme.color(background_key))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(area, radius, radius)
        if self.hasFocus() and self._keyboard_focus:
            # 焦点仅表示键盘将操作的按钮，不改变工具的活动状态和填充色。
            # 描边矩形由控件规格决定，圆角随外扩或内缩同步，深色主题使用更亮的品牌色。
            focus_area = self._focus_rect()
            focus_radius = max(0, radius + (focus_area.width() - area.width()) / 2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.color("control-brand-border"), 2))
            painter.drawRoundedRect(focus_area, focus_radius, focus_radius)
        rect = self._icon_rect()
        icon = theme.icon(self.icon_name, rect.width()) if self.icon_name else None
        if icon and not icon.isNull():
            if selected:
                pixmap = icon.pixmap(rect.size(), self.devicePixelRatioF())
                tint = QPainter(pixmap)
                tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                tint.fillRect(pixmap.rect(), foreground)
                tint.end()
                painter.drawPixmap(rect, pixmap)
            else:
                icon.paint(painter, rect)
        else:
            painter.setPen(QPen(foreground, 1.2))
            c = self.rect().center()
            if self.symbol == "close":
                painter.drawLine(c + QPoint(-4, -4), c + QPoint(4, 4))
                painter.drawLine(c + QPoint(4, -4), c + QPoint(-4, 4))
            elif self.symbol == "minus":
                painter.drawLine(c + QPoint(-5, 0), c + QPoint(5, 0))
            elif self.symbol == "maximize":
                painter.drawRect(QRect(c.x() - 4, c.y() - 4, 8, 8))
            elif self.symbol == "more":
                painter.setBrush(foreground)
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
            painter.setPen(foreground if selected else theme.color("text-default"))
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
