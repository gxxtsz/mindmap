"""工具窗面板、浮动窗口与覆盖容器，所有业务控件始终复用同一个实例。"""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QSizePolicy,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .controls import IconButton, MenuButton


class DraggableTitle(MenuButton):
    """标题既可打开内容视图选择，也可启动工具窗拖动，按拖动阈值区分两者。"""

    def __init__(self, panel):
        super().__init__(panel.manager, parent=panel.header, dropdown=False)
        self.panel = panel
        self.press = QPoint()

    def mousePressEvent(self, event):
        self.press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and (event.position().toPoint() - self.press).manhattanLength() >= QApplication.startDragDistance():
            self.setDown(False)
            self.panel.manager.start_tool_drag(self.panel.tool_id, self, self.press)
            return
        super().mouseMoveEvent(event)


class PanelHeader(QWidget):
    """归工具面板所有的可拖动标题栏；事件处理和主题绘制均在 Qt 主线程执行。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.manager = panel.manager
        self.press = QPoint()
        self.setObjectName(f"header_{panel.tool_id}")

    def paintEvent(self, event):
        painter = QPainter(self)
        theme = self.manager.theme
        # 主题未定义标题颜色时继承自身边框色，避免深色界面回退成浅色横线。
        color = theme.color("ToolWindow.Header.borderColor", theme.color("tool-window-border").name())
        # 用填充矩形准确表示 1 个逻辑像素的线宽，两端各留 10；Qt 负责设备像素比缩放。
        # 极窄标题将长度收至零，避免负尺寸或线段反向越过预留的边距。
        painter.fillRect(10, self.height() - 1, max(0, self.width() - 20), 1, color)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press = event.position().toPoint()
            # 按下时只能更新活动标记；刷新布局会隐藏源控件并丢失鼠标抓取，
            # 从而让“按住标题再拖动”在真正启动 QDrag 前就被中断。
            self.manager.mark_active(self.panel.tool_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and (event.position().toPoint() - self.press).manhattanLength() >= QApplication.startDragDistance():
            self.manager.start_tool_drag(self.panel.tool_id, self, self.press)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.manager.toggle_maximize(self.panel.tool_id)

    def contextMenuEvent(self, event):
        self.manager.tool_menu(self.panel.tool_id, self).exec(event.globalPos())


class ToolPanel(QFrame):
    """工具窗注册工厂在此处只调用一次；重停靠仅移动该面板，不销毁内容。"""

    def __init__(self, manager, spec):
        super().__init__()
        try:
            content = spec.factory()
            if not isinstance(content, QWidget):
                raise TypeError(f"{spec.id} 的 factory 必须返回 QWidget")
        except Exception:
            self.deleteLater()
            raise
        self.manager = manager
        self.tool_id = spec.id
        self.spec = spec
        self.setObjectName(f"panel_{spec.id}")
        self.setMinimumSize(120, 80)
        self._hover = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.header = PanelHeader(self)
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(10, 2, 5, 2)
        self.header_layout.setSpacing(3)
        self.title_button = DraggableTitle(self)
        self.title_button.setText(spec.title)
        self.title_button.setAutoRaise(True)
        self.title_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        title_font = self.title_button.font()
        title_font.setBold(True)
        self.title_button.setFont(title_font)
        self.title_button.clicked.connect(self.show_views)
        self.header_layout.addWidget(self.title_button)
        self.tabs = QTabBar(self.header)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.currentChanged.connect(self._change_content_tab)
        self.header_layout.addWidget(self.tabs)
        self.header_layout.addStretch(1)
        self.more = IconButton(manager, symbol="more", tooltip="选项", parent=self.header)
        self.more.setObjectName(f"options_{spec.id}")
        self.more.clicked.connect(lambda: manager.tool_menu(spec.id, self.more).exec(self.more.mapToGlobal(QPoint(0, self.more.height()))))
        self.hide_button = IconButton(manager, symbol="minus", tooltip="隐藏  Shift+Esc", parent=self.header)
        self.hide_button.clicked.connect(lambda: manager.hide(spec.id))
        self.header_layout.addWidget(self.more)
        self.header_layout.addWidget(self.hide_button)
        self._layout.addWidget(self.header)
        self.content = content
        self.content.setParent(self)
        self._layout.addWidget(self.content, 1)
        # 可分组的内容视图页签位于工具窗标题内：将 QTabWidget 的标签镜像到标题，
        # 不改变用户工厂提供的页面对象、所有权或 currentChanged 语义。
        if isinstance(self.content, QTabWidget):
            self.content.tabBar().hide()
            for n in range(self.content.count()):
                self.tabs.addTab(self.content.tabText(n))
            self.content.currentChanged.connect(self.tabs.setCurrentIndex)
        else:
            self.tabs.hide()
        self.refresh_theme()

    def refresh_theme(self):
        theme = self.manager.theme
        self.header.setFixedHeight(theme.header_height)
        title_font = self.title_button.font()
        title_font.setPixelSize(12 if theme.compact else 13)
        self.title_button.setFont(title_font)
        self.setStyleSheet(f"QFrame#{self.objectName()} {{ background:{theme.css('tool-window-bg')}; border-radius:{theme.radius}px; }}")
        self.title_button.setStyleSheet(f"QToolButton {{background:transparent; border:0; color:{theme.css('text-default')}; padding:0 2px;}} QToolButton:hover {{background:{theme.css('toolbar-bg-hovered')};border-radius:4px;}}")
        self.update_header()
        self.update()

    def resizeEvent(self, event):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainterPath, QRegion
        super().resizeEvent(event)
        path = QPainterPath()
        radius = self.manager.theme.radius
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def update_header(self):
        grouped = self.manager.states[self.tool_id].grouped_tabs
        self.tabs.setVisible(isinstance(self.content, QTabWidget) and not grouped)
        self.title_button.dropdown = grouped and isinstance(self.content, QTabWidget)
        self.title_button.setText(self.spec.title)
        self.title_button.updateGeometry()
        self.title_button.update()
        visible = self._hover or self.manager.active_id == self.tool_id or self.manager.settings.always_header_icons
        self.more.setVisible(visible)
        self.hide_button.setVisible(visible)

    def _change_content_tab(self, index):
        if isinstance(getattr(self, "content", None), QTabWidget) and index >= 0:
            self.content.setCurrentIndex(index)

    def cycle_view(self, direction):
        if isinstance(self.content, QTabWidget) and self.content.count():
            self.content.setCurrentIndex((self.content.currentIndex() + direction) % self.content.count())

    def show_views(self):
        if not isinstance(self.content, QTabWidget):
            return
        menu = QMenu(self)
        for index in range(self.content.count()):
            action = menu.addAction(self.content.tabText(index))
            action.setCheckable(True)
            action.setChecked(index == self.content.currentIndex())
            action.triggered.connect(lambda checked=False, n=index: self.content.setCurrentIndex(n))
        menu.exec(self.title_button.mapToGlobal(QPoint(0, self.title_button.height())))

    def enterEvent(self, event):
        self._hover = True
        self.update_header()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update_header()
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        self.manager.tool_menu(self.tool_id, self).exec(event.globalPos())


class FloatingToolWindow(QMainWindow):
    """Float 使用 Qt.Tool 所有者窗口；Window 使用无所有者 Qt.Window。

    两者不可只通过置顶开关模拟：所有者关系决定任务栏、主窗最小化和关闭行为。
    关闭事件转为隐藏，持有的业务 QWidget 交还管理器后才能销毁窗口外壳。
    """

    def __init__(self, manager, tool_id, independent=False):
        state = manager.states[tool_id]
        owner = None if independent else manager.hosts[state.host_id].window()
        super().__init__(owner, Qt.WindowType.Window if independent else Qt.WindowType.Tool)
        self.manager = manager
        self.tool_id = tool_id
        self.independent = independent
        self._retiring = False
        self.setObjectName(f"floating_{tool_id}")
        self.setWindowTitle(manager.specs[tool_id].title)
        self.setMinimumSize(240, 160)
        self.setAcceptDrops(True)

    def closeEvent(self, event):
        if self._retiring or self.manager.closing:
            event.accept()
            return
        event.ignore()
        self.manager.hide(self.tool_id)

    def moveEvent(self, event):
        self._store_geometry()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._store_geometry()
        super().resizeEvent(event)

    def _store_geometry(self):
        if self.isVisible() and not self.manager.refreshing and not self.isMinimized():
            rect = self.geometry()
            self.manager.states[self.tool_id].geometry = [rect.x(), rect.y(), rect.width(), rect.height()]


class OverlayContainer(QFrame):
    """Undock 覆盖容器；边缘命中区位于内容外，避免吞掉编辑器/终端鼠标事件。"""

    def __init__(self, host, tool_id):
        super().__init__(host)
        self.host = host
        self.tool_id = tool_id
        self.manager = host.manager
        self.drag_origin = None
        self.setMouseTracking(True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(1, 1, 6, 6)
        self._layout.setSpacing(0)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _edge(self, point):
        position = self.manager.states[self.tool_id].position
        if position.startswith("bottom"):
            return "top" if point.y() <= 6 else ""
        if position.startswith("right"):
            return "left" if point.x() <= 6 else ""
        return "right" if point.x() >= self.width() - 7 else ""

    def mousePressEvent(self, event: QMouseEvent):
        edge = self._edge(event.position().toPoint())
        if edge and event.button() == Qt.MouseButton.LeftButton:
            # 从当前实际几何开始调整；窗口曾被临时压缩时，不跳回未限幅的目标尺寸。
            extent = self.height() if edge == "top" else self.width()
            self.drag_origin = (event.globalPosition().toPoint(), extent, edge)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_origin:
            origin, extent, edge = self.drag_origin
            delta = event.globalPosition().toPoint() - origin
            change = -delta.y() if edge == "top" else (-delta.x() if edge == "left" else delta.x())
            self.host._set_region_extent(self.manager.states[self.tool_id].position,
                                         extent + change, overlay=True)
        else:
            edge = self._edge(event.position().toPoint())
            self.setCursor(Qt.CursorShape.SizeVerCursor if edge == "top" else Qt.CursorShape.SizeHorCursor if edge else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        self.drag_origin = None
        self.manager.schedule_save()
        super().mouseReleaseEvent(event)
