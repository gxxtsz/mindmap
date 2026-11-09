"""桌面外壳接入边界：文档、停靠面板、应用动作与局部主题映射。"""

from copy import deepcopy
from os.path import isdir
from pathlib import Path

from mindmap_core import MindMapError
from mindmap_docking import DockManager, DockPosition, Theme, ToolWindowSpec
from mindmap_formats import create_default_format_registry
from mindmap_layouts import create_default_layout_registry
from mindmap_qt_canvas import MindMapFileController
from mindmap_qt_render import ensure_fonts
from mindmap_qt_ui import UiColors
from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow, run_desktop
from PySide6.QtCore import QEvent, QLibraryInfo, QSettings, QSignalBlocker, Qt, QTimer, QTranslator, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from .document_state import DocumentState
from .operation_log import OperationLogPanel
from .recent_files import RecentFilesButton
from .sessions import MindMapSession, MindMapSessionManager
from .status_controls import _StatusViewControls


def _format_file_size(size: int | None) -> str:
    """将已缓存的字节数转换为中文状态栏使用的大小文本，不访问文件系统。

    None 表示当前无法读取磁盘大小，显示“未知”；字节使用整数，其余单位按
    1024 进制转换并保留一位小数。最大使用 GiB，不影响实际文件内容或保存状态。
    """
    if size is None:
        return "未知"
    units = ("B", "KiB", "MiB", "GiB")
    value, index = float(size), 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{size} B" if index == 0 else f"{value:.1f} {units[index]}"


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


