"""导图桌面运行时：直接装配唯一应用，协调窗口、标签、配置与会话。"""

from collections.abc import Callable
from pathlib import Path

from mindmap_desktop_api import ApplicationContext, MindMapApplication, OperationStatus
from mindmap_desktop_api.qt import MenuLocation
from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .commands import EditCommandRouter
from .config import DesktopConfig
from .documents import DocumentService, MissingView
from .operation_log import OperationLogPanel
from .palette import colors_from_theme
from .recent_files import RecentFilesButton
from .services import ApplicationDocuments, CommandRegistry, UiService
from .state import SessionLayoutStore, Settings, StateStore
from .storage import JsonLayoutStore
from .ui._window_placement import restore_window_placement
from .ui.docking import DockManager, DockPosition, Theme, ToolWindowSpec
from .ui.shell import ActionSpec, AppSpec, DesktopWindow


class WelcomePage(QWidget):
    """只提供新建和打开导图的欢迎页。"""

    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.addStretch()
        title = QLabel(window.config.title, self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:24px;font-weight:600;")
        layout.addWidget(title)
        entries = [("新建导图", window.new_document), ("打开导图", window.open_dialog)]
        for title, callback in entries:
            button = QPushButton(title, self)
            button.setFixedWidth(180)
            button.clicked.connect(lambda checked=False, action=callback: window.call(action))
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        self.refresh_theme()

    def refresh_theme(self):
        theme = self.window.manager.theme
        self.setStyleSheet(f"background:{theme.css('editor-bg')};color:{theme.css('text-default')};")


class DesktopRuntimeWindow(DesktopWindow):
    """通过应用工厂直接持有导图控制器，节点扩展不参与窗口装配。"""

    _appearance_menu_title = "设置"

    def __init__(self, *, config: DesktopConfig | None = None, storage_dir=None, theme=None,
                 application_factory: Callable[[ApplicationContext], MindMapApplication],
                 restore=True, migration=None, options=None):
        """装配唯一导图应用，产品通过 application_factory 注入控制器。

        storage_dir 用于隔离本机配置；migration 只在读取前转换历史数据。
        主体初始化失败时回滚资源并抛出异常，不能降级成没有业务能力的窗口。
        """
        self.config = config or DesktopConfig()
        self._disposed = False
        self._ready = False
        self._opening_dropped_files = False
        self.application = None
        self.command_specs = {}
        self.menu_contributions = []
        self.status_items = {}
        self.default_panel_ids = {"operation_log"}
        directory = Path(storage_dir) if storage_dir is not None else self.config.default_storage_dir()
        initial_state, initial_settings = migration(directory) if migration else (None, {})
        self.state = StateStore(directory, initial_state)
        settings_store = JsonLayoutStore(directory / "mindmap-settings-v1.json")
        self.settings, settings_error = Settings.load(settings_store, initial_settings)
        manager = DockManager(application_id=self.config.application_id, storage_dir=directory,
                              theme=Theme(theme or "light"), preserve_open_documents=True,
                              remember_window_placement=True,
                              shortcut_context=Qt.ShortcutContext.WindowShortcut,
                              layout_store=SessionLayoutStore(
                                  self.state, lambda: (self.documents.snapshot(), self.documents.active_id)))
        manager.storage_file = manager.layout_store.path
        super().__init__(AppSpec(self.config.application_id, self.config.title, icon=self.config.icon,
                                 storage_dir=directory, theme=manager.theme), manager=manager)
        restore_window_placement(self)
        self.documents = DocumentService(self)
        self.operation_log = OperationLogPanel(manager.parking)
        manager.register_tool_window(ToolWindowSpec(
            "operation_log", "操作日志", lambda: self.operation_log, "log", DockPosition.BOTTOM_LEFT, "Alt+5",
            header_actions=(self.operation_log.copy_all_action, self.operation_log.clear_action),
        ))
        manager.activate("operation_log", focus=False)
        self._register_actions()
        self.edit_commands = EditCommandRouter(self)
        self.recent_files_button = RecentFilesButton(self)
        self.set_title_bar_navigation(self.recent_files_button, beside_window_controls=True)
        self.documents.changed.connect(self.refresh_document)
        self.context = self._make_context(options or {})
        try:
            application = application_factory(self.context)
            if not all(callable(getattr(application, name, None))
                       for name in ("new_document", "open_document", "dispose")):
                raise TypeError("应用工厂必须返回完整的导图应用")
            # 通过协议检查后才交给窗口托管，避免失败回滚调用不完整对象的释放接口。
            self.application = application
        except Exception:
            # 核心应用初始化失败必须终止装配，并释放已经登记的界面资源。
            # 不生成一个看似启动成功、实际没有导图能力的空窗口。
            self.dispose()
            self.deleteLater()
            raise
        self.host.editor.set_empty_widget_factory(lambda: WelcomePage(self))
        layout = manager.capture_layout()
        for sizes in layout["hosts"]["main"]["region_sizes"].values():
            sizes.update(left=260, right=360, bottom=260)
        manager.restore_layout(layout)
        manager.set_default_layout()
        manager.layout_store.default_tools = [item for item in layout["tools"]
                                               if item["id"] in self.default_panel_ids]
        pinned = self.settings.value("window/always_on_top", False)
        self.set_always_on_top(pinned is True or isinstance(pinned, str) and pinned == "true")
        self.always_on_top_changed.connect(self._save_pin)
        failures = []
        # 文件加载可能处理 Qt 事件，因此恢复屏障必须跨越全部文件及布局恢复。
        if restore:
            with manager.defer_session_saving():
                failures = self.documents.restore(self.state.open_documents, self.state.active_id)
                selected = self.documents.active_id
                manager.load_session()
                if selected in self.documents.views:
                    self.documents.activate_view(selected)
        if theme is not None:
            manager.set_theme(theme)
        self.state.restoring = False
        self._ready = True
        self.refresh_theme()
        self.documents.notify_active_changed()
        self.refresh_document()
        if failures:
            self.show_status_message("部分文件暂不可用：" + "；".join(failures))
        elif self.state.error:
            self.show_status_message(self.state.error)
        elif settings_error:
            self.show_status_message(settings_error)
        self.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)

    def _make_context(self, options):
        """建立导图主体独享的服务。生命周期与主窗口一致，不存在业务应用注册表。"""
        context = ApplicationContext(None, None, None, self.settings.scoped("mindmap"),
                                     self.state.application_state(), self.record_operation, options)
        context.commands = CommandRegistry(self, context)
        context.ui = UiService(self, context)
        context.documents = ApplicationDocuments(self.documents, context)
        return context

    def _register_actions(self):
        for identifier, title, callback, shortcut, icon in (
            ("new", "新建导图", self.new_document, "Ctrl+N", "new-document"),
            ("open", "打开导图…", self.open_dialog, "Ctrl+O", "folder"),
            ("open_directory", "打开所在文件夹", self.open_file_directory, "", "open-directory"),
            ("save", "保存", self.save, "Ctrl+S", "save"),
            ("save_all", "保存全部", self.save_all, "Ctrl+Alt+S", "save-all"),
            ("save_as", "另存为…", lambda: self.save(save_as=True), "Ctrl+Shift+S", "save-as"),
            ("quit", "退出", self.close, "Ctrl+Q", "exit"),
        ):
            self.add_action(ActionSpec("app." + identifier, title, lambda action=callback: self.call(action),
                                       icon, shortcut, "文件"))

    def call(self, callback):
        try:
            return callback()
        except Exception as error:
            if not getattr(error, "_mindmap_reported", False):
                self.record_operation(f"操作失败：{error}", level="error")
            error._mindmap_reported = False
            self.show_status_message(str(error))
            return None

    def record_operation(self, message, *, level="info", source="应用", source_path="", session=None,
                         title=None, detail=""):
        if self._disposed or not hasattr(self, "operation_log"):
            return
        if session is not None:
            path = Path(session) if isinstance(session, (str, Path)) else session.document.path
            source, source_path = (path.name, str(path)) if path else ("未命名", "")
        self.operation_log.append_entry(message, level=level, source=source, source_path=source_path,
                                        title=title, detail=detail)

    def report_tool_error(self, tool_id, error):
        super().report_tool_error(tool_id, error)
        self.record_operation(f"无法创建工具窗口 {tool_id}：{error}", level="error")

    def new_document(self, path=None):
        """唯一新建入口只创建导图；显式提供其他文件后缀时拒绝写入。"""
        if path is not None and Path(path).suffix and not self.can_open_path(path):
            return False
        result = self.application.new_document(Path(path) if path else None)
        self._report_result(result)
        return bool(result)

    @staticmethod
    def can_open_path(path) -> bool:
        """顶级标签只接受原生导图；节点扩展自行处理所属页面的导入来源。"""
        return Path(path).suffix.lower() == ".mindmap"

    def file_icon(self, path: Path | None, size: int = 16) -> QIcon:
        """导图列表及标签使用统一图标，非导图路径仅作为诊断文字展示。"""
        name = "mindmap" if path is not None and self.can_open_path(path) else "document"
        return self.manager.theme.icon(name, size)

    def open_path(self, path):
        if not self.can_open_path(path):
            raise ValueError(f"只能打开 .mindmap 导图：{path}；其他内容请在节点专用详情页中导入")
        existing = self.documents.find_path(path)
        if existing and not isinstance(existing, MissingView):
            self.documents.activate_view(existing.id)
            self.remember_path(path)
            self.record_operation("激活已打开的文档", session=existing)
            return True
        result = self.application.open_document(Path(path), None, None)
        if result.status == OperationStatus.FAILED:
            error = result.error or ValueError(result.message)
            error._mindmap_reported = result.reported
            raise error
        if result and existing:
            self.documents.remove_view(existing.id)
        self._report_result(result)
        return bool(result)

    def _report_result(self, result):
        if not result and result.message:
            self.show_status_message(result.message)
            if result.status == OperationStatus.FAILED and not result.reported:
                self.record_operation(result.message, level="error")

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开导图", "", "思维导图 (*.mindmap)")
        return self.open_path(path) if path else False

    def save(self, *, save_as=False):
        return bool(self.documents.save(save_as=save_as))

    def save_all(self):
        return bool(self.documents.save_all())

    def remember_path(self, path):
        try:
            self.state.remember(path)
        except OSError as error:
            self.show_status_message(f"最近文件未能保存：{error}")
            self.record_operation(f"最近文件未能保存：{error}", level="warning")

    def clear_recent_files(self):
        self.call(self.state.clear_recent)

    def open_file_directory(self, session_id=None):
        view = self.documents.active if session_id is None else self.documents.views.get(session_id)
        if self._disposed or view is None or view.document.path is None:
            return False
        directory = view.document.path.parent
        try:
            if not directory.is_dir() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                raise OSError(f"无法打开文件夹：{directory}")
        except Exception as error:
            self.record_operation(f"打开所在文件夹失败：{error}", session=view, level="error")
            self.show_status_message(f"打开所在文件夹失败：{error}")
            return False
        self.record_operation(f"打开所在文件夹：{directory}", session=view)
        return True

    def _menu_group_titles(self) -> tuple[str, ...]:
        """保留已声明分区的顺序，补齐导图桌面常驻的文件、编辑和视图入口。"""
        return tuple(dict.fromkeys((*super()._menu_group_titles(), "文件", "编辑", "视图")))

    def main_menu(self) -> QMenu:
        """按宿主分区装配临时主菜单，标准编辑动作始终由导图桌面摆放。"""
        self.edit_commands.refresh()
        menu, groups, settings = self._build_main_menu()
        files = groups["文件"]
        sections = {
            MenuLocation.FILE: files,
            MenuLocation.SETTINGS: settings,
            MenuLocation.EDIT: groups["编辑"],
            MenuLocation.VIEW: groups["视图"],
        }

        edit = sections[MenuLocation.EDIT]
        first = next(iter(edit.actions()), None)
        edit.insertActions(first, list(self.edit_commands.actions.values()))
        edit.insertSeparator(self.edit_commands.actions["cut"])
        if first is not None:
            edit.insertSeparator(first)

        exports = QMenu("导出当前页", files)
        sections[MenuLocation.FILE_EXPORT] = exports
        self._append_application_menu_items(sections)
        export_actions = [action for action in exports.actions() if not action.isSeparator()]
        if export_actions:
            exports.setIcon(self.manager.theme.icon("export-image"))
            exports.menuAction().setProperty("desktop_icon", "export-image")
            exports.setEnabled(any(action.isEnabled() for action in export_actions))
            files.insertMenu(self.action("app.quit"), exports)
        self._append_panels_menu(sections[MenuLocation.VIEW])
        return menu

    def _append_application_menu_items(self, sections):
        """先装配单个贡献，再合入宿主分区。失败的贡献不会留下半组动作。"""
        items = {location: [] for location in sections}
        for contribution in tuple(self.menu_contributions):
            section = sections[contribution.menu]
            staging = QMenu(section.title(), section)
            try:
                contribution.populate(staging)
                items[contribution.menu].extend(staging.actions())
            except Exception as error:
                staging.deleteLater()
                self.record_operation(f"导图菜单填充失败：{error}", level="error")
        for identifier, spec in self.command_specs.items():
            if spec.menu:
                items[spec.menu].append(self.action(identifier))

        view_actions = getattr(self.documents.active, "edit_actions", {})
        for location, actions in items.items():
            menu = sections[location]
            excluded = set(menu.actions())
            if location == MenuLocation.EDIT:
                excluded.update(view_actions[key] for key in self.edit_commands.actions if key in view_actions)
            additions = [action for action in dict.fromkeys(actions) if action not in excluded]
            if not additions:
                continue
            if location == MenuLocation.EDIT:
                menu.addSeparator()
            anchor = self.action("app.quit") if location == MenuLocation.FILE else None
            menu.insertActions(anchor, additions)

    def _append_panels_menu(self, menu):
        """按当前可用的工具生成显隐开关，停靠状态和切换行为仍由管理器维护。"""
        if menu.actions():
            menu.addSeparator()
        panels = menu.addMenu("面板")
        for identifier, spec in self.manager.specs.items():
            if not self.manager.is_tool_available(identifier):
                continue
            action = panels.addAction(self.manager.theme.icon(spec.icon), spec.title)
            action.setCheckable(True)
            action.setChecked(self.manager.states[identifier].visible)
            if spec.shortcut:
                # 菜单内显示快捷键，常驻的窗口快捷键仍由停靠管理器负责。
                action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
                action.setShortcut(spec.shortcut)
            action.triggered.connect(lambda checked=False, key=identifier:
                                     self.call(lambda: self.manager.toggle(key)))
        panels.setEnabled(bool(panels.actions()))

    def refresh_document(self, *_):
        if not self._ready or self._disposed:
            return
        view = self.documents.active
        self.edit_commands.refresh()
        writable = self.documents.can_save(view)
        for key in ("save", "save_as"):
            self.action("app." + key).setEnabled(writable)
        self.action("app.save_all").setEnabled(
            any(self.documents.can_save(item) for item in self.documents.views.values()))
        self.action("app.open_directory").setEnabled(view is not None and view.document.path is not None)
        self.action("app.new").setEnabled(self.application is not None)
        self.action("app.open").setEnabled(self.application is not None)
        title = self.config.title
        if view:
            title = f"{'* ' if view.document.is_dirty else ''}{self.documents.display_title(view)} — {title}"
        self.setWindowTitle(title)
        self.status_label.setText("  " + (getattr(view, "status_text", "就绪") if view else "新建或打开导图以开始"))
        for identifier, spec in self.command_specs.items():
            self.action(identifier).setEnabled(spec.enabled())
        for spec in self.status_items.values():
            spec.widget.setVisible(spec.visible())
        self.recent_files_button.refresh_document()

    def refresh_theme(self):
        super().refresh_theme()
        if not self._ready:
            return
        self.operation_log.set_ui_colors(colors_from_theme(self.manager.theme), compact=self.manager.theme.compact)
        self.recent_files_button.refresh_theme()
        for group in self.host.editor.groups:
            if group.empty_widget:
                group.empty_widget.refresh_theme()

    def _save_pin(self, value):
        self.settings.setValue("window/always_on_top", value)
        self.settings.sync()

    def confirm_close(self, view):
        return QMessageBox.question(self, "保存修改", f"是否保存“{view.title}”的修改？",
                                    QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                                    QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save)

    def eventFilter(self, watched, event):
        if self._ready and not self._disposed and watched is self and event.type() in (
            QEvent.Type.Move, QEvent.Type.Resize,
        ):
            self.manager.schedule_save()
        if (not self._ready or self._disposed or self._opening_dropped_files or
                event.type() not in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop) or
                not isinstance(watched, QWidget) or (watched is not self and not self.isAncestorOf(watched))):
            return super().eventFilter(watched, event)
        mime = event.mimeData()
        paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile() and
                 Path(url.toLocalFile()).is_absolute() and
                 self.can_open_path(url.toLocalFile())] if mime.hasUrls() else []
        if not paths:
            return super().eventFilter(watched, event)
        if not event.possibleActions() & Qt.DropAction.CopyAction:
            return False
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if event.type() == QEvent.Type.Drop:
            self._opening_dropped_files = True
            QTimer.singleShot(0, self, lambda: self._open_dropped_files(paths))
        return True

    def _open_dropped_files(self, paths):
        try:
            for path in paths:
                if self._disposed:
                    break
                if not path.is_dir() and self.call(lambda: self.open_path(path)) is False:
                    break
        finally:
            self._opening_dropped_files = False

    def closeEvent(self, event):
        if self._disposed:
            event.accept()
            return
        if not self.documents.prepare_close(list(self.documents.views)):
            event.ignore()
            return
        # 先捕获仍然存活的视图与分屏，再提交退出快照；释放过程不能反写空会话。
        try:
            self.manager.save_session(raise_errors=True)
            self.settings.sync()
        except (OSError, ValueError, TypeError) as error:
            self.show_status_message(f"退出会话未能保存：{error}")
            event.ignore()
            return
        self.dispose()
        event.accept()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        QApplication.instance().removeEventFilter(self)
        self.edit_commands.dispose()
        self.state.restoring = True
        self.manager.shutdown()
        self.documents.dispose()
        try:
            if self.application is not None:
                self.application.dispose()
        finally:
            self.context.dispose()
