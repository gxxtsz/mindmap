"""导图主体的界面装配器；独立于主窗口类型，通过公开桌面服务管理文档及节点页面。"""
from pathlib import Path

from mindmap_core.formats import create_default_format_registry
from mindmap_core.layouts import create_default_layout_registry
from mindmap_desktop_api import OperationResult
from mindmap_desktop_api.qt import CommandSpec, MenuContribution, MenuLocation, PanelSpec, StatusItemSpec
from mindmap_qt_editor.node_pages import NodePageRegistry
from mindmap_qt_editor.theme import UiColors
from mindmap_qt_render import ensure_fonts
from PySide6.QtCore import QObject, QSignalBlocker, QTimer
from PySide6.QtGui import QAction, QIcon, QPalette
from PySide6.QtWidgets import QLabel, QMenu, QStackedWidget, QWidget

from .operation_log import describe_display
from .page_history import PageHistoryState
from .sessions import MindMapSessions
from .status_controls import _StatusViewControls


def ui_colors_from_host(ui):
    """把平台颜色令牌转换为导图组件自己的界面色板。"""
    keys = ("main-window-bg", "tool-window-bg", "editor-bg", "text-default", "text-muted",
            "tool-window-border", "toolbar-bg-hovered", "toolbar-selected-bg-active", "control-brand-border", "danger")
    return UiColors(*(ui.theme_color(key).name() for key in keys))


