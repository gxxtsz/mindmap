"""桌面外壳接入边界：文档、停靠面板、应用动作与局部主题映射。"""

from pathlib import Path

from mindmap_docking import DockManager, DockPosition, Theme, ToolWindowSpec
from mindmap_engine import MindMapError, create_default_format_registry, create_default_layout_registry
from mindmap_qt_render import ensure_fonts
from mindmap_qt_ui import UiColors
from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow, run_desktop
from PySide6.QtCore import QLibraryInfo, QPoint, QSettings, QSignalBlocker, Qt, QTranslator
from PySide6.QtGui import QAction, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from .sessions import MindMapSessionManager


def ui_colors_from_shell(theme) -> UiColors:
    """把框架主题映射为通用色板；两种 Theme 不相互依赖或写回文档。"""
    def color(key):
        return theme.color(key).name()

    return UiColors(
        shell=color("main-window-bg"), surface=color("tool-window-bg"),
        field=color("editor-bg"), text=color("text-default"), muted=color("text-muted"),
        border=color("tool-window-border"), hover=color("toolbar-bg-hovered"),
        selected=color("toolbar-selected-bg-active"), accent=color("control-brand-border"),
        error=color("danger"),
    )


class _WelcomePage(QWidget):
    """不占用文档标识的空白页；每个空分组各有实例，主题与编辑器外轮廓保持一致。"""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setObjectName("mindmap_welcome")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.addStretch()
        title = QLabel("开始一份导图", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:20px; font-weight:600; background:transparent;")
        layout.addWidget(title)
        for text, callback in (("新建导图", window.new_document), ("打开文件", window.open_dialog)):
            button = QPushButton(text, self)
            button.setFixedWidth(170)
            button.clicked.connect(lambda checked=False, action=callback: window.call(action))
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        self.refresh_theme()

    def refresh_theme(self):
        theme = self.window.manager.theme
        self.setStyleSheet(f"QWidget#mindmap_welcome {{ background:{theme.css('editor-bg')}; "
                          f"color:{theme.css('text-default')}; }}")


class MindMapWorkbenchWindow(WorkbenchWindow):
    """单主窗口、多导图会话；运行中的文件与桌面布局分开管理。

    无活动会话时四个兼容访问属性返回 None；新建、打开始终通过会话管理器，
    文件动作在触发时解析当前会话，不能保存构造期的画布或控制器绑定方法。
    """

    def __init__(self, *, storage_dir=None, theme="light", restore=True,
                 layout_registry=None, format_registry=None):
        # 空白页不创建画布，也需要先补齐离屏平台字体；正常桌面字体不会被改写。
        ensure_fonts()
        self._disposed = False
        self._updating_preferences = False
        self.layout_registry = layout_registry if layout_registry is not None else create_default_layout_registry()
        self.format_registry = format_registry if format_registry is not None else create_default_format_registry()
        manager = DockManager(application_id="mindmap-workbench", storage_dir=storage_dir,
                              theme=Theme(theme or "light"), preserve_open_documents=True,
                              shortcut_context=Qt.ShortcutContext.WindowShortcut)
        super().__init__(AppSpec("mindmap-workbench", "原生思维导图", subtitle="思维导图工作台",
                                storage_dir=storage_dir, theme=manager.theme), manager=manager)
        self.preferences = QSettings(str(manager.storage_dir / "preferences.ini"), QSettings.Format.IniFormat)
        self._outline_stack = QStackedWidget(manager.parking)
        self._properties_stack = QStackedWidget(manager.parking)
        for stack in (self._outline_stack, self._properties_stack):
            message = QLabel("没有打开的导图")
            message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stack.addWidget(message)
        self._depth_action = QAction("显示层级", self)
        self._empty_settings_menu = QMenu("导图设置", self)
        self._empty_settings_menu.setEnabled(False)
        self.sessions = MindMapSessionManager(self)
        for identifier, title, stack, icon, position, shortcut in (
            ("outline", "大纲", self._outline_stack, "outline", DockPosition.LEFT_TOP, "Alt+1"),
            ("properties", "节点属性", self._properties_stack, "properties", DockPosition.RIGHT_TOP, "Alt+2"),
        ):
            manager.register_tool_window(ToolWindowSpec(
                identifier, title, lambda content=stack: content, icon, position, shortcut,
                header_actions=(self._depth_action,) if identifier == "outline" else (),
            ))
        self.host.editor.set_empty_widget_factory(lambda: _WelcomePage(self))
        self._register_file_actions()
        self.settings_action = manager.register_action("app.settings", "导图设置", self.show_settings_menu,
                                                       shortcut="Alt+3", icon="settings")
        self.read_only_action = QAction("只读模式", self, checkable=True)
        self.read_only_action.toggled.connect(self._set_read_only)
        manager.activate("properties", focus=False)
        layout = manager.capture_layout()
        for sizes in layout["hosts"]["main"]["region_sizes"].values():
            sizes.update(left=260, right=360, bottom=260)
        manager.restore_layout(layout)
        manager.set_default_layout()
        self._properties_visible = True
        manager.layout_changed.connect(self._layout_changed)
        if restore:
            manager.load_session()
        self.refresh_theme()
        self._bind_session(None)

    @property
    def active_session(self):
        """当前逻辑会话；侧栏获取焦点不会改变，未打开导图时返回 None。"""
        return self.sessions.active if hasattr(self, "sessions") else None

    @property
    def canvas(self):
        """只读访问活动画布；空白页期间返回 None，不能替换会话拥有的实例。"""
        return self.active_session.canvas if self.active_session else None

    @property
    def engine(self):
        """只读访问活动引擎；所有历史和内容状态由对应会话独立持有。"""
        return self.active_session.engine if self.active_session else None

    @property
    def files(self):
        """只读访问活动文件控制器，未打开导图时不创建临时控制器。"""
        return self.active_session.files if self.active_session else None

    @property
    def panels(self):
        """只读访问活动面板组合；外层停靠容器在各会话间共用。"""
        return self.active_session.panels if self.active_session else None

    @property
    def settings_menu(self):
        return self.active_session.settings_menu if self.active_session else self._empty_settings_menu

    def call(self, callback):
        """无会话时也能报告新建、打开错误；UI 异常边界不依赖某个画布的生命周期。"""
        try:
            return callback()
        except (MindMapError, OSError, ValueError) as error:
            self.show_status_message(str(error))
            return None

    def _register_file_actions(self):
        """全窗口文件快捷键仅注册一次；每次触发时再取得活动会话。"""
        for identifier, title, callback, shortcut, icon in (
            ("new", "新建", self.new_document, "Ctrl+N", "new-document"),
            ("open", "打开…", self.open_dialog, "Ctrl+O", "folder"),
            ("save", "保存", self.save, "Ctrl+S", "save"),
            ("save_all", "保存全部", self.save_all, "Ctrl+Alt+S", "save-all"),
            ("save_as", "另存为…", lambda: self.save(save_as=True), "Ctrl+Shift+S", "save-as"),
            ("quit", "退出", self.close, "Ctrl+Q", "exit"),
        ):
            self.add_action(ActionSpec("app." + identifier, title, callback, icon, shortcut, "文件", False))

    def _configure_session(self, session):
        """从本机偏好初始化新会话；绑定回调始终携带所属会话，不借用活动文档。"""
        canvas, panels = session.canvas, session.panels
        mode = self.preferences.value("view/wheel_mode", "scroll")
        canvas.set_wheel_mode(mode if mode in ("scroll", "zoom") else "scroll")
        depth = str(self.preferences.value("outline/display_depth", 2))
        depth = int(depth) if depth in {str(n) for n in range(10)} else 2
        panels.outline.set_display_depth(None if depth == 0 else depth)
        mode = self.preferences.value("clipboard/edraw_mode", "append")
        keep = self.preferences.value("clipboard/edraw_keep_children", True)
        if isinstance(keep, str) and keep in ("true", "false"):
            keep = keep == "true"
        canvas.set_edraw_paste_options(mode if mode in ("append", "replace") else "append",
                                     keep if isinstance(keep, bool) else True)
        canvas.wheel_mode_changed.connect(self._save_wheel_mode)
        canvas.edraw_paste_options_changed.connect(self._save_edraw_paste_options)
        panels.outline.display_depth_changed.connect(self._save_outline_depth)
        panels.outline.depth_action.changed.connect(self._sync_depth_action)
        canvas.set_properties_handler(lambda: self._show_properties(session))
        colors = ui_colors_from_shell(self.manager.theme)
        canvas.set_ui_colors(colors)
        panels.set_ui_colors(colors)

    def _save_wheel_mode(self, mode):
        if self._updating_preferences:
            return
        self._updating_preferences = True
        try:
            self.preferences.setValue("view/wheel_mode", mode)
            self.preferences.sync()
            for session in self.sessions.values():
                session.canvas.set_wheel_mode(mode)
        finally:
            self._updating_preferences = False
        self._sync_document()

    def _save_edraw_paste_options(self, mode, keep_children):
        if self._updating_preferences:
            return
        self._updating_preferences = True
        try:
            self.preferences.setValue("clipboard/edraw_mode", mode)
            self.preferences.setValue("clipboard/edraw_keep_children", keep_children)
            self.preferences.sync()
            for session in self.sessions.values():
                session.canvas.set_edraw_paste_options(mode, keep_children)
        finally:
            self._updating_preferences = False

    def _save_outline_depth(self, depth):
        """当前会话独立保留层级，最近值只作为后续新会话默认值。"""
        self.preferences.setValue("outline/display_depth", 0 if depth is None else depth)
        self.preferences.sync()
        self._sync_depth_action()

    def _sync_depth_action(self):
        """只在有活动导图时显示层级入口，空白页不提供无效的文字占位。"""
        source = self.panels.outline.depth_action if self.panels else None
        self._depth_action.setEnabled(source is not None)
        self._depth_action.setVisible(source is not None)
        self._depth_action.setMenu(source.menu() if source else None)
        self._depth_action.setIcon(source.icon() if source else QIcon())
        self._depth_action.setText(source.text() if source else "显示层级")
        self._depth_action.setToolTip(source.toolTip() if source else "没有打开的导图")

    def _bind_session(self, session):
        """仅切换堆叠页和动作，不重建业务面板，不改变其他会话的选择和滚动位置。"""
        self._outline_stack.setCurrentWidget(session.panels.outline) if session else self._outline_stack.setCurrentIndex(0)
        self._properties_stack.setCurrentWidget(session.panels.properties) if session else self._properties_stack.setCurrentIndex(0)
        self._sync_depth_action()
        self._sync_document()
        # 欢迎页临时暂停依赖导图的侧栏，不把“没有文档”写成用户主动隐藏面板。
        # 关闭活动标签到下一标签的短暂空绑定不暂停，只有关闭全部导图才回欢迎页。
        self.manager.set_tools_suspended(("outline", "properties"), not bool(len(self.sessions)))

    def _set_read_only(self, checked):
        if self.canvas:
            self.canvas.set_editable(not checked)

    def _show_properties(self, session=None):
        session = session or self.active_session
        if session:
            self.manager.activate_document(session.id)
            session.canvas.finish_edit(commit=True)
            self.manager.activate("properties")

    def show_settings_menu(self):
        if self.active_session and not self._disposed:
            button = self.title_bar.menu
            self.settings_menu.popup(button.mapToGlobal(QPoint(0, button.height())))

    def appearance_menu(self):
        menu = super().appearance_menu()
        menu.setTitle("设置")
        if hasattr(self, "_empty_settings_menu"):
            anchor = menu.actions()[0]
            menu.insertMenu(anchor, self.settings_menu)
            menu.insertSeparator(anchor)
        return menu

    def _sync_document(self, *_):
        """文件状态属于会话；窗口标题、状态栏和全局动作仅反映当前活动会话。"""
        if self._disposed or not hasattr(self, "settings_action"):
            return
        session = self.active_session
        for identifier in ("save", "save_as"):
            self.action("app." + identifier).setEnabled(session is not None)
        self.action("app.save_all").setEnabled(bool(len(self.sessions)))
        self.settings_action.setEnabled(session is not None)
        self.read_only_action.setEnabled(session is not None)
        with QSignalBlocker(self.read_only_action):
            self.read_only_action.setChecked(session is not None and not session.canvas.editable)
        if session:
            status = "保存失败" if session.last_error else "待保存" if session.files.is_dirty else "已保存"
            title = session.files.path.name
            self.setWindowTitle(f"{'* ' if session.files.is_dirty else ''}{title} — 原生思维导图")
            self.status_label.setText(f"  {session.engine.node_count} 个节点 · "
                                      f"{len(session.canvas.selected_node_ids())} 个选中 · {status}")
        else:
            self.setWindowTitle("原生思维导图")
            self.status_label.setText("  新建导图或打开文件以开始")

    def new_document(self, path=None):
        """选择路径并成功首次保存后新增会话，现有会话保持不变。"""
        return self.sessions.new_document(path)

    def open_path(self, path):
        """仅打开 .mindmap 导图；重复路径激活原标签，失败不替换任何现有文档。"""
        return self.sessions.open_path(path)

    def open_dialog(self):
        return self.sessions.open_dialog()

    def save(self, *, save_as=False):
        """保存动作按触发时的活动会话处理，成功后清除该会话自动保存错误。"""
        session = self.active_session
        if not session:
            return False
        if session.panels.properties.composing or (session.canvas.editor and session.canvas.editor.composing):
            session._failed("请先完成输入法候选，再保存导图")
            return False
        # 另存为取消时仍沿用原路径的防抖保存；成功或写入失败由文件状态通知
        # 停止计时器。不能在打开对话框前停掉已经排队的自动保存。
        result = session.files.save(save_as=True) if save_as else session.flush()
        self.sessions.sync(session)
        return result

    def save_all(self):
        return self.sessions.save_all()

    def main_menu(self):
        """稳定的四个主菜单；空会话使用禁用占位，文件操作始终动态路由。"""
        menu = super().main_menu()
        files = next(action.menu() for action in menu.actions() if action.text() == "文件")
        exports = QMenu("导出图形", files)
        exports.setIcon(self.manager.theme.icon("export-image"))
        exports.menuAction().setProperty("mindmap_icon", "export-image")
        exports.setEnabled(self.active_session is not None)
        for format_id, label in (("png", "PNG 图片"), ("svg", "SVG 矢量图"), ("pdf", "PDF 文档")):
            action = exports.addAction(label + "…")
            action.setData(format_id)
            action.triggered.connect(lambda checked=False, selected=format_id:
                                     self.call(lambda: self.files.export_dialog(selected)) if self.files else None)
        files.insertMenu(self.action("app.quit"), exports)
        anchor = next(action for action in menu.actions() if action.text() == "设置")
        for title, keys in (
            ("编辑", ("undo", "redo", "add_child", "add_sibling", "insert_parent", "edit", "properties",
                    "delete", "copy", "cut", "paste", "image", "image_url")),
            ("视图", ("zoom_in", "zoom_out", "fit", "center", "focus", "unfocus", "expand_all", "collapse_all")),
        ):
            section = QMenu(title, menu)
            for key in keys:
                if self.canvas:
                    section.addAction(self.canvas.action(key))
                else:
                    section.addAction("没有打开的导图").setEnabled(False)
                    break
            if title == "视图":
                section.addAction(self.read_only_action)
            menu.insertMenu(anchor, section)
        return menu

    def refresh_theme(self):
        """对所有会话同步界面色板，导图内容和自动保存计时不因外壳主题变化而修改。"""
        super().refresh_theme()
        if hasattr(self, "sessions"):
            colors = ui_colors_from_shell(self.manager.theme)
            for session in self.sessions.values():
                session.canvas.set_ui_colors(colors)
                session.panels.set_ui_colors(colors)
            for stack in (self._outline_stack, self._properties_stack):
                palette = stack.palette()
                palette.setColor(QPalette.ColorRole.Window, self.manager.theme.color("tool-window-bg"))
                stack.setPalette(palette)
                stack.setAutoFillBackground(True)
            for group in self.host.editor.groups:
                if group._empty_widget:
                    group._empty_widget.refresh_theme()
            self._sync_depth_action()

    def _layout_changed(self):
        if self._disposed:
            return
        visible = self.manager.states["properties"].visible
        if getattr(self, "_properties_visible", visible) and not visible and self.panels:
            self.panels.flush_pending_edits()
        self._properties_visible = visible
        # 恢复布局会重建标签容器，文件路径和保存错误属于业务元信息，须重新附加。
        for session in self.sessions.values():
            self.sessions.sync(session)

    def dispose(self):
        """主窗口通过全部保存检查后停止所有会话；重复调用安全。"""
        if not self._disposed:
            self._disposed = True
            # 先停用停靠层的全局焦点监听，再删除业务控件；显式释放入口也不能
            # 让延迟焦点回调继续访问已经销毁的画布。正常关闭时 shutdown 已执行。
            self.manager.shutdown()
            self.sessions.dispose()

    def hideEvent(self, event):
        if hasattr(self, "sessions"):
            for session in self.sessions.values():
                session.panels.settings.close_popups()
        super().hideEvent(event)

    def closeEvent(self, event):
        # 先按视觉标签顺序保存全部文件，任何失败均不让基类关闭或删除停靠页面。
        if not self.manager.closing and not self.sessions.save_all():
            event.ignore()
            return
        super().closeEvent(event)
        if event.isAccepted():
            self.dispose()


def build_window(*, storage_dir=None, theme=None, restore=True,
                 layout_registry=None, format_registry=None):
    """构建可测试的独立窗口；显式主题优先于已保存的工作台布局主题。"""
    window = MindMapWorkbenchWindow(
        storage_dir=storage_dir, theme=theme, restore=restore,
        layout_registry=layout_registry, format_registry=format_registry,
    )
    if theme is not None:
        window.manager.set_theme(theme)
    return window


def run(argv=None):
    """复用桌面启动器，追加文件参数和中文 Qt 翻译，截图路径由框架处理。"""
    def configure(parser):
        parser.add_argument("file", nargs="?", type=Path, help="启动时打开的 .mindmap 导图")

    def builder(options):
        app = QApplication.instance()
        translator = QTranslator(app)
        translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
        app.installTranslator(translator)
        app.setApplicationName("原生思维导图工作台")
        window = build_window(storage_dir=options.state_dir, theme=options.theme,
                              restore=not options.no_restore)
        window._translator = translator
        if options.file:
            window.call(lambda: window.open_path(options.file))
        return window

    return run_desktop(builder, argv, description="原生思维导图工作台", configure_parser=configure)