class _WorkbenchLayoutStore:
    """在既有布局存储边界补齐新面板，原始校验及原子写入仍交给停靠框架。"""

    def __init__(self, store, default_tools):
        self.store = store
        self.default_tools = deepcopy(default_tools)

    def load(self) -> dict | None:
        """读取旧布局时补齐日志和最近访问；已保存的新面板显隐及位置一律保留。

        只迁移结构可识别的版本 1 数据，损坏结构原样交回管理器统一校验；读取
        不写文件，下一次正常布局保存才落盘。复制数据，避免修改存储方缓存的快照。
        """
        payload = self.store.load()
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return payload
        session = payload.get("session")
        tools = session.get("tools") if isinstance(session, dict) else None
        if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
            return payload
        payload = deepcopy(payload)
        tools = payload["session"]["tools"]
        for default in self.default_tools:
            if any(tool.get("id") == default["id"] for tool in tools):
                continue
            tool = deepcopy(default)
            if tool["id"] == "operation_log":
                # 日志沿用原迁移规则，优先使用未占用的底部格。
                occupied = {item.get("position") for item in tools if item.get("visible")}
                if tool["position"] in occupied and DockPosition.BOTTOM_RIGHT not in occupied:
                    tool["position"] = tool["last_position"] = DockPosition.BOTTOM_RIGHT
            else:
                # 最近访问仅在首次加入旧布局时展开；同一停靠格只显示一个面板。
                # 保留被替换侧栏的位置等状态，之后仍可从原来的入口切回。
                for item in tools:
                    if (item.get("host_id") == tool["host_id"] and item.get("position") == tool["position"]
                            and item.get("mode") in ("dock_pinned", "dock_unpinned")):
                        item["visible"] = False
                        if payload["session"].get("active") == item.get("id"):
                            payload["session"]["active"] = tool["id"]
            tools.append(tool)
        return payload

    def save(self, payload: dict) -> None:
        """保存完整布局，不保存日志文本或页面访问记录，沿用原存储格式和失败语义。"""
        self.store.save(payload)


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
        self._restoring_recent_pages = True
        self._updating_preferences = False
        self._operation_error_token = object()
        self.layout_registry = layout_registry if layout_registry is not None else create_default_layout_registry()
        self.format_registry = format_registry if format_registry is not None else create_default_format_registry()
        manager = DockManager(application_id="mindmap-workbench", storage_dir=storage_dir,
                              theme=Theme(theme or "light"), preserve_open_documents=True,
                              shortcut_context=Qt.ShortcutContext.WindowShortcut)
        super().__init__(AppSpec("mindmap-workbench", "原生思维导图", subtitle="思维导图工作台",
                                storage_dir=storage_dir, theme=manager.theme), manager=manager)
        self.operation_log = OperationLogPanel(manager.parking)
        self.preferences = QSettings(str(manager.storage_dir / "preferences.ini"), QSettings.Format.IniFormat)
        # 置顶是本机窗口偏好，与是否恢复导图及布局无关；首次显示前应用，避免闪现。
        # INI 重读可能返回字符串，不能直接 bool("false")；无效值统一视为关闭。
        pinned = self.preferences.value("window/always_on_top", False)
        self.set_always_on_top(pinned is True or isinstance(pinned, str) and pinned == "true")
        self.always_on_top_changed.connect(self._save_always_on_top)
        self.document_state = DocumentState(manager.storage_dir / "documents-v1.json")
        self._recent_pages_timer = QTimer(self)
        self._recent_pages_timer.setSingleShot(True)
        self._recent_pages_timer.setInterval(500)
        self._recent_pages_timer.timeout.connect(self.flush_recent_pages)
        startup_messages = []
        try:
            self.document_state.load()
        except (OSError, ValueError, TypeError) as error:
            startup_messages.append(f"最近文件与退出会话记录无效，已忽略：{error}")
            self.record_operation(startup_messages[-1], level="warning")
        self._outline_stack = QStackedWidget(manager.parking)
        self._favorites_stack = QStackedWidget(manager.parking)
        self._inspiration_stack = QStackedWidget(manager.parking)
        self._recent_pages_stack = QStackedWidget(manager.parking)
        self._properties_stack = QStackedWidget(manager.parking)
        self._settings_stack = QStackedWidget(manager.parking)
        for stack in (self._outline_stack, self._favorites_stack, self._inspiration_stack, self._recent_pages_stack,
                      self._properties_stack, self._settings_stack):
            message = QLabel("没有打开的导图")
            message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stack.addWidget(message)
        self._depth_action = QAction("显示层级", self)
        self._new_inspiration_action = QAction("新建想法", self)
        self._new_inspiration_action.setToolTip("新建想法")
        self._new_inspiration_action.setEnabled(False)
        self._new_inspiration_action.setVisible(False)
        self._new_inspiration_action.triggered.connect(self._new_inspiration)
        self.sessions = MindMapSessionManager(self)
        # 视图按钮位于状态栏最右侧的常驻区，不会被临时状态消息遮挡。
        self._status_controls = _StatusViewControls(self.status)
        self.status.addPermanentWidget(self._status_controls)
        header_actions = {
            "outline": (self._depth_action,),
            "inspiration": (self._new_inspiration_action,),
        }
        for identifier, title, stack, icon, position, shortcut in (
            ("outline", "大纲", self._outline_stack, "outline", DockPosition.LEFT_TOP, "Alt+1"),
            ("favorites", "收藏", self._favorites_stack, "favorites", DockPosition.LEFT_TOP, "Alt+4"),
            ("inspiration", "灵感", self._inspiration_stack, "inspiration", DockPosition.LEFT_TOP, "Alt+6"),
            ("recent_pages", "最近访问", self._recent_pages_stack, "recent-pages", DockPosition.LEFT_TOP, "Alt+7"),
            ("properties", "节点属性", self._properties_stack, "properties", DockPosition.RIGHT_TOP, "Alt+2"),
            ("map_settings", "导图设置", self._settings_stack, "settings", DockPosition.RIGHT_TOP, ""),
        ):
            manager.register_tool_window(ToolWindowSpec(
                identifier, title, lambda content=stack: content, icon, position, shortcut,
                header_actions=header_actions.get(identifier, ()),
            ))
        manager.register_tool_window(ToolWindowSpec(
            "operation_log", "操作日志", lambda: self.operation_log, "log",
            DockPosition.BOTTOM_LEFT, "Alt+5",
            header_actions=(self.operation_log.copy_all_action, self.operation_log.clear_action),
        ))
        self.log_action = manager.actions["tool:operation_log"]
        self.log_action.setCheckable(True)
        self.recent_pages_action = manager.actions["tool:recent_pages"]
        self.recent_pages_action.setCheckable(True)
        self.host.editor.set_empty_widget_factory(lambda: _WelcomePage(self))
        self._register_file_actions()
        self.settings_action = manager.register_action("app.settings", "导图设置", self.show_settings_panel,
                                                       shortcut="Alt+3", icon="settings")
        self.read_only_action = QAction("只读模式", self, checkable=True)
        self.read_only_action.toggled.connect(self._set_read_only)
        self.recent_files_button = RecentFilesButton(self)
        self.set_title_bar_navigation(self.recent_files_button)
        manager.activate("recent_pages", focus=False)
        self.recent_pages_action.setChecked(True)
        manager.activate("properties", focus=False)
        manager.activate("operation_log", focus=False)
        self.log_action.setChecked(True)
        layout = manager.capture_layout()
        for sizes in layout["hosts"]["main"]["region_sizes"].values():
            sizes.update(left=260, right=360, bottom=260)
        manager.restore_layout(layout)
        manager.set_default_layout()
        manager.layout_store = _WorkbenchLayoutStore(
            manager.layout_store, [next(tool for tool in layout["tools"] if tool["id"] == identifier)
                                   for identifier in ("operation_log", "recent_pages")],
        )
        self._properties_visible = True
        self._settings_visible = False
        manager.layout_changed.connect(self._layout_changed)
        if restore:
            # 大文件后台等待会处理 Qt 事件，旧的 250 毫秒自动保存可能在读取布局前
            # 把默认布局写回磁盘。整个文件恢复和布局恢复共用持久化屏障，期间仍可
            # 正常刷新界面；全部状态恢复后才重新安排保存，不能只暂停第一份文件。
            with manager.defer_session_saving():
                failures = self.sessions.restore_documents(self.document_state.open_documents,
                                                          self.document_state.active_id)
                restored_active = self.sessions.active_id
                # 必须先注册文件，再按相同会话标识恢复外壳；反向顺序会把旧文档 token
                # 作为未知项过滤，导致全部文件挤进同一个标签组。
                manager.load_session()
                if restored_active:
                    manager.activate_document(restored_active)
            if failures:
                startup_messages.append("部分导图未能恢复：" + "；".join(failures))
        self.refresh_theme()
        self._bind_session(self.active_session)
        self._restoring_recent_pages = False
        if self.active_session is not None:
            self.active_session._record_page_visit()
        if startup_messages:
            self.show_status_message("；".join(startup_messages))
        self._opening_dropped_files = False
        self.setAcceptDrops(True)
        # 文本控件可能先接受文件 URL 并插入路径，父窗口收不到这些事件。
        # 在应用层先识别导图拖放，接收范围仍限制在本窗口及其子控件内。
        QApplication.instance().installEventFilter(self)

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

    def record_operation(self, message: str, *, session: MindMapSession | str | Path | None = None,
                         level: str = "info") -> None:
        """在 GUI 线程记录一条操作摘要，返回 None，不影响状态栏或当前活动导图。

        message 为中文说明；session 可以是所属 MindMapSession、尚未建立会话的
        文件路径（str 或 Path），或代表应用级事件的 None。来源在调用时冻结为文件名
        及完整路径字符串，分别供标签和悬停详情使用，不保存会话引用，也不随标签切换
        或另存为回写旧记录。level 接受 info、warning、
        error，语义由调用方明确指定。已释放的窗口或会话忽略迟到回调。
        """
        if self._disposed or not hasattr(self, "operation_log"):
            return
        if session is None:
            source, source_path = "应用", ""
        elif isinstance(session, (str, Path)):
            source = Path(session).name or "新建导图"
            source_path = str(session)
        else:
            if session.disposed:
                return
            source = session.files.path.name if session.files.path else "新建导图"
            source_path = str(session.files.path) if session.files.path else ""
        self.operation_log.append_entry(message, source=source, level=level, source_path=source_path)

    def _record_exception(self, error, *, operation="操作", session=None):
        """在业务失败边界记录异常，并标记本次传播，防止 UI 再重复记录同一失败。

        标记不持有窗口或文档；业务入口每次失败都重新记录，即使调用方复用异常
        实例也不吞掉后续失败。只有外层 call 消费标记，不根据错误文字做去重。
        """
        self.record_operation(f"{operation}失败：{error}", session=session, level="error")
        error._mindmap_operation_log_token = self._operation_error_token

    def report_tool_error(self, tool_id, error):
        """保留框架工具失败提示，并将失败原因写入应用级日志。"""
        super().report_tool_error(tool_id, error)
        self.record_operation(f"无法创建工具窗口 {tool_id}：{error}", level="error")

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

    def _register_file_actions(self):
        """全窗口文件快捷键仅注册一次；每次触发时再取得活动会话。"""
        for identifier, title, callback, shortcut, icon in (
            ("new", "新建", self.new_document, "Ctrl+N", "new-document"),
            ("open", "打开…", self.open_dialog, "Ctrl+O", "folder"),
            ("open_directory", "打开所在文件夹", self.open_file_directory, "", "open-directory"),
            ("save", "保存", self.save, "Ctrl+S", "save"),
            ("save_all", "保存全部", self.save_all, "Ctrl+Alt+S", "save-all"),
            ("save_as", "另存为…", lambda: self.save(save_as=True), "Ctrl+Shift+S", "save-as"),
            ("quit", "退出", self.close, "Ctrl+Q", "exit"),
        ):
            self.add_action(ActionSpec("app." + identifier, title,
                                       lambda action=callback: self.call(action), icon, shortcut, "文件", False))

    def _save_always_on_top(self, enabled: bool) -> None:
        """在窗口实际切换置顶后立即保存本机偏好，不改动导图及退出会话记录。"""
        self.preferences.setValue("window/always_on_top", enabled)
        self.preferences.sync()

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
        return ui_colors_from_shell(self.manager.theme)

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
        self._status_controls.bind_canvas(session.canvas if session else None)
        self._sync_depth_action()
        self._sync_new_inspiration_action()
        self._sync_document()
        # 欢迎页临时暂停依赖导图的侧栏，不把“没有文档”写成用户主动隐藏面板。
        # 关闭活动标签到下一标签的短暂空绑定不暂停，只有关闭全部导图才回欢迎页。
        self.manager.set_tools_suspended(("outline", "favorites", "inspiration", "recent_pages", "properties", "map_settings"),
                                         not bool(len(self.sessions)))

    def _set_read_only(self, checked):
        if self.canvas:
            self.canvas.set_editable(not checked)

    def _show_properties(self, session=None):
        session = session or self.active_session
        if session:
            self.manager.activate_document(session.id)
            session.canvas.finish_edit(commit=True)
            self.manager.activate("properties")

    def show_settings_panel(self):
        """打开活动导图的布局及外观设置；不强制结束正文编辑或另建临时导图。

        与节点属性复用右上停靠格，Alt+3 和侧边按钮解析同一活动会话。
        focus=False 使快捷键查看设置时保留正文草稿，编辑期间控件维持禁用。
        """
        if self.active_session and not self._disposed:
            self.manager.activate("map_settings", focus=False)

    def appearance_menu(self):
        """仅保留应用外观与布局；滚轮行为通过状态栏按钮直接切换。"""
        menu = super().appearance_menu()
        menu.setTitle("设置")
        return menu

    def _sync_document(self, *_):
        """文件状态属于会话；窗口标题、状态栏和全局动作仅反映当前活动会话。"""
        if self._disposed or not hasattr(self, "settings_action"):
            return
        session = self.active_session
        for identifier in ("save", "save_as"):
            self.action("app." + identifier).setEnabled(session is not None)
        self.action("app.open_directory").setEnabled(session is not None and session.files.path is not None)
        self.action("app.save_all").setEnabled(bool(len(self.sessions)))
        self.settings_action.setEnabled(session is not None)
        self.read_only_action.setEnabled(session is not None)
        with QSignalBlocker(self.read_only_action):
            self.read_only_action.setChecked(session is not None and not session.canvas.editable)
        if session:
            status = "保存失败" if session.last_error else "待保存" if session.files.is_dirty else "已保存"
            title = session.files.path.name
            self.setWindowTitle(f"{'* ' if session.files.is_dirty else ''}{title} — 原生思维导图")
            # 只统计活动画布当前页的只读节点集合，包含根节点、折叠节点和屏幕外节点。
            # 不跨子导图、详情页或其他想法累计；直接取长度，避免复制文档或受图元虚拟化影响。
            self.status_label.setText(f"  {len(session.canvas.document_view.nodes)} 个节点 · "
                                      f"{len(session.canvas.selected_node_ids())} 个选中 · {status} · "
                                      f"文件大小：{_format_file_size(session._file_size_bytes)}")
        else:
            self.setWindowTitle("原生思维导图")
            self.status_label.setText("  新建导图或打开文件以开始")
        if hasattr(self, "recent_files_button"):
            self.recent_files_button.refresh_document()

    def _remember_path(self, path):
        """文件操作成功后更新历史；配置写入失败只提示，不把已成功打开的文件报成失败。"""
        try:
            self.document_state.remember(path)
        except OSError as error:
            self.show_status_message(f"最近文件未能保存：{error}")
            self.record_operation(f"最近文件未能保存：{error}", level="warning")

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
            self.document_state.flush_recent_pages()
        except OSError as error:
            message = f"最近访问记录未能保存：{error}"
            self.show_status_message(message)
            self.record_operation(message, level="warning")

    def clear_recent_files(self):
        """清空最近文件，退出快照及运行中的全部会话继续保留。"""
        try:
            self.document_state.clear_recent()
        except OSError as error:
            self.show_status_message(f"最近文件未能保存：{error}")
            self.record_operation(f"最近文件未能保存：{error}", level="warning")

    def new_document(self, path=None):
        """选择路径并成功首次保存后新增会话，现有会话保持不变。"""
        return self.sessions.new_document(path)

    def open_path(self, path):
        """仅打开 .mindmap 导图；重复路径激活原标签，失败不替换任何现有文档。"""
        return self.sessions.open_path(path)

    def open_dialog(self):
        return self.sessions.open_dialog()

    def eventFilter(self, watched, event):
        """在主窗口内接收外部原生导图，返回是否已处理本次 Qt 拖放事件。

        只提取有效文件 URL 的绝对路径，节点、停靠和文本拖放交还原控件。
        进入与移动阶段只检查载荷；松手后仅保留独立的路径，避免延迟回调
        使用已经失效的事件或 MIME 对象。
        """
        kind = event.type()
        # 标题栏和空白页的 window 属性保存宿主引用，用 Qt 方法查询实际顶层窗口。
        if (kind not in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop)
                or not isinstance(watched, QWidget) or QWidget.window(watched) is not self
                or event.source() is not None):
            return super().eventFilter(watched, event)

        paths = []
        for url in event.mimeData().urls():
            if not url.isValid() or not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if not path.is_absolute():
                continue
            try:
                paths.append(MindMapFileController.validate_open_path(path))
            except MindMapError:
                continue
        if not paths:
            return super().eventFilter(watched, event)

        # 显式选择复制动作，避免系统因默认移动动作而移动源文件。
        if self._opening_dropped_files or not (event.possibleActions() & Qt.DropAction.CopyAction):
            event.ignore()
            return True
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if kind == QEvent.Type.Drop:
            # 先占用批次，再退出系统拖放事件；大文件打开可进入模态事件循环，
            # 不能让第二个拖放回调在第一批尚未结束时重入会话管理器。
            self._opening_dropped_files = True
            QTimer.singleShot(0, self, lambda: self._open_dropped_files(paths))
        return True

    def _open_dropped_files(self, paths: list[Path]) -> None:
        """松手后在 GUI 线程依次打开导图，复用标签、最近文件与错误处理流程。"""
        try:
            for path in paths:
                if self._disposed:
                    break
                # 文件夹也可能以 .mindmap 结尾；松手后再查类型，失效路径
                # 仍交给打开入口报告，以便在操作日志中定位失败文件。
                if isdir(path):
                    continue
                # 读取异常由 call 报告并返回 None；False 表示用户取消加载，
                # 此时停止余下文件，已打开的标签继续保留。
                if self.call(lambda: self.open_path(path)) is False:
                    break
        finally:
            self._opening_dropped_files = False

    def open_file_directory(self, session_id: str | None = None) -> bool:
        """通过系统文件管理器打开目标导图所在文件夹，返回打开请求是否被接受。

        session_id 为 None 时使用触发时的活动会话，否则按稳定标签标识查找；窗口
        已释放、标签失效或没有文件路径时返回 False。每次读取最新路径，另存为及
        同文件多标签无需重新绑定回调。只检查父目录，文件已删除但目录存在仍可用。
        该操作仅限 GUI 线程，不主动提交编辑、保存或切换标签，也不调整最近文件。
        失败只显示中文状态提示并记录目标导图日志，不改变导图的保存错误状态。
        """
        if self._disposed:
            return False
        session = self.active_session if session_id is None else self.sessions.get(session_id)
        if session is None or session.disposed or session.files.path is None:
            return False
        directory = session.files.path.parent
        try:
            if not directory.is_dir():
                raise MindMapError(f"文件夹不存在或无法访问：{directory}")
            # 使用本地文件 URL 保留中文、空格及 #、% 等字符，避免把路径解释为网址。
            # 不要求文件仍然存在，也不借用保存流程，以保留草稿和既有保存状态。
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                raise MindMapError(f"系统无法打开文件夹：{directory}")
        except Exception as error:
            message = f"打开所在文件夹失败：{error}"
            self.record_operation(message, session=session, level="error")
            self.show_status_message(message)
            return False
        self.record_operation(f"打开所在文件夹：{directory}", session=session)
        return True

    def save(self, *, save_as=False):
        """保存动作按触发时的活动会话处理，成功后清除该会话自动保存错误。"""
        session = self.active_session
        if not session:
            return False
        if session.is_composing:
            session._failed("请先完成输入法候选，再保存导图")
            return False
        # 另存为取消时仍沿用原路径的防抖保存；成功或写入失败由文件状态通知
        # 停止计时器。不能在打开对话框前停掉已经排队的自动保存。
        try:
            result = session.files.save(save_as=True) if save_as else session.flush()
        except Exception as error:
            self._record_exception(error, operation="另存为" if save_as else "保存导图", session=session)
            raise
        self.sessions.sync(session)
        if result and save_as:
            self._remember_path(session.files.path)
        if result:
            self.record_operation(f"{'另存为' if save_as else '保存导图'}：{session.files.path}", session=session)
        return result

    def save_all(self):
        return self.sessions.save_all()

    def main_menu(self):
        """稳定的四个主菜单；空会话使用禁用占位，文件操作始终动态路由。"""
        menu = super().main_menu()
        files = next(action.menu() for action in menu.actions() if action.text() == "文件")
        can_export = self.active_session is not None and bool(self.canvas.document_view.nodes)
        exports = QMenu("导出图形", files)
        exports.setIcon(self.manager.theme.icon("export-image"))
        exports.menuAction().setProperty("mindmap_icon", "export-image")
        exports.setEnabled(can_export)
        for format_id, label in (("png", "PNG 图片"), ("svg", "SVG 矢量图"), ("pdf", "PDF 文档")):
            action = exports.addAction(label + "…")
            action.setData(format_id)
            action.triggered.connect(lambda checked=False, selected=format_id:
                                     self.call(lambda: self.files.export_dialog(selected)) if self.files else None)
        files.insertMenu(self.action("app.quit"), exports)
        # TXT 与图形共用空态检查，触发时再取活动会话，支持分屏及只读页面。
        export_text = QAction(self.manager.theme.icon("outline"), "导出当前页为 TXT…", files)
        export_text.setProperty("mindmap_icon", "outline")
        export_text.setEnabled(can_export)
        export_text.triggered.connect(
            lambda: self.call(lambda: self.files.export_dialog("txt")) if self.files else None
        )
        files.insertAction(self.action("app.quit"), export_text)
        anchor = next(action for action in menu.actions() if action.text() == "设置")
        for title, keys in (
            ("编辑", ("undo", "redo", "add_inspiration", "move_to_inspiration", "attach_inspirations",
                    "group_inspirations", "add_child", "add_sibling", "insert_parent", "edit", "properties",
                    "delete", "convert_descendants_to_submap", "convert_submap_to_descendants",
                    "set_page_reference", "open_page_reference", "clear_page_reference",
                    "copy", "cut", "paste", "image", "image_url")),
            ("视图", ("zoom_in", "zoom_out", "expand_all", "collapse_all")),
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
                section.addSeparator()
                section.addAction(self.recent_pages_action)
                section.addAction(self.log_action)
            menu.insertMenu(anchor, section)
        return menu

    def refresh_theme(self):
        """对所有会话同步界面色板，导图内容和自动保存计时不因外壳主题变化而修改。"""
        super().refresh_theme()
        if hasattr(self, "recent_files_button"):
            self.recent_files_button.refresh_theme()
        if hasattr(self, "sessions"):
            colors = ui_colors_from_shell(self.manager.theme)
            for session in self.sessions.values():
                session.canvas.set_ui_colors(colors)
                session.panels.set_ui_colors(colors)
                session.favorites.set_ui_colors(colors)
                session.inspiration.set_ui_colors(colors)
                session.recent_pages.set_ui_colors(colors)
            for stack in (self._outline_stack, self._favorites_stack, self._inspiration_stack, self._recent_pages_stack,
                          self._properties_stack, self._settings_stack):
                palette = stack.palette()
                palette.setColor(QPalette.ColorRole.Window, self.manager.theme.color("tool-window-bg"))
                stack.setPalette(palette)
                stack.setAutoFillBackground(True)
            for group in self.host.editor.groups:
                if group.empty_widget:
                    group.empty_widget.refresh_theme()
            self._sync_depth_action()
        if hasattr(self, "_status_controls"):
            self._status_controls.set_ui_colors(ui_colors_from_shell(self.manager.theme),
                                                compact=self.manager.theme.compact)
        if hasattr(self, "operation_log"):
            self.operation_log.set_ui_colors(ui_colors_from_shell(self.manager.theme),
                                             compact=self.manager.theme.compact)
        if hasattr(self, "log_action"):
            self.log_action.setIcon(self.manager.theme.icon("log"))
        if hasattr(self, "recent_pages_action"):
            self.recent_pages_action.setIcon(self.manager.theme.icon("recent-pages"))

    def _layout_changed(self):
        if self._disposed:
            return
        self.log_action.setChecked(self.manager.states["operation_log"].visible)
        self.recent_pages_action.setChecked(self.manager.states["recent_pages"].visible)
        visible = self.manager.states["properties"].visible
        if getattr(self, "_properties_visible", visible) and not visible and self.panels:
            self.panels.flush_pending_edits()
        self._properties_visible = visible
        settings_visible = self.manager.states["map_settings"].visible
        if getattr(self, "_settings_visible", settings_visible) and not settings_visible and self.panels:
            self.panels.settings.flush()
        self._settings_visible = settings_visible
        # 恢复布局会重建标签容器，文件路径和保存错误属于业务元信息，须重新附加。
        for session in self.sessions.values():
            self.sessions.sync(session)

    def dispose(self):
        """主窗口通过全部保存检查后停止所有会话；重复调用安全。"""
        if not self._disposed:
            self.flush_recent_pages()
            self._disposed = True
            QApplication.instance().removeEventFilter(self)
            self.setAcceptDrops(False)
            # 先停用停靠层的全局焦点监听，再删除业务控件；显式释放入口也不能
            # 让延迟焦点回调继续访问已经销毁的画布。正常关闭时 shutdown 已执行。
            self.manager.shutdown()
            self.sessions.dispose()

    def hideEvent(self, event):
        if hasattr(self, "sessions"):
            for session in self.sessions.values():
                session.panels.close_popups()
        super().hideEvent(event)

    def closeEvent(self, event):
        if self._disposed:
            event.accept()
            return
        # 先按视觉标签顺序保存全部文件，任何失败均不让基类关闭或删除停靠页面。
        # 正式启动器在事件循环退出后也可能先关闭停靠管理器；此时仍须完成业务保存。
        if not self.sessions.save_all():
            event.ignore()
            return
        super().closeEvent(event)
        if event.isAccepted():
            # 基类已完成全部关闭检查及布局保存，但 Qt 延迟删除尚未执行。此处仍能
            # 读取真实标签树和活动项；dispose 后会话集合清空，不能再生成退出快照。
            try:
                self.document_state.save_session([
                    {"id": session.id, "path": str(session.files.path),
                     "view_state": session.canvas.capture_page_view_state()}
                    for session in self.sessions.ordered()
                ], self.sessions.active_id)
            except OSError as error:
                self.show_status_message(f"退出会话未能保存：{error}")
                self.record_operation(f"退出会话未能保存：{error}", level="warning")
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
