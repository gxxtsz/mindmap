"""桌面外壳接入边界：文档、停靠面板、应用动作与局部主题映射。"""

from pathlib import Path

from mindmap_docking import DockPosition, DocumentSpec, Theme, ToolWindowSpec
from mindmap_engine import MindMapEngine
from mindmap_qt import MindMapFileController, MindMapPanels, MindMapWidget, UiColors
from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow, run_desktop
from PySide6.QtCore import QLibraryInfo, QPoint, QSettings, QTimer, QTranslator
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu

DOCUMENT_ID = "mindmap.document"


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


class MindMapWorkbenchWindow(WorkbenchWindow):
    """一个文档会话的工作台；注册内容后由桌面外壳管理 QWidget 归属。

    storage_dir 可隔离布局与滚轮偏好，测试和截图必须指定临时目录。
    注册表原样注入画布，菜单、文件操作和导出共用同一实例。普通标签关闭
    保留会话；真正关闭主窗口或 CLI 截图退出时才幂等释放面板与画布。
    """

    def __init__(self, *, storage_dir=None, theme="light", restore=True,
                 layout_registry=None, format_registry=None):
        self._disposed = False
        self._document_registered = False
        super().__init__(AppSpec(
            "mindmap-workbench", "原生思维导图", subtitle="思维导图工作台",
            storage_dir=storage_dir, theme=Theme(theme or "light"),
        ))
        # 框架不再注册工作区导航快捷键；Escape 留给画布取消编辑和退出聚焦。
        self.engine = MindMapEngine()
        self.canvas = MindMapWidget(
            self.engine, layout_registry=layout_registry, format_registry=format_registry,
            toolbar=False, view_controls=True, embedded=True,
        )
        # 外壳对中央内容调用 QWidget.setFocus，组合画布把焦点交给真实视图。
        self.canvas.setFocusProxy(self.canvas.view)
        self.panels = MindMapPanels(self.canvas, self)
        self.files = MindMapFileController(self.canvas, self.panels, self)
        self.preferences = QSettings(
            str(self.manager.storage_dir / "preferences.ini"), QSettings.Format.IniFormat,
        )
        mode = self.preferences.value("view/wheel_mode", "scroll")
        self.canvas.set_wheel_mode(mode if mode in ("scroll", "zoom") else "scroll")
        self.canvas.wheel_mode_changed.connect(self._save_wheel_mode)
        # QSettings 的 INI 值可能是字符串；严格校验后恢复，损坏配置回退默认2层。
        depth = str(self.preferences.value("outline/display_depth", 2))
        depth = int(depth) if depth in {str(value) for value in range(10)} else 2
        self.panels.outline.set_display_depth(None if depth == 0 else depth)
        self.panels.outline.display_depth_changed.connect(self._save_outline_depth)
        # 粘贴偏好与滚轮一样只保存在宿主；Qt 组件不读取 QSettings，也不写入导图文件。
        edraw_mode = self.preferences.value("clipboard/edraw_mode", "append")
        keep_children = self.preferences.value("clipboard/edraw_keep_children", True)
        if isinstance(keep_children, str) and keep_children in ("true", "false"):
            keep_children = keep_children == "true"
        self.canvas.set_edraw_paste_options(
            edraw_mode if edraw_mode in ("append", "replace") else "append",
            keep_children if isinstance(keep_children, bool) else True,
        )
        self.canvas.edraw_paste_options_changed.connect(self._save_edraw_paste_options)
        self.canvas.error_occurred.connect(self.show_status_message)
        self.files.error_occurred.connect(self.show_status_message)
        self.files.message.connect(self.show_status_message)
        self.files.state_changed.connect(self._sync_document)
        self.canvas.document_changed.connect(self._sync_document)
        self.canvas.selection_changed.connect(self._sync_document)
        self.canvas.editable_changed.connect(self._sync_document)
        self.manager.register_document(DocumentSpec(
            DOCUMENT_ID, "未命名", lambda: self.canvas, can_close=self.files.confirm_replace,
        ))
        # 标签栏复用画布历史动作，不另建 QAction，菜单、快捷键、只读限制与
        # 原地编辑期间的启用状态因此保持同源；视图入口留在画布右下角。
        self.manager.set_document_actions(
            DOCUMENT_ID, [self.canvas.action("undo"), self.canvas.action("redo")],
        )
        self._document_registered = True
        for identifier, title, panel, icon, position, shortcut in (
            ("outline", "大纲", self.panels.outline, "outline", DockPosition.LEFT_TOP, "Alt+1"),
            ("properties", "节点属性", self.panels.properties, "properties", DockPosition.RIGHT_TOP, "Alt+2"),
        ):
            self.manager.register_tool_window(ToolWindowSpec(
                identifier, title, lambda content=panel: content, icon, position, shortcut,
                header_actions=(self.panels.outline.depth_action,) if identifier == "outline" else (),
            ))
        self.canvas.set_properties_handler(self._show_properties)
        self._register_file_actions()
        # 设置由左上角主菜单承载；旧面板只保留兼容访问对象，不再注册工具窗口。
        self.settings_menu = self.panels.settings.create_menu(self)
        self.settings_action = self.manager.register_action(
            "app.settings", "导图设置", self.show_settings_menu, shortcut="Alt+3", icon="settings",
        )
        self.read_only_action = QAction("只读模式", self, checkable=True)
        self.read_only_action.toggled.connect(lambda checked: self.canvas.set_editable(not checked))
        self.manager.activate("properties", focus=False)
        # 通过公开快照接口声明业务默认尺寸，避免依赖 DockHost 的内部集合。
        layout = self.manager.capture_layout()
        for sizes in layout["hosts"]["main"]["region_sizes"].values():
            sizes.update(left=260, right=360, bottom=260)
        self.manager.restore_layout(layout)
        self.manager.set_default_layout()
        self._properties_visible = True
        self.manager.layout_changed.connect(self._layout_changed)
        if restore:
            self.manager.load_session()
        self.refresh_theme()
        self._sync_document()
        QTimer.singleShot(0, self.canvas.fit_to_view)

    def _register_file_actions(self):
        """文件快捷键只注册到工作台一次，节点快捷键仍由画布和焦点决定。"""
        for identifier, title, callback, shortcut, icon, toolbar in (
            ("new", "新建", self.new_document, "Ctrl+N", "new-document", False),
            ("open", "打开 / 导入…", self.open_dialog, "Ctrl+O", "folder", False),
            ("save", "保存", self.files.save, "Ctrl+S", "save", False),
            ("save_as", "另存为…", lambda: self.files.save(save_as=True), "Ctrl+Shift+S", "save-as", False),
            ("export", "导出图形…", self.files.export_dialog, "", "export-image", False),
            ("quit", "退出", self.close, "Ctrl+Q", "exit", False),
        ):
            self.add_action(ActionSpec(
                "app." + identifier, title, callback, icon, shortcut, "文件", toolbar,
            ))

    def show_settings_menu(self):
        """在左上角主菜单按钮下展开导图设置，保留侧栏和原地编辑状态。

        Alt+3 继续直达共用设置菜单；定位只依赖保留的主菜单按钮，避免引用
        已移除的右侧入口，同时不增加新的可见按钮。
        """
        if not self._disposed:
            button = self.title_bar.menu
            self.settings_menu.popup(button.mapToGlobal(QPoint(0, button.height())))

    def appearance_menu(self):
        """扩展主菜单中的设置；导图设置与外壳外观沿用各自的状态和保存语义。"""
        menu = super().appearance_menu()
        menu.setTitle("设置")
        if hasattr(self, "settings_menu"):
            anchor = menu.actions()[0]
            menu.insertMenu(anchor, self.settings_menu)
            menu.insertSeparator(anchor)
        return menu

    def _save_wheel_mode(self, mode):
        self.preferences.setValue("view/wheel_mode", mode)
        self.preferences.sync()
        self._sync_document()

    def _save_outline_depth(self, depth):
        """只保存大纲层级预设；0表示所有层，手动展开状态不写入配置或导图。"""
        self.preferences.setValue("outline/display_depth", 0 if depth is None else depth)
        self.preferences.sync()

    def _save_edraw_paste_options(self, mode, keep_children):
        """成功粘贴后的选择立即写回本机偏好，下一次弹窗和重启工作台均恢复该值。"""
        self.preferences.setValue("clipboard/edraw_mode", mode)
        self.preferences.setValue("clipboard/edraw_keep_children", keep_children)
        self.preferences.sync()

    def _show_properties(self):
        self.canvas.finish_edit(commit=True)
        self.manager.activate("properties")

    def _sync_document(self, *_):
        if not self._document_registered or self._disposed:
            return
        title = self.files.path.name if self.files.path else "未命名"
        self.manager.set_document_title(DOCUMENT_ID, title)
        self.manager.set_document_modified(DOCUMENT_ID, self.files.is_dirty)
        self.setWindowTitle(f"{'* ' if self.files.is_dirty else ''}{title} — 原生思维导图")
        self.status_label.setText(
            f"  {len(self.engine.document.nodes)} 个节点 · "
            f"{len(self.canvas.selected_node_ids())} 个选中 · "
            f"{self.canvas.action('wheel_' + self.canvas.wheel_mode).text()}"
        )

    def show_document(self):
        """显示关闭后保留的中央页；始终复用原来的画布及撤销历史。"""
        self.manager.open_document(DOCUMENT_ID)

    def new_document(self):
        """用户允许替换后重用中央页创建空白导图，取消时返回 False。"""
        if self.files.new_document():
            self.show_document()
            return True
        return False

    def open_path(self, path, *, format_id=None):
        """校验成功且用户允许替换后显示同一中央页；失败保持当前会话。"""
        if self.files.open_path(path, format_id=format_id):
            self.show_document()
            return True
        return False

    def open_dialog(self):
        """调用共享文件选择和校验，成功后恢复关闭的中央标签。"""
        if self.files.open_dialog():
            self.show_document()

    def main_menu(self):
        """在标准外壳菜单中复用画布动作，每次打开重新枚举扩展能力。"""
        menu = super().main_menu()
        # 使用设置菜单作为稳定锚点，避免删除工作区后依赖菜单数字下标。
        anchor = next(action for action in menu.actions() if action.text() == "设置")
        edit = QMenu("编辑", menu)
        for key in ("undo", "redo", "add_child", "add_sibling", "insert_parent", "edit",
                    "properties", "delete", "copy", "cut", "paste", "image", "image_url"):
            edit.addAction(self.canvas.action(key))
        menu.insertMenu(anchor, edit)
        view = QMenu("视图", menu)
        view.addAction("显示导图", self.show_document)
        for key in ("zoom_in", "zoom_out", "fit", "center", "focus", "unfocus", "expand_all", "collapse_all"):
            view.addAction(self.canvas.action(key))
        self.read_only_action.setChecked(not self.canvas.editable)
        view.addAction(self.read_only_action)
        menu.insertMenu(anchor, view)
        exports = QMenu("导出文档", menu)
        for entry in self.canvas.format_registry.list():
            if entry.exporter is not None:
                action = exports.addAction(entry.info.name)
                action.setData(entry.info.id)
                action.triggered.connect(lambda checked=False, identifier=entry.info.id:
                                         self.canvas.call(lambda: self.files.export_document_dialog(identifier)))
        menu.insertMenu(anchor, exports)
        return menu

    def refresh_theme(self):
        """把外壳局部主题应用于控件，导图主题始终由引擎单独管理。"""
        super().refresh_theme()
        # 基类构造期间会调用本方法，此时业务控件尚未创建。
        if hasattr(self, "canvas"):
            colors = ui_colors_from_shell(self.manager.theme)
            self.canvas.set_ui_colors(colors)
            self.panels.set_ui_colors(colors)

    def _layout_changed(self):
        # 框架更新样式时也会暂时隐藏内容，只有布局快照中的实际可见性变化
        # 才表示用户隐藏面板。使用公开快照，避免把一次外观切换变成文档编辑。
        if self._disposed:
            return
        state = next(item for item in self.manager.capture_layout()["tools"] if item["id"] == "properties")
        visible = state["visible"] and state["available"]
        if self._properties_visible and not visible:
            self.panels.flush_pending_edits()
            self.panels.properties.discard_pending()
            self.panels.properties.sync()
        self._properties_visible = visible

    def dispose(self):
        """仅在主应用退出时调用；重复清理不会重新提交输入或访问已释放内容。"""
        if not self._disposed:
            self._disposed = True
            self.panels.dispose()
            self.canvas.dispose()

    def hideEvent(self, event):
        """主窗口隐藏或关闭时取消业务设置菜单，兼容基类构造期的显示事件。"""
        if hasattr(self, "panels"):
            self.panels.settings._close_popups()
        super().hideEvent(event)

    def closeEvent(self, event):
        # 基类先完成保存确认；取消关闭时保留控制器、计时器和下载能力。
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
        parser.add_argument("file", nargs="?", type=Path, help="启动时打开的导图或可导入文件")

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
            window.canvas.call(lambda: window.open_path(options.file))
        return window

    return run_desktop(builder, argv, description="原生思维导图工作台", configure_parser=configure)
