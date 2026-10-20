"""可配置的通用工作台外壳；业务通过规格、工厂和 QAction 注入。"""

import sys

from mindmap_docking import DockHost, DockManager
from PySide6.QtCore import QPoint, Qt, Slot
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .specs import ActionSpec, AppSpec


class _TitleBar(QWidget):
    """只显示业务配置的品牌与动作，标题空白处交由系统开始移动窗口。"""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.box = QHBoxLayout(self)
        self.box.setContentsMargins(12, 5, 6, 5)
        self.box.setSpacing(9)
        self.logo = QLabel(self)
        self.box.addWidget(self.logo)
        self.menu = QToolButton(self)
        self.menu.setToolTip("主菜单")
        self.menu.setAccessibleName("主菜单")
        self.menu.clicked.connect(self.show_menu)
        self.box.addWidget(self.menu)
        self.title = QLabel(window.spec.title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.box.addWidget(self.title)
        self.subtitle = QLabel(" / " + window.spec.subtitle)
        self.box.addWidget(self.subtitle)
        self.box.addStretch(1)
        self.action_buttons = []
        for spec in window._action_specs:
            if spec.toolbar:
                self.add_action(window.manager.actions[spec.id])
        self.theme = QToolButton(self)
        self.theme.setToolTip("切换深浅主题")
        self.theme.setAccessibleName("切换深浅主题")
        self.theme.clicked.connect(lambda: window.manager.set_theme("light" if window.manager.theme.dark else "dark"))
        self.box.addWidget(self.theme)
        self.options = QToolButton(self)
        self.options.setToolTip("外观与布局")
        self.options.setAccessibleName("外观与布局")
        self.options.clicked.connect(lambda: window.appearance_menu().exec(self.options.mapToGlobal(QPoint(0, self.options.height()))))
        self.box.addWidget(self.options)
        self.minimize = QToolButton(self)
        self.maximize = QToolButton(self)
        self.close = QToolButton(self)
        for button, label, callback in [
            (self.minimize, "最小化", window.showMinimized),
            (self.maximize, "最大化或还原", window.toggle_maximize),
            (self.close, "关闭窗口", window.close),
        ]:
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.clicked.connect(callback)
            button.setFixedSize(32, 30)
            button.setVisible(not window.spec.native_title_bar)
            self.box.addWidget(button)

    def add_action(self, action):
        button = QToolButton(self)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setDefaultAction(action)
        self.action_buttons.append(button)
        # 运行期间追加业务动作时，仍放在外观和原生窗口控制按钮之前。
        # 初始构造阶段 theme 按钮尚不存在，此时按声明顺序正常追加。
        if hasattr(self, "theme"):
            self.box.insertWidget(self.box.indexOf(self.theme), button)
        else:
            self.box.addWidget(button)

    def refresh_theme(self):
        theme = self.window.manager.theme
        self.setFixedHeight(42 if theme.compact else 52)
        self.logo.setPixmap(theme.icon(self.window.spec.icon, 24).pixmap(26, 26))
        for button, name in [(self.menu, "menu"), (self.theme, "sun" if theme.dark else "moon"),
                             (self.options, "settings"), (self.minimize, "minus"),
                             (self.maximize, "restore" if self.window.isMaximized() else "maximize"),
                             (self.close, "close")]:
            button.setIcon(theme.icon(name))
        self.subtitle.setStyleSheet(f"color:{theme.css('text-muted')};")

    def show_menu(self):
        self.window.main_menu().exec(self.menu.mapToGlobal(QPoint(0, self.menu.height())))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.window.windowHandle():
            self.window.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.toggle_maximize()


class WorkbenchWindow(QMainWindow):
    """可直接使用或继承的公开主窗口；停靠管理器通过 manager 属性访问。

    辅助框架复用同一个 AppSpec 与 QAction，避免业务回调重复注册。
    传入 manager 时由调用方配置存储与主题；外壳不导入任何业务包。
    """

    def __init__(self, spec: AppSpec, *, manager: DockManager | None = None, host_id="main"):
        flags = Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint
        if not spec.native_title_bar:
            flags |= Qt.WindowType.FramelessWindowHint
        super().__init__(None, flags)
        self.spec = spec
        self.host_id = host_id
        self._retiring = False
        self.manager = manager or DockManager(
            application_id=spec.application_id, storage_dir=spec.storage_dir, theme=spec.theme,
            shortcut_context=Qt.ShortcutContext.WindowShortcut,
        )
        if self.manager.frame_factory is None:
            self.manager.frame_factory = lambda item, identifier: WorkbenchWindow(spec, manager=item, host_id=identifier)
        self.setObjectName("mindmap_shell_" + host_id)
        self.setWindowTitle(spec.title + ("" if host_id == "main" else " · " + host_id))
        self.resize(1440 if host_id == "main" else 1100, 900 if host_id == "main" else 760)
        self.setMinimumSize(860, 540)
        self.host = DockHost(self.manager, host_id, self)
        if host_id == "main":
            self._action_specs = list(spec.actions)
            self.manager._business_action_specs = self._action_specs
            for action_spec in spec.actions:
                self._register_action(action_spec)
        else:
            self._action_specs = self.manager._business_action_specs
        body = QWidget(self)
        self.box = QVBoxLayout(body)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)
        self.title_bar = _TitleBar(self)
        self.box.addWidget(self.title_bar)
        self.box.addWidget(self.host, 1)
        self.setCentralWidget(body)
        self.status = self.statusBar()
        self.status_label = QLabel("  " + spec.status_text)
        self.status.addWidget(self.status_label, 1)
        self.status.addPermanentWidget(QLabel("  思维导图工作台 · " + spec.version + "  "))
        self.manager.message.connect(self.show_status_message)
        self.manager.theme_changed.connect(self.refresh_theme)
        self.manager.tool_failed.connect(self.report_tool_error)
        self.refresh_theme()

    def _register_action(self, spec):
        def invoke():
            try:
                spec.callback()
            except Exception as error:
                self.manager.message.emit(f"操作失败：{spec.title}：{error}")
        action = self.manager.register_action(spec.id, spec.title, invoke, shortcut=spec.shortcut, icon=spec.icon)
        action.setCheckable(spec.checkable)
        return action

    def add_action(self, spec: ActionSpec):
        """运行期间追加业务动作，返回可用于按钮或菜单的 QAction。"""
        action = self._register_action(spec)
        self._action_specs.append(spec)
        if spec.toolbar:
            for host in self.manager.hosts.values():
                frame = host.window()
                if isinstance(frame, WorkbenchWindow):
                    frame.title_bar.add_action(action)
        return action

    def action(self, action_id):
        """取得业务或停靠系统的共享动作，用于启用状态、图标和快捷键设置。"""
        return self.manager.actions[action_id]

    @Slot()
    def refresh_theme(self):
        theme = self.manager.theme
        theme.apply(self)
        if self.spec.global_theme:
            theme.apply_global(QApplication.instance())
        self.setWindowIcon(theme.icon(self.spec.icon, 32))
        self.title_bar.refresh_theme()
        self.status.setFixedHeight(24 if theme.compact else 28)
        self.update()

    @Slot(str)
    def show_status_message(self, text):
        self.status.showMessage(text, 6500)

    @Slot(str, str)
    def report_tool_error(self, tool_id, error):
        self.show_status_message(f"无法创建工具窗口 {tool_id}：{error}")

    def toggle_maximize(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()
        self.title_bar.refresh_theme()

    def main_menu(self):
        """根据动作声明动态构建菜单，业务菜单与内建工作区菜单共用动作实例。"""
        menu = QMenu(self)
        groups = {}
        for spec in self._action_specs:
            if spec.menu not in groups:
                groups[spec.menu] = menu.addMenu(spec.menu)
            groups[spec.menu].addAction(self.manager.actions[spec.id])
        workspace = menu.addMenu("工作区")
        workspace.addAction("新建工作区窗口", lambda: self.manager.create_frame())
        workspace.addAction("搜索工具窗口…", self.search_tools)
        tools = workspace.addMenu("工具窗口")
        for tool_id, spec in self.manager.specs.items():
            if self.manager.states[tool_id].available:
                action = tools.addAction(self.manager.theme.icon(spec.icon), spec.title)
                action.triggered.connect(lambda checked=False, item=tool_id: self.activate_tool(item))
        workspace.addSeparator()
        for key in ["hide_active", "hide_all", "editor_focus", "last_tool", "maximize"]:
            workspace.addAction(self.manager.actions[key])
        menu.addMenu(self.appearance_menu())
        menu.addSeparator()
        menu.addAction("关于 " + self.spec.title,
                       lambda: self.show_status_message(f"{self.spec.title} {self.spec.version} · 思维导图工作台 · MIT"))
        menu.addAction("关闭窗口", self.close)
        return menu

    def activate_tool(self, tool_id):
        try:
            self.manager.activate(tool_id)
        except Exception as error:
            self.show_status_message(str(error))

    def appearance_menu(self):
        """公开的外观菜单可由业务放进自己的菜单栏或设置页。"""
        menu = QMenu("外观与布局", self)
        for name, label in [("light", "浅色"), ("dark", "深色")]:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.manager.theme.name == name)
            action.triggered.connect(lambda checked=False, value=name: self.manager.set_theme(value))
        menu.addSeparator()
        for key, label in [("compact", "紧凑模式"), ("show_bars", "显示停靠栏"),
                           ("show_names", "显示工具名称"), ("always_header_icons", "始终显示标题按钮"),
                           ("widescreen", "宽屏布局"), ("left_side_by_side", "左侧并排"),
                           ("right_side_by_side", "右侧并排")]:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(getattr(self.manager.settings, key))
            action.triggered.connect(lambda checked, value=key: self.manager.set_option(value, checked))
        layouts = menu.addMenu("命名布局")
        layouts.addAction("另存为…", lambda: self.manager.prompt_save_layout(self))
        for name in self.manager.layout_names:
            sub = layouts.addMenu(name)
            sub.addAction("应用", lambda checked=False, item=name: self.manager.restore_layout(item))
            if name != "默认":
                sub.addAction("更新", lambda checked=False, item=name: self.manager.save_layout(item))
                sub.addAction("重命名…", lambda checked=False, item=name: self.rename_layout(item))
                action = sub.addAction("删除", lambda checked=False, item=name: self.manager.delete_layout(item))
                action.setEnabled(name != self.manager.current_layout)
        return menu

    def rename_layout(self, name):
        new, accepted = QInputDialog.getText(self, "重命名布局", "新名称：", text=name)
        if accepted:
            try:
                self.manager.rename_layout(name, new)
            except ValueError as error:
                self.show_status_message(str(error))

    def search_tools(self):
        titles = [spec.title for key, spec in self.manager.specs.items() if self.manager.states[key].available]
        value, accepted = QInputDialog.getItem(self, "查找工具窗口", "窗口名称：", titles, editable=False)
        if accepted:
            for key, spec in self.manager.specs.items():
                if spec.title == value:
                    self.activate_tool(key)
                    break

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32" and not self.spec.native_title_bar:
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0084:
                # 集成标题栏仍将边框与最大化按钮的命中结果交给 Windows，保留系统缩放和贴靠。
                point = self.mapFromGlobal(QCursor.pos())
                button = self.title_bar.maximize
                if button.geometry().contains(button.parentWidget().mapFrom(self, point)):
                    return True, 9
                if not self.isMaximized():
                    x, y, edge = point.x(), point.y(), 5
                    left, right, top, bottom = x < edge, x >= self.width() - edge, y < edge, y >= self.height() - edge
                    hit = (13 if left else 14 if right else 12) if top else (16 if left else 17 if right else 15) if bottom else 10 if left else 11 if right else 0
                    if hit:
                        return True, hit
            elif msg.message == 0x00A1 and msg.wParam == 9:
                return True, 0
            elif msg.message == 0x00A2 and msg.wParam == 9:
                self.toggle_maximize()
                return True, 0
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        if self._retiring or self.manager.closing:
            event.accept()
        elif self.host_id != "main":
            self.manager.close_frame(self.host_id)
            event.accept()
        elif self.manager.can_close_documents():
            self.manager.shutdown()
            event.accept()
        else:
            event.ignore()
