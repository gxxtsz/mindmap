"""可配置的通用工作台外壳；业务通过规格、工厂和 QAction 注入。"""

import sys

from mindmap_docking import DockHost, DockManager
from PySide6.QtCore import QEvent, QPoint, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ._windows_frame import _create_windows_frame, _preserve_windows_placement, _track_nonclient_mouse_leave
from .specs import ActionSpec, AppSpec


class _WindowOutline(QWidget):
    """覆盖在主窗口上的一像素轮廓；不参与布局，也不截获鼠标或键盘输入。"""

    def __init__(self, window):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        theme = self.parentWidget().manager.theme
        painter = QPainter(self)
        painter.setPen(QPen(theme.color("main-window-outline"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 画笔中心内缩半个逻辑像素，保证四边等粗且不挤占内部控件的空间。
        painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class _WindowControlButton(QToolButton):
    """轻量窗口按钮；Qt 与 Windows 原生鼠标消息共用同一套悬浮、按下反馈。"""

    def __init__(self, title_bar, role):
        super().__init__(title_bar)
        self.title_bar = title_bar
        self.role = role
        self._hovered = False
        self._native_hovered = False
        self._native_pressed = False
        self._tracking_native_leave = False
        self.setProperty("windowControlHovered", False)
        # 图标缩为 16 逻辑像素，保留更宽的点击区域；SVG 的实际线宽约为 1.13px。
        # 尺寸固定使悬浮和按下只改变颜色，不移动图形或挤压相邻按钮。
        self.setFixedSize(40, 32)
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def refresh_theme(self):
        """同步深浅主题、置顶及最大化状态；切换主题时保留当前悬停反馈。"""
        theme = self.title_bar.window.manager.theme
        hover = theme.color("window-control-bg-hovered")
        pressed = theme.color("window-control-bg-pressed")
        if self.role == "close":
            hover, pressed = QColor("#C42B1C"), QColor("#A82318")
        self.setStyleSheet(f"""
            QToolButton {{ background:transparent; border:1px solid transparent;
                border-radius:4px; padding:0; }}
            QToolButton:checked {{ background:{theme.css('toolbar-selected-bg-active')};
                border-color:{theme.css('control-brand-border')}; }}
            QToolButton[windowControlHovered="true"] {{ background:{hover.name()}; }}
            QToolButton:pressed {{ background:{pressed.name()}; }}
            QToolButton:focus {{ border-color:{theme.css('control-brand-border')}; }}
        """)
        if self.role == "maximize":
            label = "还原窗口" if self.title_bar.window.isMaximized() else "最大化窗口"
            self.setToolTip(label)
            self.setAccessibleName(label)
        elif self.role == "pin":
            pinned = self.title_bar.window.is_always_on_top()
            # 实际窗口标志是唯一状态来源；主题刷新和启动恢复只同步外观，
            # 阻断 toggled，避免重复改动原生窗口或反复写入用户偏好。
            with QSignalBlocker(self):
                self.setChecked(pinned)
            label = "取消置顶" if pinned else "置顶窗口"
            self.setToolTip(label)
            self.setAccessibleName(label)
        self._refresh_icon()

    def _set_hovered(self, hovered: bool) -> None:
        """更新共同的悬浮属性；只在状态改变时重算 QSS，避免移动过程中重复刷新。"""
        hovered = hovered and self.isEnabled()
        if hovered == self._hovered:
            return
        self._hovered = hovered
        self.setProperty("windowControlHovered", hovered)
        # HTMAXBUTTON 属于非客户区，Qt 的 :hover 不一定更新；显式属性驱动两条输入路径。
        self.style().unpolish(self)
        self.style().polish(self)
        self._refresh_icon()
        self.update()

    def set_native_hovered(self, hovered: bool) -> None:
        """接收原生命中状态，并监听移出窗口；主题切换不会覆盖当前输入状态。"""
        self._native_hovered = hovered and self.isEnabled() and self.isVisible()
        self._set_hovered(self._native_hovered)
        if self._native_hovered and not self._native_pressed and not self._tracking_native_leave:
            self._tracking_native_leave = _track_nonclient_mouse_leave(int(self.window().winId()))

    def begin_native_press(self) -> None:
        """接管最大化按钮的原生左键按下，抓取鼠标以接收按钮外部的移动和松开。"""
        if self._native_pressed or not self.isEnabled() or not self.isVisible():
            return
        self._native_pressed = True
        self.set_native_hovered(True)
        self.setDown(True)
        self.grabMouse()
        self.pressed.emit()

    def finish_native_press(self, *, activate: bool = False) -> None:
        """结束原生按压；只有在按钮内松开才激活，取消、失焦及失去抓取均不触发动作。

        先清除按压标记再释放抓取，避免同步到来的 UngrabMouse／WM_CAPTURECHANGED
        重入结束流程。激活后重新命中光标，防止最大化改变按钮位置而留下旧底色。
        """
        was_pressed = self._native_pressed
        self._native_pressed = False
        self.setDown(False)
        if was_pressed and QWidget.mouseGrabber() is self:
            self.releaseMouse()
        if was_pressed:
            self.released.emit()
            if activate and self.isEnabled() and self.isVisible():
                self.clicked.emit()
        hovered = activate and self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self.set_native_hovered(hovered)

    def _refresh_icon(self):
        """复用主题图标；置顶选中时使用强调色，关闭悬停时使用白色。"""
        theme = self.title_bar.window.manager.theme
        name = self.role
        if name == "minimize":
            name = "minus"
        elif name == "maximize" and self.title_bar.window.isMaximized():
            name = "restore"
        color = "#FFFFFF" if self.role == "close" and self._hovered else theme.color("text-default").name()
        if self.role == "pin" and self.isChecked():
            # SVG 供应器需要十六进制颜色；QSS 使用的 rgba(...) 会使 SVG 图形不可见。
            color = theme.color("control-brand-border").name()
        self.setIcon(theme.icons.get_icon(name, color=color, size=self.iconSize().width()))

    def enterEvent(self, event):
        super().enterEvent(event)
        self._set_hovered(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        # Qt 可能在进入原生最大化区域时发送 Leave，原生悬浮状态由系统离开消息撤销。
        self._set_hovered(self._native_hovered)

    def event(self, event):
        """控件隐藏、被禁用或鼠标抓取被接管时撤销原生按压，避免出现卡住的背景。"""
        if event.type() in (QEvent.Type.Hide, QEvent.Type.UngrabMouse) and self._native_pressed:
            self.finish_native_press()
        elif event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.finish_native_press()
        return super().event(event)


class _TitleBar(QWidget):
    """只显示应用图标与配置动作，标题空白处交由系统开始移动窗口。"""

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("mindmap_shell_title_bar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.window = window
        self.box = QHBoxLayout(self)
        self.box.setContentsMargins(12, 3, 6, 3)
        self.box.setSpacing(9)
        # 应用名称由系统窗口标题和图标提示提供；顶栏左侧仅保留图标与菜单，
        # 不再重复绘制主副标题，同时为辅助技术保留可读的应用名称。
        self.logo = QLabel(self)
        self.logo.setToolTip(window.spec.title)
        self.logo.setAccessibleName(window.spec.title)
        self.box.addWidget(self.logo)
        self.menu = QToolButton(self)
        self.menu.setToolTip("主菜单")
        self.menu.setAccessibleName("主菜单")
        self.menu.clicked.connect(self.show_menu)
        self.box.addWidget(self.menu)
        self.navigation = None
        self.box.addStretch(1)
        self.action_buttons = []
        for spec in window._action_specs:
            if spec.toolbar:
                self.add_action(window.manager.actions[spec.id])
        # 设置统一从左侧主菜单进入，不再创建右侧齿轮及其占位。
        # 主菜单保持正方形命中区域，清除通用留白，避免挤压图标。
        self.menu.setProperty("titleBarAction", True)
        self.menu.setFixedSize(32, 32)
        self.menu.setIconSize(QSize(20, 20))
        self.menu.setStyleSheet("QToolButton { padding:0; }")
        self.pin = _WindowControlButton(self, "pin")
        self.pin.setCheckable(True)
        self.minimize = _WindowControlButton(self, "minimize")
        self.maximize = _WindowControlButton(self, "maximize")
        self.close = _WindowControlButton(self, "close")
        # 窗口按钮独立成组，组内间距为 2px；菜单和业务动作继续沿用标题栏原有间距。
        # 只增加子布局，按钮仍以标题栏为父控件，原生命中坐标和控件所有权不变。
        self.window_controls = QHBoxLayout()
        self.window_controls.setContentsMargins(0, 0, 0, 0)
        self.window_controls.setSpacing(2)
        self.box.addLayout(self.window_controls)
        for button, label, callback in [
            (self.pin, "置顶窗口", window.set_always_on_top),
            (self.minimize, "最小化", window.showMinimized),
            (self.maximize, "最大化或还原", window.toggle_maximize),
            (self.close, "关闭窗口", window.close),
        ]:
            button.setToolTip(label)
            button.setAccessibleName(label)
            if button is self.pin:
                button.toggled.connect(callback)
            else:
                button.clicked.connect(callback)
            button.setVisible(not window.spec.native_title_bar)
            self.window_controls.addWidget(button)

    def add_action(self, action):
        """将业务动作加入顶栏，并标记为透明工具按钮；动态追加时沿用当前主题。"""
        button = QToolButton(self)
        button.setProperty("titleBarAction", True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setDefaultAction(action)
        self.action_buttons.append(button)
        # 窗口按钮组始终是主布局的最后一项，动态业务动作稳定插入到该组之前。
        # 初始构造阶段窗口按钮尚未创建，此时按声明顺序正常追加。
        if hasattr(self, "window_controls"):
            self.box.insertWidget(self.box.count() - 1, button)
        else:
            self.box.addWidget(button)

    def refresh_theme(self):
        """同步顶栏尺寸、底色和按钮状态；文档标签继续由各自的编辑分屏持有。"""
        theme = self.window.manager.theme
        # 仅匹配显式标记的顶栏按钮；窗口控制按钮保留独立的关闭警示色和绘制规则。
        # 默认透明使图标融入外围背景，预留透明边框避免键盘聚焦时发生尺寸变化。
        self.setStyleSheet(f"""
            QToolButton[titleBarAction="true"] {{
                background:transparent; border:1px solid transparent; border-radius:5px;
                padding:4px 12px;
            }}
            QToolButton[titleBarAction="true"]:hover {{
                background:{theme.css('toolbar-bg-hovered')};
            }}
            QToolButton[titleBarAction="true"]:pressed,
            QToolButton[titleBarAction="true"]:checked {{
                background:{theme.css('toolbar-selected-bg-active')};
            }}
            QToolButton[titleBarAction="true"]:focus {{
                border-color:{theme.css('control-brand-border')};
            }}
            QToolButton[titleBarAction="true"]:disabled {{
                background:transparent; border-color:transparent; color:{theme.css('text-muted')};
            }}
        """)
        # 双层布局保留独立标签栏；菜单仍为 32×32，窗口按钮为 40×32，不增加顶栏高度。
        # 普通模式上下各留 3px，紧凑模式各留 2px，避免固定高度挤压按钮。
        # 业务动作使用上方的局部内边距，保证带文字的工具按钮也能完整放入这一行。
        vertical = 2 if theme.compact else 3
        self.box.setContentsMargins(12, vertical, 6, vertical)
        self.setFixedHeight(32 + vertical * 2)
        self.logo.setPixmap(theme.icon(self.window.spec.icon, 20).pixmap(20, 20))
        self.menu.setIcon(theme.icon("menu"))
        for button in (self.pin, self.minimize, self.maximize, self.close):
            button.refresh_theme()

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

    一个管理器仅承载一个主窗口，业务菜单与顶栏共享 QAction。
    传入 manager 时由调用方配置存储与主题；外壳不导入任何业务包。
    """

    # 仅在实际置顶标志变化后通知宿主；持久化策略由业务层自行决定。
    always_on_top_changed = Signal(bool)

    def __init__(self, spec: AppSpec, *, manager: DockManager | None = None, host_id="main"):
        flags = Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint
        if not spec.native_title_bar:
            flags |= Qt.WindowType.FramelessWindowHint
        super().__init__(None, flags)
        if host_id != "main":
            raise ValueError("仅支持 main 主窗口")
        self.spec = spec
        self._native_frame = None
        self.host_id = host_id
        self.manager = manager or DockManager(
            application_id=spec.application_id, storage_dir=spec.storage_dir, theme=spec.theme,
            shortcut_context=Qt.ShortcutContext.WindowShortcut,
        )
        self.setObjectName("mindmap_shell_" + host_id)
        self.setWindowTitle(spec.title)
        self.resize(1440, 900)
        self.setMinimumSize(860, 540)
        self.host = DockHost(self.manager, host_id, self)
        # 单窗口只注册一份业务动作，菜单和顶栏继续共享 QAction。
        self._action_specs = list(spec.actions)
        for action_spec in spec.actions:
            self._register_action(action_spec)
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
        self.manager.message.connect(self.show_status_message)
        self.manager.theme_changed.connect(self.refresh_theme)
        self.manager.tool_failed.connect(self.report_tool_error)
        self._frame_update_timer = QTimer(self)
        self._frame_update_timer.setSingleShot(True)
        self._frame_update_timer.timeout.connect(self._sync_native_frame)
        self._frame_outline = _WindowOutline(self)
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

    def set_title_bar_navigation(self, widget: QWidget | None) -> None:
        """设置紧邻主菜单右侧的业务导航控件，传入 None 清空该插槽。

        插槽接管控件的父子所有权，替换时隐藏并延迟销毁旧控件；重复设置同一
        实例不做任何操作。调用方负责控件的尺寸及业务行为，外壳仅提供位置，
        不读取文件、保存业务状态或改变右侧工具动作与窗口按钮的排列。
        """
        if widget is not None and not isinstance(widget, QWidget):
            raise TypeError("顶栏导航必须是 QWidget 或 None")
        bar = self.title_bar
        if widget is bar.navigation:
            return
        if widget is not None and (widget is bar or widget.isAncestorOf(bar)):
            raise ValueError("顶栏导航不能包含标题栏自身")
        previous = bar.navigation
        if previous is not None:
            bar.box.removeWidget(previous)
            previous.hide()
            previous.deleteLater()
        bar.navigation = widget
        if widget is not None:
            # 插入到主菜单与伸缩留白之间；不能使用末尾索引，否则会落入业务动作区。
            widget.setParent(bar)
            bar.box.insertWidget(bar.box.indexOf(bar.menu) + 1, widget)
            widget.show()

    @Slot()
    def refresh_theme(self):
        """刷新局部外观及窗口装饰，保持业务内容、停靠尺寸和全局主题开关的原语义。"""
        theme = self.manager.theme
        theme.apply(self, f"QWidget#mindmap_shell_title_bar {{background:{theme.css('main-window-titlebar-bg')};}}")
        if self.spec.global_theme:
            theme.apply_global(QApplication.instance())
        self.setWindowIcon(theme.icon(self.spec.icon, 32))
        self.title_bar.refresh_theme()
        self.status.setFixedHeight(24 if theme.compact else 28)
        self._refresh_window_frame()
        self.update()

    def _refresh_window_frame(self):
        """同步不占布局空间的细边框和系统阴影，原生标题栏模式保留系统装饰。"""
        self._refresh_frame_outline()
        if self.isVisible() and not self.spec.native_title_bar and QApplication.platformName() == "windows":
            # 合并连续的缩放与状态事件；退出当前原生消息后再更新 DWM，避免窗口
            # 正在计算客户区时重入系统装饰接口，也减少实时拖动中的重复合成调用。
            self._frame_update_timer.start(0)

    def _refresh_frame_outline(self):
        """仅在系统无法提供边框时显示 Qt 轮廓，避免圆角被直角线条覆盖或裁断。"""
        self._frame_outline.setGeometry(self.rect())
        self._frame_outline.setVisible(
            not self.spec.native_title_bar and not self.isMaximized() and not self.isFullScreen()
            and not (self._native_frame is not None and self._native_frame.native_border)
        )
        self._frame_outline.raise_()
        self._frame_outline.update()

    def _sync_native_frame(self):
        """在事件循环空闲时更新系统装饰，原生句柄变化后重新接入系统阴影。"""
        if not self.isVisible():
            return
        hwnd = int(self.winId())
        if self._native_frame is None or self._native_frame.hwnd != hwnd:
            self._native_frame = _create_windows_frame(hwnd)
        if self._native_frame is not None:
            color = self.manager.theme.color("main-window-outline")
            self._native_frame.refresh(
                maximized=self.isMaximized(), fullscreen=self.isFullScreen(), dark=self.manager.theme.dark,
                border_color=color.red() | (color.green() << 8) | (color.blue() << 16),
            )
        self._refresh_frame_outline()

    def showEvent(self, event):
        """窗口可见后安排同步装饰，保持 Qt 自身的显示与最大化流程完整。"""
        super().showEvent(event)
        self._sync_always_on_top_window_flag()
        self._refresh_window_frame()

    def resizeEvent(self, event):
        """覆盖层随窗口边界更新，中央布局、停靠尺寸和状态栏位置均由原布局负责。"""
        super().resizeEvent(event)
        if hasattr(self, "_frame_outline"):
            self._refresh_window_frame()

    def changeEvent(self, event):
        """系统贴靠或双击标题栏改变窗口状态时，同步装饰和最大化按钮图标。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "_frame_outline"):
            self.title_bar.refresh_theme()
            self._refresh_window_frame()
        elif event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            if hasattr(self, "title_bar"):
                self.title_bar.maximize.finish_native_press()

    @Slot(str)
    def show_status_message(self, text):
        self.status.showMessage(text, 6500)

    @Slot(str, str)
    def report_tool_error(self, tool_id, error):
        self.show_status_message(f"无法创建工具窗口 {tool_id}：{error}")

    def toggle_maximize(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()
        self.title_bar.refresh_theme()

    def is_always_on_top(self) -> bool:
        """读取主窗口的实际置顶标志，不依赖图钉按钮或另一份缓存状态。"""
        return bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    @Slot(bool)
    def set_always_on_top(self, enabled: bool) -> None:
        """在 GUI 线程设置主窗口置顶，并在状态改变后发送 always_on_top_changed。

        重复设置相同状态不改动窗口，也不发送信号。可见窗口直接同步 QWindow
        标志，保留几何、焦点及最大化／最小化状态；隐藏窗口将新标志留给后续
        显示流程，避免提前显示。此接口不读写配置，由宿主负责持久化。
        """
        enabled = bool(enabled)
        if enabled == self.is_always_on_top():
            return
        flag = Qt.WindowType.WindowStaysOnTopHint
        flags = self.windowFlags() | flag if enabled else self.windowFlags() & ~flag
        # overrideWindowFlags 只更新 QWidget 记录；可见时立即同步 QWindow，隐藏时
        # 由 showEvent 在显示时同步。这样两层使用同一置顶标志，又不会像
        # QWidget.setWindowFlag 一样隐藏或重建窗口，保留编辑焦点和系统贴靠位置。
        self.overrideWindowFlags(flags)
        self._sync_always_on_top_window_flag()
        self.title_bar.pin.refresh_theme()
        self._refresh_window_frame()
        self.always_on_top_changed.emit(enabled)

    def _sync_always_on_top_window_flag(self) -> None:
        """将 QWidget 的置顶标志提交给可见原生窗口；隐藏窗口等到 showEvent 再同步。"""
        handle = self.windowHandle()
        flag = Qt.WindowType.WindowStaysOnTopHint
        enabled = self.is_always_on_top()
        if not self.isVisible() or handle is None or bool(handle.flags() & flag) == enabled:
            return
        # Qt 即使复用同一 HWND，也可能移除自绘窗口补回的 WS_THICKFRAME；
        # 作废适配器，让后续装饰同步重新启用缩放样式、阴影和系统边框。
        self._native_frame = None
        with _preserve_windows_placement(int(handle.winId())):
            handle.setFlag(flag, enabled)

    def main_menu(self):
        """根据动作声明动态构建菜单，业务菜单与内建工作区菜单共用动作实例。"""
        menu = QMenu(self)
        groups = {}
        for spec in self._action_specs:
            if spec.menu not in groups:
                groups[spec.menu] = menu.addMenu(spec.menu)
            groups[spec.menu].addAction(self.manager.actions[spec.id])
        menu.addMenu(self.appearance_menu())
        return menu

    def appearance_menu(self):
        """构建平铺设置菜单；外观、界面显示、布局三组仅用分隔线区分。

        业务可复用此菜单并插入自己的设置入口；每次展开时从管理器读取勾选状态，
        动作仍通过原主题和选项接口提交，沿用会话保存及界面刷新流程。
        """
        menu = QMenu("外观与布局", self)
        for name, label in [("light", "浅色"), ("dark", "深色")]:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.manager.theme.name == name)
            action.triggered.connect(lambda checked=False, value=name: self.manager.set_theme(value))
        # 第一组选项紧接明暗主题；后续两组各加一条分隔线，不引入额外子菜单。
        option_groups = [
            [("compact", "紧凑模式")],
            [("show_bars", "显示停靠栏"), ("always_header_icons", "始终显示标题按钮")],
            [("widescreen", "宽屏布局"), ("left_side_by_side", "左侧并排"),
             ("right_side_by_side", "右侧并排")],
        ]
        for index, options in enumerate(option_groups):
            if index:
                menu.addSeparator()
            for key, label in options:
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(getattr(self.manager.settings, key))
                action.triggered.connect(lambda checked, value=key: self.manager.set_option(value, checked))
        return menu

    def _handle_native_maximize_mouse(self, msg) -> bool:
        """将最大化按钮的 Windows 鼠标消息同步到 Qt；返回是否已消费该条消息。

        悬浮移动和离开消息继续交给系统，使 HTMAXBUTTON 的贴靠菜单正常出现。
        原生按压期间由 Qt 抓取鼠标，后续消息可能变成客户区 WM_MOUSEMOVE／
        WM_LBUTTONUP，因此同时处理两条路径，并只允许一次按钮内松开触发动作。
        """
        button = self.title_bar.maximize
        if msg.message not in (0x00A0, 0x00A1, 0x00A2, 0x0200, 0x0202, 0x02A2, 0x0215, 0x001F):
            return False
        # QCursor 提供 Qt 的逻辑坐标，避免与 Win32 的物理像素混用，支持分数缩放和负坐标副屏。
        inside = (button.isVisible() and button.isEnabled()
                  and button.rect().contains(button.mapFromGlobal(QCursor.pos())))
        if msg.message in (0x00A0, 0x0200):  # WM_NCMOUSEMOVE / WM_MOUSEMOVE
            button.set_native_hovered(inside and (msg.message == 0x0200 or msg.wParam == 9))
            if button._native_pressed:
                button.setDown(inside)
                return True
        elif msg.message == 0x00A1 and msg.wParam == 9:  # WM_NCLBUTTONDOWN / HTMAXBUTTON
            if inside:
                button.begin_native_press()
            return True
        elif msg.message == 0x0202 and button._native_pressed:
            button.finish_native_press(activate=inside)
            return True
        elif msg.message == 0x00A2 and msg.wParam == 9:  # WM_NCLBUTTONUP
            # 没有从当前按钮开始的按压也消费松开，防止系统或旧消息误触发最大化。
            if button._native_pressed:
                button.finish_native_press(activate=inside)
            return True
        elif msg.message == 0x02A2:  # WM_NCMOUSELEAVE；每次离开后需要重新订阅。
            button._tracking_native_leave = False
            if not button._native_pressed:
                button.set_native_hovered(False)
        elif msg.message == 0x001F or (msg.message == 0x0215 and msg.lParam != msg.hWnd):
            # WM_CANCELMODE / WM_CAPTURECHANGED：允许 Qt 继续清理抓取，只撤销本按钮的输入状态。
            if button._native_pressed:
                button.finish_native_press()
        return False

    def nativeEvent(self, event_type, message):
        """处理自绘边框与原生窗口按钮消息，保留系统缩放及最大化贴靠菜单。"""
        if sys.platform == "win32" and not self.spec.native_title_bar:
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if hasattr(self, "title_bar") and self._handle_native_maximize_mouse(msg):
                return True, 0
            if msg.message == 0x0083 and msg.wParam and self._native_frame is not None:
                # WM_NCCALCSIZE：恢复系统阴影所需的样式后仍保留完整客户区；
                # 最大化时额外限制到工作区，防止标题栏和状态栏被屏幕边界裁掉。
                if not self.isFullScreen():
                    self._native_frame.constrain_maximized_client(msg.lParam)
                return True, 0
            if msg.message == 0x031E and self._native_frame is not None:
                # WM_DWMCOMPOSITIONCHANGED：系统重启合成器后重新提交阴影设置。
                self._refresh_window_frame()
            elif msg.message == 0x0084:
                # 集成标题栏仍将边框与最大化按钮的命中结果交给 Windows，保留系统缩放和贴靠。
                point = self.mapFromGlobal(QCursor.pos())
                button = self.title_bar.maximize
                if (button.isVisible() and button.isEnabled()
                        and button.geometry().contains(button.parentWidget().mapFrom(self, point))):
                    return True, 9
                if not self.isMaximized():
                    x, y, edge = point.x(), point.y(), 5
                    left, right, top, bottom = x < edge, x >= self.width() - edge, y < edge, y >= self.height() - edge
                    hit = (13 if left else 14 if right else 12) if top else (16 if left else 17 if right else 15) if bottom else 10 if left else 11 if right else 0
                    if hit:
                        return True, hit
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        if self.manager.closing:
            event.accept()
        elif self.manager.can_close_documents():
            self.manager.shutdown()
            event.accept()
        else:
            event.ignore()
