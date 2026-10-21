"""可嵌入的停靠工作区；侧边按钮与面板共用状态，偏好持久化交给宿主。"""

from __future__ import annotations

from dataclasses import dataclass, field

from mindmap_engine import FormatRegistry, LayoutRegistry, MindMapEngine, MindMapError
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .icons import _toolbar_icon, apply_toolbar_theme, set_action_icon
from .panels import MindMapPanels
from .ui_style import (
    CARD_INSET,
    CARD_RADIUS,
    HEADER_HEIGHT,
    RAIL_SIZE,
    apply_ui_palette,
    sidebar_surface,
    ui_colors,
    workspace_styles,
)
from .widget import MindMapWidget

_AREAS = {
    "left": Qt.DockWidgetArea.LeftDockWidgetArea,
    "right": Qt.DockWidgetArea.RightDockWidgetArea,
    "bottom": Qt.DockWidgetArea.BottomDockWidgetArea,
}
_BARS = {
    "left": Qt.ToolBarArea.LeftToolBarArea,
    "right": Qt.ToolBarArea.RightToolBarArea,
    "bottom": Qt.ToolBarArea.BottomToolBarArea,
}
_LABELS = {"left": "左侧", "right": "右侧", "bottom": "底部"}
_TOOL_ICONS = {"outline": "outline", "properties": "properties", "settings": "settings"}


@dataclass
class _ToolWindow:
    """界面偏好，不含节点或文档内容；浮动时仍保留一个归属按钮栏。"""

    dock: QDockWidget
    action: QAction
    button: QToolButton
    widget_action: QWidgetAction
    area: str
    visible: bool = False
    floating: bool = False
    width: int = 360
    height: int = 260
    geometry: list[int] = field(default_factory=lambda: [120, 120, 360, 580])


class _TitleBar(QWidget):
    """原生停靠布局按 sizeHint 分配标题高度，必须与固定高度保持一致。"""

    def sizeHint(self):
        return QSize(super().sizeHint().width(), HEADER_HEIGHT)

    def minimumSizeHint(self):
        return QSize(super().minimumSizeHint().width(), HEADER_HEIGHT)


class _Dock(QDockWidget):
    def __init__(self, owner, tool_id, title):
        super().__init__(title, owner)
        self.owner, self.tool_id = owner, tool_id
        self.setObjectName("tool_" + tool_id)
        self.setProperty("mindmap_tool_dock", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setAllowedAreas(_AREAS["left"] | _AREAS["right"] | _AREAS["bottom"])
        self.setMinimumSize(220 if tool_id == "outline" else 280, 160)

    def closeEvent(self, event):
        # 浮动窗口关闭与停靠面板的隐藏按钮使用相同路径；不能销毁表单或丢失按钮。
        self.owner.hide_tool_window(self.tool_id)
        event.ignore()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.owner._capture_size(self.tool_id)

    def moveEvent(self, event):
        super().moveEvent(event)
        self.owner._capture_size(self.tool_id)


class _RailButton(QToolButton):
    """侧边按钮自己拥有拖动手势；释放前不改变停靠状态，Escape 可完整取消。"""

    def __init__(self, owner, tool_id):
        super().__init__(owner)
        self.owner, self.tool_id = owner, tool_id
        self._press = None
        self._dragging = self._cancelled = False
        self.setFixedSize(32, 32)
        self.setIconSize(QSize(20, 20))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._cancelled = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            point = event.globalPosition().toPoint()
            if (
                not self._dragging
                and (point - self._press).manhattanLength() >= QApplication.startDragDistance()
            ):
                self._dragging = True
                self.setDown(False)
                self.owner._begin_drag(self.tool_id)
            if self._dragging:
                self.owner._update_drag(point)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging or self._cancelled:
            dragging, self._dragging = self._dragging, False
            self._press = None
            self.setDown(False)
            if dragging:
                self.owner._finish_drag(event.globalPosition().toPoint())
            self._cancelled = False
            event.accept()
            return
        self._press = None
        super().mouseReleaseEvent(event)


class _DropOverlay(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        owner = self.owner
        if owner._drop_area is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(owner.canvas._document.theme.accent)
        painter.setPen(QPen(color, 2))
        color.setAlpha(40)
        painter.setBrush(color)
        rect = owner._preview_rect(owner._drop_area)
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), CARD_RADIUS, CARD_RADIUS)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "停靠到" + _LABELS[owner._drop_area])
        painter.drawLine(*owner._insertion_line())


