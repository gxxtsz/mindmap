"""通用个人工作台宿主；所有编辑器行为均从已注册的插件能力取得。"""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
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
from shiboken6 import isValid
from workbench_api import EditorProvider, OperationStatus, PluginContext
from workbench_api.qt import MenuLocation

from .commands import EditCommandRouter
from .config import WorkbenchConfig
from .documents import DocumentService, MissingView
from .operation_log import OperationLogPanel
from .palette import colors_from_theme
from .plugins import PluginManager
from .recent_files import RecentFilesButton
from .services import CommandRegistry, EditorRegistry, ScopedDocuments, UiService, menu_location
from .state import Settings, StateStore, WorkspaceLayoutStore
from .storage import JsonLayoutStore
from .ui.docking import DockManager, DockPosition, Theme, ToolWindowSpec
from .ui.shell import ActionSpec, AppSpec, WorkbenchWindow


class WelcomePage(QWidget):
    """为支持新建的编辑器生成入口，并提供打开文件和插件管理。"""

    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.addStretch()
        title = QLabel(window.config.title, self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:24px;font-weight:600;")
        layout.addWidget(title)
        for identifier, provider in window._new_document_providers(respect_active_view=False).items():
            button = QPushButton("新建" + provider.title, self)
            button.setFixedWidth(180)
            button.clicked.connect(lambda checked=False, key=identifier: window.new_document(provider_id=key))
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)
        for title, callback in (("打开文件", window.open_dialog), ("插件管理", window.show_plugin_manager)):
            button = QPushButton(title, self)
            button.setFixedWidth(180)
            button.clicked.connect(lambda checked=False, action=callback: window.call(action))
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        self.refresh_theme()

    def refresh_theme(self):
        theme = self.window.manager.theme
        self.setStyleSheet(f"background:{theme.css('editor-bg')};color:{theme.css('text-default')};")