class MindMapController(QObject):
    """导图专属面板、偏好和会话装配，所有私有堆叠控件均由应用组件持有。"""
    def __init__(self, context):
        super().__init__(context.ui.parent)
        self.context = context
        self._disposed = False
        self._restoring_recent_pages = True
        self._updating_preferences = False
        self._operation_error_token = object()
        ensure_fonts()
        self.layout_registry = context.options.get("layout_registry") or create_default_layout_registry()
        self.format_registry = context.options.get("format_registry") or create_default_format_registry()
        self.page_registry = context.options.get("page_registry") or NodePageRegistry.discover()
        for error in self.page_registry.errors:
            context.log(error, level="error")
        self.preferences = context.settings
        self.page_history = PageHistoryState(context.state)
        self.page_history.load()
        self._recent_pages_timer = QTimer(self)
        self._recent_pages_timer.setSingleShot(True)
        self._recent_pages_timer.setInterval(500)
        self._recent_pages_timer.timeout.connect(self.flush_recent_pages)
        # 面板首次挂载时由宿主接管堆栈。一直未挂载的堆栈随隐藏容器释放。
        panel_staging = QWidget(context.ui.parent)
        panel_staging.hide()
        context.own(panel_staging.deleteLater)
        for name in ("outline", "favorites", "inspiration", "recent_pages", "properties", "settings"):
            stack = QStackedWidget(panel_staging)
            stack.addWidget(QLabel("没有打开的导图"))
            setattr(self, "_" + name + "_stack", stack)
        self._depth_action = QAction("显示层级", self)
        self._new_inspiration_action = QAction("新建想法", self)
        self._new_inspiration_action.triggered.connect(self._new_inspiration)
        self.sessions = MindMapSessions(self)
        self._status_controls = _StatusViewControls()
        context.own(self.dispose)
        context.ui.register_status(StatusItemSpec("mindmap.view", self._status_controls,
                                                 lambda: self.active_session is not None))
        headers = {"outline": (self._depth_action,), "inspiration": (self._new_inspiration_action,)}
        for identifier, title, stack, icon, position, shortcut in (
            ("outline", "大纲", self._outline_stack, "outline", "left_top", "Alt+1"),
            ("favorites", "收藏", self._favorites_stack, "favorites", "left_top", "Alt+4"),
            ("inspiration", "灵感", self._inspiration_stack, "inspiration", "left_top", "Alt+6"),
            ("recent_pages", "最近访问", self._recent_pages_stack, "recent-pages", "left_top", "Alt+7"),
            ("properties", "节点属性", self._properties_stack, "properties", "right_top", "Alt+2"),
            ("map_settings", "导图设置", self._settings_stack, "settings", "right_top", ""),
        ):
            context.ui.register_panel(PanelSpec(identifier, title, lambda content=stack: content,
                                                icon, position, shortcut, headers.get(identifier, ()),
                                                identifier in ("recent_pages", "properties")))
        self.settings_action = context.commands.register(CommandSpec(
            "mindmap.settings", "导图设置", self.show_settings_panel, "Alt+3", "settings",
            enabled=lambda: self.active_session is not None and not self.active_session.pages.active))
        self.read_only_action = QAction("只读模式", self, checkable=True)
        self.read_only_action.toggled.connect(self._set_read_only)
        context.ui.subscribe_theme(self.refresh_theme)
        context.ui.subscribe_layout(self._layout_changed)
        context.ui.subscribe_active(self._finish_restoring)
        context.ui.contribute_menu(MenuContribution(MenuLocation.FILE, self.populate_conflict_menu))
        context.ui.contribute_menu(MenuContribution(MenuLocation.FILE_EXPORT, self.populate_export_menu))
        context.ui.contribute_menu(MenuContribution(MenuLocation.EDIT, self.populate_edit_menu))
        context.ui.contribute_menu(MenuContribution(MenuLocation.VIEW, self.populate_view_menu))
        self._properties_visible = True
        self._settings_visible = False
        self.refresh_theme()
        self._bind_session(None)

    def show_status_message(self, message):
        self.context.ui.show_message(message)

    def record_operation(self, message, *, session=None, level="info"):
        if self._disposed:
            return
        if isinstance(session, (str, Path)):
            path = Path(session)
        elif session is not None:
            if session.disposed:
                return
            path = session.files.path
        else:
            path = None
        title, detail = describe_display(message)
        self.context.log(message, source=path.name if path else "应用", source_path=str(path) if path else "",
                         level=level, title=title, detail=detail)

    def _remember_path(self, path):
        self.flush_recent_pages()
        self.context.ui.remember_path(path)

    def _finish_restoring(self, view):
        """恢复结束后只记录最终活动导图，不将逐个后台加载计入最近访问。"""
        if self._restoring_recent_pages and not self.context.ui.restoring:
            self._restoring_recent_pages = False
            if self.active_session is not None:
                self.active_session._record_page_visit()

    def _sync_document(self, *_):
        if self._disposed or not hasattr(self, "settings_action"):
            return
        session = self.active_session
        self.settings_action.setEnabled(session is not None and not session.pages.active)
        self.read_only_action.setEnabled(session is not None)
        with QSignalBlocker(self.read_only_action):
            self.read_only_action.setChecked(session is not None and not session.canvas.editable)
        if session is None:
            self._status_controls.set_save_status("")
        else:
            self._status_controls.set_save_status(session.save_status, session.document.last_error)
        self.context.ui.refresh()

    def new_document(self, path=None):
        try:
            return OperationResult(view_id=self.sessions.active_id) if self.sessions.new_document(path) else OperationResult.cancelled()
        except Exception as error:
            return OperationResult.failed(error, reported=True)

    def open_document(self, path, view_id=None, state=None):
        try:
            opened = (self.sessions.open_path(path) if view_id is None else
                      self.sessions._open_path(path, session_id=view_id, remember=False, view_state=state))
            return OperationResult(view_id=self.sessions.active_id) if opened else OperationResult.cancelled()
        except Exception as error:
            return OperationResult.failed(error, reported=view_id is None)

    def populate_conflict_menu(self, menu: QMenu) -> None:
        """当前导图发生保存冲突时，提供保留内存内容或重新读取磁盘版本的入口。"""
        session = self.active_session
        if session is None or not session.document.file_state.conflict:
            return
        document = session.document
        path = document.path
        actions = menu.addMenu("处理保存冲突")
        if path is not None and path.is_file():
            actions.addAction("重新加载磁盘版本…", lambda: self.call(document.reload_after_conflict))
        elif path is not None and not path.exists():
            actions.addAction("恢复原文件", lambda: self.call(document.restore_missing))
        actions.addAction(self.context.ui.action("app.save_as"))

    def populate_export_menu(self, menu: QMenu) -> None:
        """在宿主的导出分区列出当前页支持的格式。空白页期间省略整组动作。"""
        canvas = self.canvas
        if canvas is None or self.active_session.pages.active:
            return
        has_nodes = bool(canvas.document_view.nodes)
        for format_id, title in (
            ("png", "PNG 图片"), ("svg", "SVG 矢量图"), ("pdf", "PDF 文档"), ("txt", "TXT 文本"),
        ):
            action = menu.addAction(title + "…")
            action.setData(format_id)
            action.setEnabled(has_nodes)
            if format_id == "txt":
                action.setIcon(self.context.ui.theme_icon("export-text"))
            action.triggered.connect(lambda checked=False, selected=format_id: self._export_current_page(selected))

    def _export_current_page(self, format_id: str) -> None:
        """触发时读取活动文件控制器，使菜单打开后切换标签仍导出正确的页面。"""
        files = self.files
        if files is not None:
            self.call(lambda: files.export_dialog(format_id))

    def populate_edit_menu(self, menu: QMenu) -> None:
        """高频节点动作直接可见，结构、页面、灵感和图片动作分别归组。"""
        canvas = self.canvas
        if canvas is None:
            return
        if self.active_session.pages.active:
            # 页面内容操作由页内工具承担，避免节点快捷菜单修改当前不可见的导图。
            menu.addAction("返回导图", self.active_session.pages.close_page)
            return
        menu.addActions([canvas.action(key) for key in ("add_child", "add_sibling", "edit", "properties")])
        menu.addSeparator()
        for title, keys in (
            ("节点结构", ("insert_parent", "delete")),
            ("页面", ("convert_descendants_to_submap", "convert_submap_to_descendants",
                     "set_page_reference", "open_page_reference", "clear_page_reference")),
            ("灵感", ("add_inspiration", "move_to_inspiration", "attach_inspirations", "group_inspirations")),
            ("图片", ("image", "image_url")),
        ):
            submenu = menu.addMenu(title)
            submenu.addActions([canvas.action(key) for key in keys])
        self.active_session.pages.populate_menu(menu.addMenu("扩展详情"))

    def populate_view_menu(self, menu: QMenu) -> None:
        """贡献活动导图的视图动作，面板入口由宿主根据停靠注册表生成。"""
        canvas = self.canvas
        if canvas is None:
            return
        if not self.active_session.pages.active:
            menu.addActions([canvas.action(key) for key in ("zoom_in", "zoom_out", "expand_all", "collapse_all")])
        menu.addAction(self.read_only_action)

    def refresh_theme(self, *_):
        colors = self.ui_colors
        panel_background = self.context.ui.theme_color("tool-window-bg")
        for session in self.sessions.values():
            session.canvas.set_ui_colors(colors)
            session.pages.refresh_theme()
            session.panels.set_ui_colors(colors)
            for panel in (session.favorites, session.inspiration, session.recent_pages):
                panel.set_ui_colors(colors)
        for stack in (self._outline_stack, self._favorites_stack, self._inspiration_stack,
                      self._recent_pages_stack, self._properties_stack, self._settings_stack):
            palette = stack.palette()
            palette.setColor(QPalette.ColorRole.Window, panel_background)
            stack.setPalette(palette)
            stack.setAutoFillBackground(True)
        self._status_controls.set_ui_colors(
            colors, compact=self.context.ui.compact, fit_icon=self.context.ui.theme_icon("fit-view"),
        )
        self._sync_depth_action()

    def dispose(self):
        if self._disposed:
            return
        self.flush_recent_pages()
        self._disposed = True
        self._recent_pages_timer.stop()
        self.sessions.dispose()
        # 大图打开的嵌套事件循环可能尚未返回，候选会话的 QObject 父链须保留到
        # 主窗口销毁，避免回调清理候选时访问已销毁的定时器。所有订阅已由上下文释放。
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


    def _record_exception(self, error, *, operation="操作", session=None):
        """在业务失败边界记录异常，并标记本次传播，防止 UI 再重复记录同一失败。

        标记不持有窗口或文档；业务入口每次失败都重新记录，即使调用方复用异常
        实例也不吞掉后续失败。只有外层 call 消费标记，不根据错误文字做去重。
        """
        self.record_operation(f"{operation}失败：{error}", session=session, level="error")
        error._mindmap_operation_log_token = self._operation_error_token
        error._mindmap_reported = True


    def call(self, callback):
        """UI 异常边界捕获操作开始时的会话；内层已记录的同一异常只显示状态提示。"""
        session = self.active_session
        try:
            return callback()
        except Exception as error:
            if getattr(error, "_mindmap_operation_log_token", None) is not self._operation_error_token:
                self._record_exception(error, session=session)
            error._mindmap_operation_log_token = None
            self.show_status_message(str(error))
            return None


    def _configure_session(self, session):
        """从本机偏好初始化新会话；绑定回调始终携带所属会话，不借用活动文档。"""
        canvas, panels = session.canvas, session.panels
        mode = self.preferences.value("view/wheel_mode", "scroll")
        canvas.set_wheel_mode(mode if mode in ("scroll", "zoom") else "scroll")
        depth = str(self.preferences.value("outline/display_depth", 2))
        depth = int(depth) if depth in {str(n) for n in range(10)} else 2
        panels.outline.set_display_depth(None if depth == 0 else depth)
        # 亿图、缩进文本及 Markdown 共用原有配置键；保留名称避免迁移及丢失历史偏好。
        mode = self.preferences.value("clipboard/edraw_mode", "append")
        keep = self.preferences.value("clipboard/edraw_keep_children", True)
        if isinstance(keep, str) and keep in ("true", "false"):
            keep = keep == "true"
        # 新配置独立校验；旧版 INI 没有此项时默认替换根节点，兼容原有粘贴行为。
        replace_root = self.preferences.value("clipboard/edraw_replace_root", True)
        if isinstance(replace_root, str) and replace_root in ("true", "false"):
            replace_root = replace_root == "true"
        canvas.set_edraw_paste_options(mode if mode in ("append", "replace") else "append",
                                     keep if isinstance(keep, bool) else True,
                                     replace_root if isinstance(replace_root, bool) else True)
        canvas.wheel_mode_changed.connect(self._save_wheel_mode)
        canvas.edraw_paste_options_changed.connect(self._save_edraw_paste_options)
        panels.outline.display_depth_changed.connect(self._save_outline_depth)
        panels.outline.depth_action.changed.connect(self._sync_depth_action)
        session.inspiration.new_action.changed.connect(self._sync_new_inspiration_action)
        canvas.set_properties_handler(lambda: self._show_properties(session))
        colors = self.ui_colors
        canvas.set_ui_colors(colors)
        panels.set_ui_colors(colors)
        session.favorites.set_ui_colors(colors)
        session.inspiration.set_ui_colors(colors)
        session.recent_pages.set_ui_colors(colors)


    @property
    def ui_colors(self) -> UiColors:
        """返回当前外壳色板，供会话在创建控件时直接采用最终配色。

        查询不缓存主题、不修改偏好或导图，后续主题切换仍由 refresh_theme 统一同步。
        """
        return ui_colors_from_host(self.context.ui)


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


    def _save_edraw_paste_options(self, mode, keep_children, replace_root):
        """保存三种外部大纲成功粘贴后的共享偏好，同步会话；重入保护避免信号循环。

        三项参数来自画布既有信号，仅单根成功确认后更新，不写入导图或撤销历史。
        保留 clipboard/edraw_* 键，使亿图、缩进文本及 Markdown 的下次默认值一致，
        并兼容旧版本保存的偏好；preferences.sync 保证重启后可以恢复。
        """
        if self._updating_preferences:
            return
        self._updating_preferences = True
        try:
            self.preferences.setValue("clipboard/edraw_mode", mode)
            self.preferences.setValue("clipboard/edraw_keep_children", keep_children)
            self.preferences.setValue("clipboard/edraw_replace_root", replace_root)
            self.preferences.sync()
            for session in self.sessions.values():
                session.canvas.set_edraw_paste_options(mode, keep_children, replace_root)
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


    def _new_inspiration(self):
        """标题栏每次点击时解析活动会话，复用所属侧栏的新建动作及权限检查。

        不保存旧标签的回调，不直接写引擎；空白页或窗口释放后忽略迟到点击。
        正常新建仍通过 new_requested 交给原有画布异常边界与输入检查处理。
        """
        session = self.active_session
        if not self._disposed and session and not session.disposed:
            session.inspiration.new_action.trigger()


    def _sync_new_inspiration_action(self):
        """将活动侧栏的新建图标与权限同步到共享标题栏，不改变会话或文档。

        后台会话的动作变化也只读取当前活动会话；无文档时隐藏并禁用入口，
        只读或输入法候选期间沿用侧栏的禁用状态，主题变化沿用侧栏图标。
        """
        if self._disposed:
            return
        session = self.active_session
        source = session.inspiration.new_action if session and not session.disposed else None
        self._new_inspiration_action.setEnabled(source is not None and source.isEnabled())
        self._new_inspiration_action.setVisible(source is not None)
        self._new_inspiration_action.setIcon(source.icon() if source else QIcon())


    def _bind_session(self, session):
        """仅切换堆叠页和动作，不重建业务面板，不改变其他会话的选择和滚动位置。"""
        self._outline_stack.setCurrentWidget(session.panels.outline) if session else self._outline_stack.setCurrentIndex(0)
        self._favorites_stack.setCurrentWidget(session.favorites) if session else self._favorites_stack.setCurrentIndex(0)
        self._inspiration_stack.setCurrentWidget(session.inspiration) if session else self._inspiration_stack.setCurrentIndex(0)
        self._recent_pages_stack.setCurrentWidget(session.recent_pages) if session else self._recent_pages_stack.setCurrentIndex(0)
        self._properties_stack.setCurrentWidget(session.panels.properties) if session else self._properties_stack.setCurrentIndex(0)
        self._settings_stack.setCurrentWidget(session.panels.settings) if session else self._settings_stack.setCurrentIndex(0)
        self._status_controls.bind_canvas(session.canvas if session and not session.pages.active else None)
        self._status_controls.set_canvas_controls_visible(session is None or not session.pages.active)
        self._sync_depth_action()
        self._sync_new_inspiration_action()
        self._sync_document()
        # 欢迎页临时暂停依赖导图的侧栏，不把“没有文档”写成用户主动隐藏面板。
        # 关闭活动标签到下一标签的短暂空绑定不暂停，只有关闭全部导图才回欢迎页。
        if not self._disposed:
            panels = ("outline", "favorites", "inspiration", "recent_pages", "properties", "map_settings")
            self.context.ui.set_panels_suspended(panels, session is None)
            if session is not None and session.pages.active:
                self.context.ui.set_panels_suspended(("properties", "map_settings"), True)


    def _set_read_only(self, checked):
        if self.canvas:
            self.canvas.set_editable(not checked)


    def _show_properties(self, session=None):
        session = session or self.active_session
        if session:
            self.context.documents.activate_view(session.id)
            session.canvas.finish_edit(commit=True)
            self.context.ui.activate_panel("properties")


    def show_settings_panel(self):
        """打开活动导图的布局及外观设置；不强制结束正文编辑或另建临时导图。

        与节点属性复用右上停靠格，Alt+3 和侧边按钮解析同一活动会话。
        focus=False 使快捷键查看设置时保留正文草稿，编辑期间控件维持禁用。
        """
        if self.active_session and not self._disposed and not self.active_session.pages.active:
            self.context.ui.activate_panel("map_settings", focus=False)


    def schedule_recent_pages_save(self) -> None:
        """合并连续切页引起的本机写入；不触发导图自动保存或改变退出快照。"""
        if not self._disposed:
            self._recent_pages_timer.start()


    def flush_recent_pages(self) -> None:
        """防抖到期、关闭标签或退出时保存访问记录；失败不阻止浏览和关闭。

        状态层保留尚未保存的内存快照，下一次访问或显式收尾可再次尝试。
        提示统一交给现有状态栏及操作日志，不弹出阻塞对话框。
        """
        self._recent_pages_timer.stop()
        try:
            self.page_history.flush()
        except OSError as error:
            message = f"最近访问记录未能保存：{error}"
            self.show_status_message(message)
            self.record_operation(message, level="warning")


    def _layout_changed(self):
        if self._disposed:
            return
        visible = self.context.ui.panel_visible("properties")
        if getattr(self, "_properties_visible", visible) and not visible and self.panels:
            self.panels.flush_pending_edits()
        self._properties_visible = visible
        settings_visible = self.context.ui.panel_visible("map_settings")
        if getattr(self, "_settings_visible", settings_visible) and not settings_visible and self.panels:
            self.panels.settings.flush()
        self._settings_visible = settings_visible
        # 恢复布局会重建标签容器，文件路径和保存错误属于业务元信息，须重新附加。
        for session in self.sessions.values():
            self.sessions.sync(session)