class MindMapWorkspace(QMainWindow):
    """包含画布、大纲、节点属性和全局设置的原生停靠工作区。

    可作为普通 QWidget 嵌入宿主；canvas 保留完整 MindMapWidget 接口。构造参数
    editable、toolbar 及两个注册表原样传给画布。组件不访问 QSettings 或导图文件，
    宿主通过 state_changed、save_state / restore_state 管理本机布局偏好。
    """

    state_changed = Signal()

    def __init__(
        self,
        engine: MindMapEngine | None = None,
        parent=None,
        *,
        editable=True,
        toolbar=True,
        layout_registry: LayoutRegistry | None = None,
        format_registry: FormatRegistry | None = None,
    ):
        super().__init__(parent, Qt.WindowType.Widget)
        # QMainWindow 构造器会补入 Window 标志，需在构造完成后显式改回可嵌入控件。
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setObjectName("mindmap_workspace")
        self.setDockOptions(QMainWindow.DockOption(0))
        self.tools, self.rails, self._spacers = {}, {}, {}
        self._order = {area: [] for area in _AREAS}
        self._moving = self._disposed = False
        self._drag_id = self._drop_area = None
        self._drop_index = 0
        self._ghost = None
        self._theme_signature = None
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(200)
        self._state_timer.timeout.connect(self.state_changed.emit)
        self.canvas = MindMapWidget(
            engine,
            self,
            editable=editable,
            toolbar=False,
            layout_registry=layout_registry,
            format_registry=format_registry,
        )
        self.canvas.setMinimumSize(320, 200)
        self.setCentralWidget(self.canvas)
        # 工具栏提到整个工作区上沿，侧栏展开后不会挤占或分割原有的一行工具入口。
        self.canvas.layout().removeWidget(self.canvas.toolbar)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.canvas.toolbar)
        self.canvas.toolbar.setVisible(toolbar)
        for area, bar_area in _BARS.items():
            bar = QToolBar(_LABELS[area] + "工具窗口", self)
            bar.setObjectName("rail_" + area)
            bar.setMovable(False)
            bar.setFloatable(False)
            bar.setAllowedAreas(bar_area)
            bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
            if area == "bottom":
                bar.setFixedHeight(RAIL_SIZE)
            else:
                bar.setFixedWidth(RAIL_SIZE)
            self.addToolBar(bar_area, bar)
            self.rails[area] = bar
            spacer = QWidget(bar)
            spacer.setMinimumSize(1, 1)
            spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._spacers[area] = bar.addWidget(spacer)
        self.panels = MindMapPanels(self.canvas, self)
        self.properties, self.outline, self.settings = (
            self.panels.properties, self.panels.outline, self.panels.settings
        )
        self._add_tool("outline", "大纲", "Alt+1", self.outline, "left")
        self._add_tool("properties", "节点属性", "Alt+2", self.properties, "right")
        self._add_tool("settings", "全局设置", "Alt+3", self.settings, "right")
        self.canvas.set_properties_handler(lambda: self.show_tool_window("properties"))
        self.canvas.document_changed.connect(lambda event: self._apply_theme())
        self.reset_layout_action = QAction("恢复默认布局", self)
        self.reset_layout_action.triggered.connect(lambda: self.restore_state(None))
        self._overlay = _DropOverlay(self)
        self._overlay.hide()
        self.restore_state(None)
        self._apply_theme()

    def createPopupMenu(self):
        """只公开工具窗口菜单，避免 Qt 原生菜单额外隐藏常驻按钮栏。"""
        return None

    def _tool(self, tool_id):
        if tool_id not in self.tools:
            raise MindMapError(f"工具窗口不存在：{tool_id}")
        return self.tools[tool_id]

    def _add_tool(self, tool_id, title, shortcut, content, area):
        dock = _Dock(self, tool_id, title)
        # 原生视口、滚动区会绘制矩形背景，底部保留圆角半径的空隙，防止覆盖
        # QDockWidget 的两个底角；标题和内容都使用同一块面板底色，不建立第二层卡片。
        body = QWidget(dock)
        body.setObjectName("tool_window_body")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, CARD_INSET)
        layout.setSpacing(0)
        layout.addWidget(content)
        dock.setWidget(body)
        action = QAction(title, self, checkable=True)
        action.setShortcut(QKeySequence(shortcut))
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.addAction(action)
        dock.addAction(action)
        action.triggered.connect(lambda checked=False: self._toggle_tool(tool_id))
        button = _RailButton(self, tool_id)
        button.setDefaultAction(action)
        button.setAccessibleName(title)
        button.setToolTip(f"{title}（{shortcut}）；可拖动到左侧、右侧、底部或窗口外")
        widget_action = QWidgetAction(self)
        widget_action.setDefaultWidget(button)
        record = _ToolWindow(dock, action, button, widget_action, area)
        self.tools[tool_id] = record
        dock.setTitleBarWidget(self._title_bar(tool_id, title))
        self.addDockWidget(_AREAS[area], dock)
        self._order[area].append(tool_id)
        dock.hide()
        dock.visibilityChanged.connect(lambda visible: self._visibility_changed(tool_id))
        dock.topLevelChanged.connect(lambda floating: self._native_location_changed(tool_id))
        dock.dockLocationChanged.connect(lambda location: self._native_location_changed(tool_id))

    def _title_bar(self, tool_id, title):
        header = _TitleBar(self)
        header.setObjectName("tool_window_header")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        header.setFixedHeight(HEADER_HEIGHT)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 0, 6, 0)
        label = QLabel(title, header)
        label.setObjectName("tool_window_title")
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)
        layout.addStretch()
        if tool_id == "outline":
            # 复用面板动作和菜单；独立工作区不访问 QSettings，偏好持久化由宿主负责。
            depth = QToolButton(header)
            depth.setObjectName("outline_display_depth")
            depth.setAutoRaise(True)
            depth.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            depth.setDefaultAction(self.outline.depth_action)
            depth.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            depth.setFixedSize(24, 24)
            depth.setIconSize(QSize(14, 14))
            depth.setStyleSheet("QToolButton::menu-indicator { image:none; width:0; height:0; }")
            depth.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            depth.setAccessibleName("大纲显示层级数")
            layout.addWidget(depth)
        menu = QToolButton(header)
        menu.setObjectName("tool_window_positions")
        menu.setFixedSize(24, 24)
        menu.setIconSize(QSize(16, 16))
        menu.setAccessibleName(title + "位置菜单")
        menu.setToolTip("移动或浮动面板")
        menu.clicked.connect(lambda: self._position_menu(tool_id, menu))
        hide = QToolButton(header)
        hide.setObjectName("tool_window_hide")
        hide.setFixedSize(24, 24)
        hide.setIconSize(QSize(16, 16))
        hide.setAccessibleName("隐藏" + title)
        hide.setToolTip("隐藏面板")
        hide.clicked.connect(lambda: self.hide_tool_window(tool_id))
        layout.addWidget(menu)
        layout.addWidget(hide)
        return header

    def _position_menu(self, tool_id, button):
        menu = QMenu(button)
        for area, label in _LABELS.items():
            action = menu.addAction("移至" + label)
            action.triggered.connect(lambda checked=False, target=area: self._move_tool(tool_id, target))
        menu.addSeparator()
        menu.addAction("浮动窗口", lambda: self._move_tool(tool_id, self.tools[tool_id].area, floating=True))
        menu.addAction("隐藏", lambda: self.hide_tool_window(tool_id))
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(button.mapToGlobal(QPoint(0, button.height())))

    def _toggle_tool(self, tool_id):
        if self.tools[tool_id].visible:
            self.hide_tool_window(tool_id)
        else:
            self.show_tool_window(tool_id)

    def show_tool_window(self, tool_id: str, *, focus: bool = True) -> None:
        """展开指定工具窗口，保留上次停靠或浮动模式；同侧其他面板先隐藏。"""
        record = self._tool(tool_id)
        if self._disposed:
            return
        if tool_id in ("properties", "settings"):
            self.canvas.finish_edit(commit=True)
        self._hide_peers(tool_id)
        record.visible = True
        record.dock.show()
        if not record.floating:
            self._resize_tool(record)
        record.dock.raise_()
        record.action.setChecked(True)
        if focus:
            targets = {
                "properties": self.properties.fields["style.font_family"],
                "outline": self.outline,
                "settings": self.settings.wheel,
            }
            target = targets[tool_id]
            (target if target.isEnabled() else record.dock.widget()).setFocus()
        self._schedule_state()

    def hide_tool_window(self, tool_id: str) -> None:
        """隐藏而不销毁面板；有效输入先提交，之后重新读取正式状态以清除无效草稿。"""
        record = self._tool(tool_id)
        self.flush_pending_edits()
        self._capture_size(tool_id)
        record.visible = False
        record.dock.hide()
        record.action.setChecked(False)
        if tool_id == "properties":
            self.properties.discard_pending()
            self.properties.sync()
        self._schedule_state()

    def flush_pending_edits(self) -> None:
        """保存、导出、切换面板及宿主生命周期操作前提交已完成的有效侧栏输入。"""
        if not self._disposed:
            self.panels.flush_pending_edits()

    def _hide_peers(self, tool_id):
        record = self.tools[tool_id]
        if record.floating:
            return
        for other_id, other in self.tools.items():
            if other_id != tool_id and other.area == record.area and other.visible and not other.floating:
                self.hide_tool_window(other_id)

    def _resize_tool(self, record):
        horizontal = record.area != "bottom"
        self.resizeDocks(
            [record.dock],
            [record.width if horizontal else record.height],
            Qt.Orientation.Horizontal if horizontal else Qt.Orientation.Vertical,
        )

    def _capture_size(self, tool_id):
        if self._moving or self._disposed or tool_id not in self.tools:
            return
        record = self.tools[tool_id]
        if record.visible and record.dock.isVisible():
            previous = (record.width, record.height, tuple(record.geometry))
            if record.floating:
                rect = record.dock.geometry()
                record.geometry = [rect.x(), rect.y(), rect.width(), rect.height()]
            elif record.area == "bottom":
                record.height = max(160, record.dock.height())
            else:
                record.width = max(record.dock.minimumWidth(), record.dock.width())
            if previous != (record.width, record.height, tuple(record.geometry)):
                self._schedule_state()

    def _visibility_changed(self, tool_id):
        if self._moving or self._disposed:
            return
        record = self.tools[tool_id]
        # 父窗口隐藏、最小化也会发送 visibilityChanged(False)，不能据此覆盖用户展开偏好。
        record.visible = not record.dock.isHidden()
        record.action.setChecked(record.visible)
        self._schedule_state()

    def _native_location_changed(self, tool_id):
        """原生标题栏拖动和侧边按钮拖动统一回写位置与互斥状态。"""
        if self._moving or self._disposed:
            return
        record = self.tools[tool_id]
        record.floating = record.dock.isFloating()
        if not record.floating:
            area = next(
                (name for name, value in _AREAS.items() if self.dockWidgetArea(record.dock) == value),
                record.area,
            )
            if area != record.area:
                self._order[record.area].remove(tool_id)
                record.area = area
                self._order[area].append(tool_id)
                self._rebuild_rails()
            if record.visible:
                self._hide_peers(tool_id)
        self._schedule_state()

    def _move_tool(self, tool_id, area, index=None, *, floating=False, position=None):
        """停靠迁移只修改界面；提交输入后在事件抑制区一次更新原生容器及按钮顺序。"""
        record = self._tool(tool_id)
        if area not in _AREAS:
            raise MindMapError("工具窗口只能停靠到左侧、右侧或底部")
        self.flush_pending_edits()
        self._capture_size(tool_id)
        self._moving = True
        try:
            self._order[record.area].remove(tool_id)
            order = self._order[area]
            order.insert(len(order) if index is None else max(0, min(index, len(order))), tool_id)
            record.area, record.floating = area, floating
            record.dock.setFloating(False)
            self.addDockWidget(_AREAS[area], record.dock)
            if floating:
                record.dock.setFloating(True)
                geometry = list(record.geometry)
                if position is not None:
                    geometry[:2] = [position.x() - 80, position.y() - 16]
                record.dock.setGeometry(self._clamp_geometry(geometry))
            self._rebuild_rails()
        finally:
            self._moving = False
        self.show_tool_window(tool_id, focus=False)

    def _rebuild_rails(self):
        for bar in self.rails.values():
            for record in self.tools.values():
                bar.removeAction(record.widget_action)
        for area, order in self._order.items():
            bar = self.rails[area]
            for tool_id in order:
                bar.insertAction(self._spacers[area], self.tools[tool_id].widget_action)
            bar.show()

    @staticmethod
    def _clamp_geometry(values):
        """屏幕数量、缩放或坐标变化后，浮动窗口整体收回可用区域，标题栏始终可达。"""
        rect = QRect(*values)
        screen = QApplication.screenAt(rect.center()) or QApplication.primaryScreen()
        available = screen.availableGeometry()
        width = min(max(280, rect.width()), available.width())
        height = min(max(180, rect.height()), available.height())
        return QRect(
            max(available.left(), min(rect.x(), available.right() - width + 1)),
            max(available.top(), min(rect.y(), available.bottom() - height + 1)),
            width,
            height,
        )

    def save_state(self) -> dict:
        """返回版本 1 的普通 JSON 字典，仅包含位置、顺序、尺寸及浮动几何。

        本接口不保存偏好，不接触引擎历史。宿主可原样编码后放入自己的 QSettings。
        """
        for tool_id in self.tools:
            self._capture_size(tool_id)
        return {
            "version": 1,
            "order": {area: list(order) for area, order in self._order.items()},
            "tools": {
                tool_id: {
                    "area": record.area,
                    "visible": record.visible,
                    "floating": record.floating,
                    "width": record.width,
                    "height": record.height,
                    "geometry": list(record.geometry),
                }
                for tool_id, record in self.tools.items()
            },
        }

    def restore_state(self, state) -> None:
        """恢复已知面板；None 或损坏数据使用默认布局，未知标识忽略且不修改文档。"""
        self.flush_pending_edits()
        self._cancel_drag()
        valid = isinstance(state, dict) and type(state.get("version")) is int and state["version"] == 1
        source = state.get("tools", {}) if valid else {}
        source = source if isinstance(source, dict) else {}
        self._moving = True
        try:
            self._order = {area: [] for area in _AREAS}
            for tool_id, record in self.tools.items():
                record.dock.hide()
                record.dock.setFloating(False)
                value = source.get(tool_id, {})
                value = value if isinstance(value, dict) else {}
                default_area = "left" if tool_id == "outline" else "right"
                area = value.get("area", default_area)
                if not isinstance(area, str) or area not in _AREAS:
                    value, area = {}, default_area
                record.area = area
                record.visible = (
                    value.get("visible", tool_id == "properties")
                    if type(value.get("visible", True)) is bool
                    else tool_id == "properties"
                )
                record.floating = (
                    value.get("floating", False) if type(value.get("floating", False)) is bool else False
                )
                for name, default, minimum in (
                    ("width", 260 if tool_id == "outline" else 360, 220 if tool_id == "outline" else 280),
                    ("height", 260, 160),
                ):
                    number = value.get(name, default)
                    setattr(record, name, max(minimum, min(number, 2400)) if type(number) is int else default)
                geometry = value.get("geometry", [120, 120, record.width, 580])
                if (
                    not isinstance(geometry, list)
                    or len(geometry) != 4
                    or not all(type(n) is int and abs(n) < 1000000 for n in geometry)
                ):
                    geometry = [120, 120, record.width, 580]
                rect = self._clamp_geometry(geometry)
                record.geometry = [rect.x(), rect.y(), rect.width(), rect.height()]
                self.addDockWidget(_AREAS[area], record.dock)
                if record.floating:
                    record.dock.setFloating(True)
                    record.dock.setGeometry(rect)
                self._order[area].append(tool_id)
            saved_order = state.get("order", {}) if valid else {}
            for area, order in self._order.items():
                requested = saved_order.get(area, []) if isinstance(saved_order, dict) else []
                if isinstance(requested, list):
                    known = []
                    for key in requested:
                        if isinstance(key, str) and key in order and key not in known:
                            known.append(key)
                    self._order[area] = known + [key for key in order if key not in known]
                active = False
                for key in self._order[area]:
                    record = self.tools[key]
                    if record.visible and not record.floating:
                        record.visible = not active
                        active = True
            self._rebuild_rails()
            for record in self.tools.values():
                record.dock.setVisible(record.visible)
                record.action.setChecked(record.visible)
                if record.visible and not record.floating:
                    self._resize_tool(record)
        finally:
            self._moving = False
        self._schedule_state()

    def _schedule_state(self):
        if not self._moving and not self._disposed:
            self._state_timer.start()

    def _outline_selected(self, ids):
        """兼容旧工作区入口，导航由宿主无关的面板组合统一处理。"""
        self.panels._outline_selected(ids)

    def _apply_theme(self):
        theme = self.canvas.engine.document.theme
        signature = ui_colors(theme)
        if signature == self._theme_signature:
            return
        self._theme_signature = signature
        # 外壳绘制标题和底部圆角留白，三个内容控件各自绘制滚动区；统一读取
        # 侧栏底色，避免停靠、浮动或反复切换主题后出现异色条带。仅更新样式，
        # 不重建控件或重载表单，保留输入焦点、光标、滚动位置与待提交内容。
        self.setStyleSheet(workspace_styles(signature, sidebar=sidebar_surface(theme)))
        apply_ui_palette(self, signature, window=signature.shell)
        for bar in self.rails.values():
            apply_toolbar_theme(bar, theme)
        for tool_id, record in self.tools.items():
            set_action_icon(record.action, _TOOL_ICONS[tool_id], theme)
            header = record.dock.titleBarWidget()
            for name, icon in (("positions", "panel_menu"), ("hide", "panel_hide")):
                header.findChild(QToolButton, "tool_window_" + name).setIcon(
                    _toolbar_icon(icon, signature.muted, signature.muted, signature.accent)
                )
        self.properties.set_theme(theme)
        self.outline.set_theme(theme)
        self.settings.set_theme(theme)

    def _begin_drag(self, tool_id):
        self._cancel_drag()
        self._drag_id = tool_id
        self._ghost = QLabel(
            self.tools[tool_id].action.text(),
            self,
            Qt.WindowType.ToolTip | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._ghost.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        theme = self.canvas._document.theme
        self._ghost.setStyleSheet(
            f"QLabel {{ background: {theme.accent}; color: {theme.background}; "
            "padding: 6px 10px; border-radius: 5px; }"
        )
        self._ghost.adjustSize()
        self._ghost.show()
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()
        self._overlay.show()
        QApplication.instance().installEventFilter(self)

    def _update_drag(self, global_point):
        if self._drag_id is None:
            return
        self._ghost.move(global_point + QPoint(12, 12))
        point = self.mapFromGlobal(global_point)
        self._drop_area = None
        if self.rect().contains(point):
            # 工具栏可隐藏，卡片内部也有留白。按按钮栏真实几何映射命中区域，
            # 不再假设画布或工具栏相对工作区的固定偏移；向内容侧扩展 8 像素便于拖入。
            for area in ("bottom", "left", "right"):
                rect = self._rail_rect(area)
                rect = (
                    rect.adjusted(0, -CARD_RADIUS, 0, 0)
                    if area == "bottom"
                    else rect.adjusted(-CARD_RADIUS, 0, CARD_RADIUS, 0)
                )
                if rect.contains(point):
                    self._drop_area = area
                    break
        if self._drop_area:
            horizontal = self._drop_area == "bottom"
            coordinate = global_point.x() if horizontal else global_point.y()
            order = [key for key in self._order[self._drop_area] if key != self._drag_id]
            self._drop_index = len(order)
            for index, key in enumerate(order):
                button = self.tools[key].button
                center = button.mapToGlobal(button.rect().center())
                if coordinate < (center.x() if horizontal else center.y()):
                    self._drop_index = index
                    break
        self._overlay.update()

    def _rail_rect(self, area):
        """把实际按钮栏映射到工作区坐标，供命中、插入线和停靠预览共用。"""
        bar = self.rails[area]
        return QRect(bar.mapTo(self, QPoint()), bar.size())

    def _preview_rect(self, area):
        record = self.tools[self._drag_id]
        card = self.canvas._view_card
        top = card.mapTo(self, QPoint()).y()
        left, right = self._rail_rect("left").right() + 1, self._rail_rect("right").left()
        # 左右停靠区域到当前中央卡片底部为止，不能覆盖已经展开的底部面板。
        # 底部目标则以底部按钮栏为基准，表示替换／展开该区域后的实际位置。
        rect = QRect(left, top, right - left, card.height())
        if area == "left":
            return QRect(rect.left(), rect.top(), min(record.width, rect.width() // 2), rect.height())
        if area == "right":
            width = min(record.width, rect.width() // 2)
            return QRect(rect.right() + 1 - width, rect.top(), width, rect.height())
        bottom = self._rail_rect("bottom").top()
        height = min(record.height, (bottom - top) // 2)
        return QRect(rect.left(), bottom - height, rect.width(), height)

    def _insertion_line(self):
        rect = self._rail_rect(self._drop_area)
        order = [key for key in self._order[self._drop_area] if key != self._drag_id]
        horizontal = self._drop_area == "bottom"
        if order:
            key = order[min(self._drop_index, len(order) - 1)]
            button = self.tools[key].button
            pos = button.mapTo(self, QPoint())
            coordinate = pos.x() if horizontal else pos.y()
            if self._drop_index == len(order):
                coordinate += button.width() if horizontal else button.height()
        else:
            coordinate = rect.left() + 4 if horizontal else rect.top() + 4
        return (
            (QPoint(coordinate, rect.top() + 3), QPoint(coordinate, rect.bottom() - 3))
            if horizontal
            else (QPoint(rect.left() + 3, coordinate), QPoint(rect.right() - 3, coordinate))
        )

    def _finish_drag(self, global_point):
        tool_id, area, index = self._drag_id, self._drop_area, self._drop_index
        outside = not self.rect().contains(self.mapFromGlobal(global_point))
        self._cancel_drag()
        if tool_id is None:
            return
        if area:
            self._move_tool(tool_id, area, index)
        elif outside:
            self._move_tool(tool_id, self.tools[tool_id].area, floating=True, position=global_point)

    def _cancel_drag(self):
        if self._drag_id is not None:
            button = self.tools[self._drag_id].button
            button._dragging = False
            button._cancelled = True
            button.setDown(False)
            QApplication.instance().removeEventFilter(self)
        self._drag_id = self._drop_area = None
        if self._ghost is not None:
            self._ghost.hide()
            self._ghost.deleteLater()
            self._ghost = None
        if hasattr(self, "_overlay"):
            self._overlay.hide()

    def eventFilter(self, watched, event):
        if (
            self._drag_id
            and event.type() == QEvent.Type.ShortcutOverride
            and event.key() == Qt.Key.Key_Escape
        ):
            # 先拦截快捷键解析阶段，否则画布的 Escape 动作会提前消费拖动取消键。
            event.accept()
            return True
        if self._drag_id and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self._cancel_drag()
            return True
        return super().eventFilter(watched, event)

    def dispose(self):
        """停止待提交输入、布局通知、拖动和网络请求；可重复调用。"""
        if self._disposed:
            return
        self._disposed = True
        self._state_timer.stop()
        self._cancel_drag()
        self.panels.dispose()
        self.canvas.dispose()
        for record in self.tools.values():
            record.dock.hide()