class WorkbenchRuntimeWindow(WorkbenchWindow):
    """统一的窗口装配器，不导入编辑器业务模块，也不判断具体插件标识。"""

    def __init__(self, *, config: WorkbenchConfig | None = None, storage_dir=None, theme=None,
                 restore=True, manifests=None, migration=None, plugin_options=None,
                 profile_upgrade: Callable[[dict, dict], tuple[bool, bool]] | None = None):
        """装配插件宿主；产品配置和历史转换由应用注入，空清单可启动空工作台。

        storage_dir 显式提供时优先使用该位置，保证测试隔离及历史目录连续性。
        config 只影响产品标识与显示信息，不改变文档资源、插件 ID 或布局格式。
        profile_upgrade 在插件激活与会话恢复前接收已加载的状态和设置值字典，
        允许原地修改，并按顺序返回状态、设置是否变更。即使 restore=False 也会调用。
        只有有效的现有配置在变更后才立即原子写回，缺失或损坏配置沿用常规保存流程。
        写回失败时保留原文件，窗口继续使用升级后的内存数据，并在操作日志中报告。
        """
        self.config = config or WorkbenchConfig()
        self._disposed = False
        self._ready = False
        self._opening_dropped_files = False
        self.providers = {}
        self.command_specs = {}
        self.menu_contributions = []
        self.status_items = {}
        self.default_panel_ids = {"operation_log"}
        self._plugin_options = plugin_options or {}
        self._contexts = {}
        directory = Path(storage_dir) if storage_dir is not None else self.config.default_storage_dir()
        initial_state, initial_settings = migration(directory) if migration else (None, {})
        self.state = StateStore(directory, initial_state)
        settings_store = JsonLayoutStore(directory / "workbench-settings-v1.json")
        self.settings, settings_error = Settings.load(settings_store, initial_settings)
        upgrade_warnings = self._upgrade_profile(profile_upgrade, settings_error) if profile_upgrade is not None else ()
        manager = DockManager(application_id=self.config.application_id, storage_dir=directory,
                              theme=Theme(theme or "light"), preserve_open_documents=True,
                              shortcut_context=Qt.ShortcutContext.WindowShortcut,
                              layout_store=WorkspaceLayoutStore(
                                  self.state, lambda: (self.documents.snapshot(), self.documents.active_id)))
        manager.storage_file = manager.layout_store.path
        super().__init__(AppSpec(self.config.application_id, self.config.title, icon=self.config.icon,
                                 storage_dir=directory, theme=manager.theme), manager=manager)
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
        self.set_title_bar_navigation(self.recent_files_button)
        self.documents.changed.connect(self.refresh_document)
        self.plugins = PluginManager(self._make_context, self.settings.scoped("plugins"), manifests)
        self.plugins.activate()
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
        self.documents.active_changed.emit(self.documents.active)
        self.refresh_document()
        for record in self.plugins.records:
            if record.error:
                self.record_operation(f"插件 {record.name} 加载失败：{record.error}", level="error")
        for warning in upgrade_warnings:
            self.record_operation(warning, level="warning")
        if failures:
            self.show_status_message("部分文件暂不可用：" + "；".join(failures))
        elif self.state.error:
            self.show_status_message(self.state.error)
        elif settings_error:
            self.show_status_message(settings_error)
        elif upgrade_warnings:
            self.show_status_message("；".join(upgrade_warnings))
        self.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)

    def _upgrade_profile(self, upgrade, settings_error):
        """将升级前已校验的两份配置分别提交，单个文件失败可在正常保存时重试。"""
        state_changed, settings_changed = upgrade(self.state.data, self.settings.values)
        warnings = []
        # 此时会话尚未恢复，直接提交清理过的完整快照，保留恢复期间的保存屏障。
        for changed, load_error, path, save in (
            (state_changed, self.state.error, self.state.store.path, lambda: self.state.store.save(self.state.data)),
            (settings_changed, settings_error, self.settings.store.path, self.settings.sync),
        ):
            if changed and not load_error:
                try:
                    if path.is_file():
                        save()
                except OSError as error:
                    warnings.append(f"启动时未能更新配置 {path.name}：{error}")
        return warnings

    def _make_context(self, plugin_id):
        context = PluginContext(plugin_id, None, None, None, None, self.settings.scoped(plugin_id),
                                self.state.plugin_state(plugin_id), self.record_operation,
                                self._plugin_options.get(plugin_id, {}))
        context.editors = EditorRegistry(self, context)
        context.commands = CommandRegistry(self, context)
        context.ui = UiService(self, context)
        context.documents = ScopedDocuments(self.documents, context)
        self._contexts[plugin_id] = context
        return context

    def _register_actions(self):
        for identifier, title, callback, shortcut, icon in (
            ("new", "新建", self.new_document, "Ctrl+N", "new-document"),
            ("open", "打开…", self.open_dialog, "Ctrl+O", "folder"),
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
            if not getattr(error, "_workbench_reported", False):
                self.record_operation(f"操作失败：{error}", level="error")
            error._workbench_reported = False
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

    def new_document(self, path=None, *, provider_id=None):
        """按显式标识、文件后缀或当前活动视图选择支持新建的提供者。

        显式选择的提供者无法新建时返回 False。省略标识和文件后缀时，活动文档的
        提供者只支持打开则返回 False。有新建能力时优先选活动提供者，否则在没有
        可用活动提供者时按注册顺序选取。
        """
        if provider_id is None and path is not None and Path(path).suffix:
            provider_id = self.provider_for(path)
            if provider_id is None:
                return False
        if provider_id is None:
            providers = self._new_document_providers()
            active = self.documents.providers.get(self.documents.active_id)
            provider_id = active if active in providers else next(iter(providers), None)
        entry = self.providers.get(provider_id)
        create = entry[1].new_document if entry is not None else None
        if create is None:
            return False
        result = create(Path(path) if path else None)
        self._report_result(result)
        return bool(result)

    def _new_document_providers(self, *, respect_active_view: bool = True) -> dict[str, EditorProvider]:
        """按注册顺序返回具备新建回调的提供者。

        文件菜单、快捷键和隐式新建共用当前文档的能力判断。欢迎页的按钮带有明确的
        提供者标识，可以列出工作台中所有支持新建的类型。
        """
        if respect_active_view:
            entry = self.providers.get(self.documents.providers.get(self.documents.active_id))
            if entry is not None and entry[1].new_document is None:
                return {}
        return {identifier: provider for identifier, (_, provider) in self.providers.items()
                if provider.new_document is not None}

    def provider_for(self, path):
        extension = Path(path).suffix.lower()
        return next((key for key, (_, provider) in self.providers.items() if extension in provider.extensions), None)

    def open_path(self, path):
        identifier = self.provider_for(path)
        if identifier is None:
            raise ValueError(f"没有启用的编辑器支持此文件：{path}")
        existing = self.documents.find_path(path)
        if existing and not isinstance(existing, MissingView):
            self.documents.activate_view(existing.id)
            self.remember_path(path)
            self.record_operation("激活已打开的文档", session=existing)
            return True
        result = self.providers[identifier][1].open_document(Path(path), None, None)
        if result.status == OperationStatus.FAILED:
            error = result.error or ValueError(result.message)
            error._workbench_reported = result.reported
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
        filters = [f"{provider.title} ({' '.join('*' + ext for ext in provider.extensions)})"
                   for _, provider in self.providers.values()]
        extensions = " ".join("*" + ext for _, provider in self.providers.values() for ext in provider.extensions)
        path, _ = QFileDialog.getOpenFileName(self, "打开文件", "",
                                             ";;".join([f"支持的文件 ({extensions})", *filters]))
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

    def appearance_menu(self):
        menu = super().appearance_menu()
        menu.setTitle("设置")
        if self._ready:
            menu.addSeparator()
            menu.addAction("插件管理…", self.show_plugin_manager)
        return menu

    def show_plugin_manager(self):
        self.plugins.show_dialog(self)

    def main_menu(self) -> QMenu:
        """按宿主分区装配临时主菜单，标准编辑动作始终由工作台摆放。"""
        self.edit_commands.refresh()
        menu, groups, settings = self._build_main_menu()
        files = groups["文件"]
        sections = {MenuLocation.FILE: files, MenuLocation.SETTINGS: settings}
        for location, title in ((MenuLocation.EDIT, "编辑"), (MenuLocation.VIEW, "视图")):
            section = groups.get(title)
            if section is None:
                section = QMenu(title, menu)
                menu.insertMenu(settings.menuAction(), section)
            sections[location] = section

        self._populate_new_menu(files)
        edit = sections[MenuLocation.EDIT]
        first = next(iter(edit.actions()), None)
        edit.insertActions(first, list(self.edit_commands.actions.values()))
        edit.insertSeparator(self.edit_commands.actions["cut"])
        if first is not None:
            edit.insertSeparator(first)

        exports = QMenu("导出当前页", files)
        sections[MenuLocation.FILE_EXPORT] = exports
        self._append_plugin_menu_items(sections)
        export_actions = [action for action in exports.actions() if not action.isSeparator()]
        if export_actions:
            exports.setIcon(self.manager.theme.icon("export-image"))
            exports.menuAction().setProperty("workbench_icon", "export-image")
            exports.setEnabled(any(action.isEnabled() for action in export_actions))
            files.insertMenu(self.action("app.quit"), exports)
        self._append_panels_menu(sections[MenuLocation.VIEW])
        return menu

    def _populate_new_menu(self, menu):
        """按当前文档能力装配新建菜单，单项复用主动作，多项从子菜单选择。"""
        action = self.action("app.new")
        providers = self._new_document_providers()
        if not providers:
            menu.removeAction(action)
            return
        if len(providers) == 1:
            provider = next(iter(providers.values()))
            action.setText("新建" + provider.title)
            return
        action.setText("新建")
        choices = QMenu("新建", menu)
        choices.setIcon(action.icon())
        choices.menuAction().setProperty("workbench_icon", action.property("workbench_icon"))
        for identifier, provider in providers.items():
            choice = choices.addAction(self.manager.theme.icon(provider.icon), provider.title)
            choice.triggered.connect(lambda checked=False, key=identifier:
                                     self.call(lambda: self.new_document(provider_id=key)))
        menu.insertMenu(action, choices)
        # 仅从弹出菜单移除，新建命令仍挂在工作台窗口上并保留 Ctrl+N。
        menu.removeAction(action)

    def _append_plugin_menu_items(self, sections):
        """先装配单个贡献，再合入宿主分区。失败的贡献不会留下半组动作。"""
        items = {location: [] for location in sections}
        self._collect_legacy_view_menu_items(items)
        for plugin_id, contribution in tuple(self.menu_contributions):
            section = sections[contribution.menu]
            staging = QMenu(section.title(), section)
            try:
                contribution.populate(staging)
                items[contribution.menu].extend(staging.actions())
            except Exception as error:
                staging.deleteLater()
                self.record_operation(f"插件 {plugin_id} 的菜单填充失败：{error}", level="error")
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

    def _collect_legacy_view_menu_items(self, items):
        """兼容旧视图的 menu_actions 扩展，校验整组后再合入菜单。"""
        view = self.documents.active
        try:
            groups = [(menu_location(name), list(actions))
                      for name, actions in getattr(view, "menu_actions", {}).items()]
            if any(not isinstance(action, QAction) or not isValid(action)
                   for _, actions in groups for action in actions):
                raise TypeError("编辑器菜单动作必须为有效的 QAction")
        except Exception as error:
            plugin_id = self.documents.owners.get(view.id, "未知")
            self.record_operation(f"插件 {plugin_id} 的编辑器菜单填充失败：{error}", level="error")
            return
        for location, actions in groups:
            items[location].extend(actions)

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
        self.action("app.new").setEnabled(bool(self._new_document_providers()))
        self.action("app.open").setEnabled(bool(self.providers))
        title = self.config.title
        if view:
            title = f"{'* ' if view.document.is_dirty else ''}{self.documents.display_title(view)} — {title}"
        self.setWindowTitle(title)
        self.status_label.setText("  " + (getattr(view, "status_text", "就绪") if view else "新建或打开文件以开始"))
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
                 self.provider_for(url.toLocalFile())] if mime.hasUrls() else []
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
        self.plugins.dispose()
