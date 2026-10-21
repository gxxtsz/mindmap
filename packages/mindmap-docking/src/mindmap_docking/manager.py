"""状态协调器：原子迁移、焦点语义、主窗口内拖放与版本化布局持久化。"""

import copy
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, fields
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QEvent, QMimeData, QObject, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QDrag, QKeySequence
from PySide6.QtWidgets import QApplication, QMenu, QTabWidget, QWidget

from .models import (
    MODE_LABELS,
    POSITION_LABELS,
    DockPosition,
    DockTarget,
    DocumentSpec,
    LayoutSettings,
    ToolState,
    ToolWindowSpec,
    ViewMode,
    default_region_sizes,
)
from .panels import ToolPanel
from .storage import JsonLayoutStore
from .theme import Theme

MIME_TYPE = "application/x-mindmap-dock-v1"


class DockManager(QObject):
    """公共协调接口；所有方法应在 QApplication 所在主线程调用。

    工具内容与其 ToolPanel 持续保留，布局只是状态快照。保存动作只写应用专属
    配置目录；注册工厂不进入 JSON，也不执行持久化文件中的任何代码。
    """

    layout_changed = Signal()
    active_changed = Signal(str)
    theme_changed = Signal(str)
    message = Signal(str)
    tool_failed = Signal(str, str)
    tool_registered = Signal(str)
    tool_unregistered = Signal(str)
    document_closed = Signal(str)

    def __init__(self, storage_dir=None, parent=None, *, application_id="mindmap-app",
                 theme=None, layout_store=None,
                 shortcut_context=Qt.ShortcutContext.WidgetWithChildrenShortcut):
        """创建独立会话，可注入主题和存储；不会修改应用全局样式。

        application_id 是配置目录的稳定标识，不能包含路径分隔符。默认快捷键仅在
        本组件内部生效；完整工作台可以显式选择 WindowShortcut。
        """
        super().__init__(parent)
        if QApplication.instance() is None:
            raise RuntimeError("请先创建 QApplication，再创建 DockManager")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", application_id):
            raise ValueError("应用标识只能包含字母、数字、点、连字符和下划线")
        self.application_id = application_id
        self.shortcut_context = shortcut_context
        self.specs = {}
        self.states = {}
        self.panels = {}
        self.hosts = {}
        self.documents = {}
        self._last_editor = {}
        self.actions = {}
        self.settings = LayoutSettings()
        self.theme = theme or Theme()
        self._pending_tool_states = {}
        self.parking = QWidget()
        self.parking.setObjectName("dock_content_parking")
        self.parking.hide()
        self.active_id = ""
        self.refreshing = False
        self.closing = False
        self._dragging = False
        self.session_token = uuid4().hex
        self._default_snapshot = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self.save_session)
        # 使用项目专属配置命名空间；布局与 preferences.ini 按 application_id 隔离。
        # 显式传入 storage_dir 时完全使用宿主指定目录，不访问默认配置位置。
        root = (
            Path(storage_dir) if storage_dir is not None else
            Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation))
            / "mindmap" / application_id
        )
        self.storage_dir = root
        self.storage_file = root / "layouts-v1.json"
        self.layout_store = layout_store or JsonLayoutStore(self.storage_file)
        app = QApplication.instance()
        app.focusChanged.connect(self._focus_changed)
        app.installEventFilter(self)

    def register_tool_window(self, spec: ToolWindowSpec):
        """注册元信息，不立即调用工厂；重复 id 直接报错，避免覆盖已有控件。"""
        if spec.id in self.specs:
            raise ValueError(f"工具窗口标识重复：{spec.id}")
        if spec.shortcut:
            self._check_shortcut(spec.shortcut)
        self.specs[spec.id] = spec
        peers = [state for state in self.states.values() if state.position == spec.position]
        self.states[spec.id] = ToolState(spec.id, spec.position, mode=spec.mode, available=spec.available, order=len(peers), last_position=spec.position)
        if spec.shortcut:
            self._add_action("tool:" + spec.id, spec.title, spec.shortcut, lambda: self.toggle(spec.id))
        pending = self._pending_tool_states.pop(spec.id, None)
        if pending is not None:
            self.states[spec.id] = ToolState(**pending)
            self.states[spec.id].available = spec.available
            if self.states[spec.id].host_id not in self.hosts:
                self.states[spec.id].host_id = "main"
            if self.hosts:
                self.refresh()
        self.tool_registered.emit(spec.id)
        return self.states[spec.id]

    def panel(self, tool_id):
        """取得持久面板实例；首次调用触发工厂，后续调用不重新构造业务控件。"""
        self._require_tool(tool_id)
        if tool_id not in self.panels:
            try:
                self.panels[tool_id] = ToolPanel(self, self.specs[tool_id])
            except Exception as error:
                self.tool_failed.emit(tool_id, str(error))
                raise
        return self.panels[tool_id]

    def unregister_tool_window(self, tool_id, *, keep_layout=True, dispose=True):
        """卸载工具并清理动作和外壳；可保留返回位置供插件稍后重新注册。

        默认销毁内容；dispose=False 时将隐藏、无父对象的内容返回给调用方接管。
        先解除所有容器归属，再删除包装控件，避免 Qt 连带销毁已交还的内容。
        """
        self._require_tool(tool_id)
        state = self.states[tool_id]
        if keep_layout:
            self._pending_tool_states[tool_id] = asdict(state)
        else:
            self._pending_tool_states.pop(tool_id, None)
            # 永久卸载同时清理内建模板，防止恢复默认布局时重新保留该工具的状态。
            if self._default_snapshot:
                snapshot = self._default_snapshot
                snapshot["tools"] = [item for item in snapshot["tools"] if item["id"] != tool_id]
        panel = self.panels.pop(tool_id, None)
        content = None
        if panel:
            panel.hide()
            if not dispose:
                content = panel.content
                content.hide()
                content.setParent(None)
            panel.setParent(None)
            panel.deleteLater()
        action = self.actions.pop("tool:" + tool_id, None)
        if action:
            for widget in action.associatedObjects():
                if isinstance(widget, QWidget):
                    widget.removeAction(action)
            action.deleteLater()
        del self.specs[tool_id], self.states[tool_id]
        if self.active_id == tool_id:
            self.active_id = ""
        # 临时展开容器也属于工具注册生命周期；先移除再刷新，防止延迟布局回调
        # 用已经注销的标识访问 states。内容已在上方交还或安排销毁，不会重复释放。
        for host in self.hosts.values():
            overlay = host.overlays.pop(tool_id, None)
            if overlay is not None:
                overlay.hide()
                overlay.deleteLater()
        self.refresh()
        self.tool_unregistered.emit(tool_id)
        return content

    def _check_shortcut(self, shortcut, excluding=""):
        sequence = QKeySequence(shortcut)
        if sequence.isEmpty():
            raise ValueError("快捷键无效")
        if any(key != excluding and sequence in action.shortcuts() for key, action in self.actions.items()):
            raise ValueError(f"快捷键已被占用：{shortcut}")

    def register_action(self, action_id, title, callback, *, shortcut="", icon=""):
        """注册业务动作；按钮、菜单和快捷键可以复用返回的同一个 QAction。"""
        if not action_id or action_id in self.actions:
            raise ValueError("动作标识为空或重复")
        action = self._add_action(action_id, title, shortcut, callback)
        if icon:
            action.setIcon(self.theme.icon(icon))
            action.setProperty("mindmap_icon", icon)
        return action

    def set_shortcut(self, action_id, shortcut):
        """修改或清空快捷键；冲突在变更前拒绝，不影响原来的按键绑定。"""
        if shortcut:
            self._check_shortcut(shortcut, excluding=action_id)
        self.actions[action_id].setShortcut(QKeySequence(shortcut))
        if action_id.startswith("tool:"):
            self.specs[action_id[5:]].shortcut = shortcut

    def _require_tool(self, tool_id):
        if tool_id not in self.states:
            raise KeyError(f"未知工具窗口：{tool_id}")

    def _add_action(self, key, title, shortcut, callback):
        if key in self.actions:
            return self.actions[key]
        action = QAction(title, self)
        if shortcut:
            self._check_shortcut(shortcut)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(self.shortcut_context)
        action.triggered.connect(callback)
        self.actions[key] = action
        for host in self.hosts.values():
            host.addAction(action)
        return action

    def install_window_actions(self, host):
        """给唯一主宿主关联动作；仅保留默认恢复、区域尺寸和内容页切换快捷键。"""
        definitions = [
            ("default", "恢复默认布局", "Shift+F12", self.restore_default_layout),
            ("resize_left", "向左拉伸", "Ctrl+Alt+Shift+Left", lambda: self.resize_active(-36, 0)),
            ("resize_right", "向右拉伸", "Ctrl+Alt+Shift+Right", lambda: self.resize_active(36, 0)),
            ("resize_up", "向上拉伸", "Ctrl+Alt+Shift+Up", lambda: self.resize_active(0, -36)),
            ("resize_down", "向下拉伸", "Ctrl+Alt+Shift+Down", lambda: self.resize_active(0, 36)),
            ("previous_view", "上一个内容页", "Alt+Left", lambda: self.cycle_content(-1)),
            ("next_view", "下一个内容页", "Alt+Right", lambda: self.cycle_content(1)),
        ]
        for key, title, shortcut, callback in definitions:
            self._add_action(key, title, shortcut, callback)
        for action in self.actions.values():
            host.addAction(action)

    def activate(self, tool_id, focus=True):
        """显示并激活工具窗；同一停靠格仅有一个占位面板，覆盖窗可以叠在其上。"""
        self._require_tool(tool_id)
        state = self.states[tool_id]
        if not state.available:
            return False
        # 先验证并构造内容，再改变任何布局状态。工厂抛出异常时原布局和活动项保持不变。
        self.panel(tool_id)
        if state.host_id not in self.hosts:
            state.host_id = "main"
        for other in self.states.values():
            if other.id == tool_id or not other.visible:
                continue
            if other.mode in (ViewMode.DOCK_UNPINNED, ViewMode.UNDOCK):
                other.visible = False
            if state.position != "editor" and state.mode in (ViewMode.DOCK_PINNED, ViewMode.DOCK_UNPINNED) and other.host_id == state.host_id and other.position == state.position and other.mode in (ViewMode.DOCK_PINNED, ViewMode.DOCK_UNPINNED):
                other.visible = False
        state.visible = True
        self.specs[tool_id].notification = False
        self.mark_active(tool_id)
        self.refresh()
        if focus:
            self._focus_panel(tool_id)
        return True

    def _focus_panel(self, tool_id):
        panel = self.panel(tool_id)
        window = self.hosts["main"].window()
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()
        content = panel.content.currentWidget() if isinstance(panel.content, QTabWidget) else panel.content
        content.setFocus(Qt.FocusReason.OtherFocusReason)

    def mark_active(self, tool_id):
        """仅更新活动标识和标题显隐，不迁移控件，适用于鼠标按下与焦点事件。"""
        if tool_id not in self.states:
            return
        changed = self.active_id != tool_id
        self.active_id = tool_id
        for panel in self.panels.values():
            panel.update_header()
        if changed:
            self.active_changed.emit(tool_id)

    def toggle(self, tool_id):
        """活动窗口再次点击会隐藏；已显示但不活动的窗口先取得焦点。"""
        state = self.states[tool_id]
        if state.visible and self.active_id == tool_id:
            self.hide(tool_id)
        else:
            self.activate(tool_id)

    def hide(self, tool_id):
        """隐藏单个面板并交还焦点；保留模式、内容和主窗口内返回位置。"""
        self._require_tool(tool_id)
        was_active = self.active_id == tool_id
        self.states[tool_id].visible = False
        if was_active:
            self.active_id = ""
        self.refresh()
        # 隐藏活动面板后还给最后使用的文档，不提供额外导航动作或快捷键。
        if was_active:
            self._focus_editor()

    def set_available(self, tool_id, available):
        """控制插件式工具的可用性；不可用时移除栏入口，且不继续占据停靠位置。"""
        self._require_tool(tool_id)
        self.states[tool_id].available = bool(available)
        if not available:
            self.states[tool_id].visible = False
            if self.active_id == tool_id:
                self.active_id = ""
        self.refresh()

    def move(self, tool_id, target: DockTarget):
        """原子移动：所有目标验证成功后才更改状态和 QWidget 所属容器。"""
        self._require_tool(tool_id)
        if target.host_id not in self.hosts:
            raise ValueError(f"目标框架不存在：{target.host_id}")
        if self.states[tool_id].available:
            self.panel(tool_id)
        state = self.states[tool_id]
        if state.position != "editor":
            state.last_position = state.position
        peers = sorted((s for s in self.states.values() if s.id != tool_id and s.host_id == target.host_id and s.position == target.position), key=lambda s: s.order)
        index = len(peers) if target.index < 0 else min(target.index, len(peers))
        peers.insert(index, state)
        for order, peer in enumerate(peers):
            peer.order = order
        state.host_id = target.host_id
        state.position = str(target.position)
        state.editor_group = target.group_id
        if target.position == "editor":
            group = self.hosts[target.host_id].editor.group(target.group_id)
            state.editor_group = group.group_id
            token = "tool:" + tool_id
            order = [group.tabBar().tabData(n) for n in range(group.count()) if group.tabBar().tabData(n) != token]
            order.insert(len(order) if target.index < 0 else min(target.index, len(order)), token)
            group._desired_order = order
        if target.position != "editor":
            state.last_position = str(target.position)
        state.mode = ViewMode.DOCK_PINNED
        state.visible = False
        self.activate(tool_id)

    def set_view_mode(self, tool_id, mode: ViewMode):
        """转换三种主窗口内模式；编辑器内工具先回到最后停靠位置，隐藏工具保持隐藏。"""
        self._require_tool(tool_id)
        mode = ViewMode(mode)
        state = self.states[tool_id]
        if state.position == "editor":
            state.position = state.last_position
        state.mode = mode
        if state.visible:
            state.visible = False
            self.activate(tool_id)
        else:
            self.refresh()

    def set_option(self, key, value):
        """设置已定义的布尔布局选项，并同步主窗口；未知键在变更前拒绝。"""
        if key not in {field.name for field in fields(LayoutSettings)}:
            raise KeyError(f"未知布局选项：{key}")
        setattr(self.settings, key, bool(value))
        if key == "compact":
            self.theme = self.theme.with_options(compact=self.settings.compact)
        self.refresh()
        self.theme_changed.emit(self.theme.name)

    def set_theme(self, name):
        """原地刷新局部主题，保留业务输入焦点、光标和未提交草稿。"""
        self.theme = name.with_options(compact=self.settings.compact) if isinstance(name, Theme) else self.theme.with_options(name=name, compact=self.settings.compact)
        # 主题切换不改变内容归属，不能走拆卸/重挂面板的布局刷新路径。
        # 否则真实输入框会收到 FocusOut，把业务的防抖草稿意外提交。
        for host in self.hosts.values():
            host.refresh_theme()
        for panel in self.panels.values():
            panel.refresh_theme()
        for action in self.actions.values():
            if icon := action.property("mindmap_icon"):
                action.setIcon(self.theme.icon(icon))
        self.theme_changed.emit(self.theme.name)
        self.schedule_save()

    def _detach_panels(self):
        # 迁移前统一暂存面板，保持业务控件实例及输入内容。
        for panel in self.panels.values():
            panel.hide()
            panel.setParent(self.parking)
        for host in self.hosts.values():
            host.clear_panels()

    def refresh(self):
        """批量提交状态到 Qt 容器；迁移期间屏蔽焦点回调，完成后只发一次布局信号。"""
        if self.refreshing or self.closing:
            return
        self.refreshing = True
        try:
            # 旧配置合并时可能出现同一格多个可见占位窗，统一消解。
            slots = {}
            for state in self.states.values():
                if state.visible and state.available and state.position != "editor" and state.mode in (ViewMode.DOCK_PINNED, ViewMode.DOCK_UNPINNED):
                    key = (state.host_id, state.position)
                    old = slots.get(key)
                    if old:
                        if old.id == self.active_id:
                            state.visible = False
                            continue
                        old.visible = False
                    slots[key] = state
            selection = {}
            orders = {}
            for host in self.hosts.values():
                for group in host.editor.groups:
                    selection[(host.host_id, group.group_id)] = group.tabBar().tabData(group.currentIndex()) if group.currentIndex() >= 0 else ""
                    orders[(host.host_id, group.group_id)] = [group.tabBar().tabData(n) for n in range(group.count())]
            self._detach_panels()
            for action in self.actions.values():
                if name := action.property("mindmap_icon"):
                    action.setIcon(self.theme.icon(name))
            for host in list(self.hosts.values()):
                host.refresh()
                for group in host.editor.groups:
                    order = getattr(group, "_desired_order", None)
                    if order is not None:
                        group._desired_order = None
                    else:
                        order = orders.get((host.host_id, group.group_id), [])
                    present = [group.tabBar().tabData(n) for n in range(group.count())]
                    order = [token for token in order if token in present]
                    for desired, token in enumerate(order):
                        current = next(n for n in range(group.count()) if group.tabBar().tabData(n) == token)
                        if current != desired:
                            group.tabBar().moveTab(current, desired)
                    token = getattr(group, "_desired_selection", None)
                    if token is not None:
                        group._desired_selection = None
                    else:
                        token = selection.get((host.host_id, group.group_id), "")
                        active = self.states.get(self.active_id)
                        if active and active.visible and active.host_id == host.host_id and active.position == "editor" and host.editor.group(active.editor_group) is group:
                            token = "tool:" + active.id
                    if token:
                        for index in range(group.count()):
                            if group.tabBar().tabData(index) == token:
                                group.setCurrentIndex(index)
                                break
        finally:
            self.refreshing = False
        self.layout_changed.emit()
        self.schedule_save()

    def _focus_changed(self, old, new):
        if self.closing or self.refreshing or self._dragging:
            return
        # 菜单/下拉框会暂时改变 Qt 焦点；等同一事件循环完成后依据真实归属判断。
        QTimer.singleShot(0, self._process_focus)

    def _process_focus(self):
        if self.closing or self.refreshing or self._dragging:
            return
        app = QApplication.instance()
        if app.activePopupWidget() is not None:
            return
        focus = app.focusWidget()
        if focus is None:
            return
        for document_id, doc in self.documents.items():
            page = doc["widget"]
            if focus is page or page.isAncestorOf(focus):
                for host in self.hosts.values():
                    location = host.editor.find_token(document_id)
                    if location and location[0].area.host is host:
                        self._last_editor[host.host_id] = document_id
                        break
                break
        owner_id = ""
        widget = focus
        while widget:
            if isinstance(widget, ToolPanel) and widget.manager is self:
                owner_id = widget.tool_id
                break
            widget = widget.parentWidget()
        changed = False
        for state in self.states.values():
            if not state.visible or state.id == owner_id:
                continue
            if state.mode == ViewMode.DOCK_UNPINNED:
                state.visible = False
                changed = True
            elif state.mode == ViewMode.UNDOCK and owner_id:
                state.visible = False
                changed = True
        if owner_id:
            self.mark_active(owner_id)
        elif focus:
            self.active_id = ""
            for panel in self.panels.values():
                panel.update_header()
        if changed:
            self.refresh()

    def eventFilter(self, watched, event):
        if self.closing:
            return False
        if event.type() == QEvent.Type.MouseButtonPress and not self._dragging:
            widget = watched if isinstance(watched, QWidget) else None
            while widget:
                if isinstance(widget, ToolPanel) and widget.manager is self:
                    self.mark_active(widget.tool_id)
                    break
                widget = widget.parentWidget()
        return False

    def _focus_editor(self):
        """隐藏面板后返回主窗口最后获得焦点的文档，保留光标；不把编辑器内工具标签当作文档。"""
        if self._dragging:
            return
        host = self.hosts.get("main")
        if host:
            document_id = self._last_editor.get(host.host_id)
            location = host.editor.find_token(document_id) if document_id else None
            if not location or location[0].area.host is not host:
                location = None
                for group in host.editor.groups:
                    indexes = [group.currentIndex()] + list(range(group.count()))
                    for index in indexes:
                        if index >= 0 and group.tabBar().tabData(index) in self.documents:
                            location = (group, index)
                            break
                    if location:
                        break
            host.window().raise_()
            host.window().activateWindow()
            if location:
                group, index = location
                group.setCurrentIndex(index)
                group.widget(index).setFocus(Qt.FocusReason.ShortcutFocusReason)
            else:
                host.editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.active_id = ""
        for panel in self.panels.values():
            panel.update_header()

    def resize_active(self, dx, dy):
        if self.active_id not in self.states:
            return
        state = self.states[self.active_id]
        host = self.hosts[state.host_id]
        if state.mode == ViewMode.UNDOCK:
            region = state.position.split("_")[0]
            change = -dy if region == "bottom" else -dx if region == "right" else dx
            if change:
                overlay = host.overlays.get(state.id)
                current = host.region_sizes["overlay"][region]
                if overlay is not None and overlay.isVisible():
                    current = overlay.height() if region == "bottom" else overlay.width()
                host._set_region_extent(state.position, current + change, overlay=True)
        elif state.position != "editor":
            region = state.position.split("_")[0]
            change = -dy if region == "bottom" else -dx if region == "right" else dx
            if change:
                widget = getattr(host, region)
                current = widget.height() if region == "bottom" else widget.width()
                center = host.vertical.widget(0) if region == "bottom" else host.horizontal.widget(1)
                room = center.height() - 140 if region == "bottom" else center.width() - 220
                host._set_region_extent(state.position, current + min(change, max(0, room)))
        self.schedule_save()

    def cycle_content(self, direction):
        if self.active_id in self.panels:
            self.panels[self.active_id].cycle_view(direction)

    def tool_menu(self, tool_id, parent=None):
        state = self.states[tool_id]
        menu = QMenu(parent or self.hosts[state.host_id])
        menu.addAction("隐藏", lambda: self.hide(tool_id))
        modes = menu.addMenu("视图模式")
        mode_group = QActionGroup(modes)
        mode_group.setExclusive(True)
        for mode in ViewMode:
            action = modes.addAction(MODE_LABELS[mode])
            action.setCheckable(True)
            action.setChecked(state.mode == mode and state.position != "editor")
            mode_group.addAction(action)
            action.triggered.connect(lambda checked=False, selected=mode: self.set_view_mode(tool_id, selected))
        positions = menu.addMenu("移至")
        for position in DockPosition:
            action = positions.addAction(POSITION_LABELS[position])
            action.triggered.connect(lambda checked=False, selected=position: self.move(tool_id, DockTarget(selected, state.host_id)))
        positions.addSeparator()
        positions.addAction("编辑器标签", lambda: self.move(tool_id, DockTarget("editor", state.host_id)))
        menu.addSeparator()
        grouped = menu.addAction("分组标签")
        grouped.setCheckable(True)
        grouped.setChecked(state.grouped_tabs)
        grouped.setEnabled(isinstance(self.panel(tool_id).content, QTabWidget))
        grouped.triggered.connect(lambda checked: self.set_grouped_tabs(tool_id, checked))
        return menu

    def set_grouped_tabs(self, tool_id, grouped):
        """切换内容页的标签/下拉呈现；页面对象与当前页面不会因此重新创建。"""
        self.states[tool_id].grouped_tabs = bool(grouped)
        self.panel(tool_id).update_header()
        self.schedule_save()

    def encode_drag(self, kind, item_id):
        mime = QMimeData()
        mime.setData(MIME_TYPE, json.dumps({"session": self.session_token, "kind": kind, "id": item_id}).encode())
        return mime

    def decode_drag(self, mime):
        if not mime.hasFormat(MIME_TYPE):
            return None
        try:
            data = json.loads(bytes(mime.data(MIME_TYPE)))
            if data.get("session") != self.session_token:
                return None
            if data.get("kind") == "tool" and data.get("id") in self.states:
                return data
            if data.get("kind") == "document" and data.get("id") in self.documents:
                return data
        except (ValueError, TypeError, AttributeError):
            return None
        return None

    def start_tool_drag(self, tool_id, source, hotspot):
        self._start_drag("tool", tool_id, source, hotspot)

    def start_document_drag(self, token, source, hotspot):
        if token.startswith("tool:"):
            self._start_drag("tool", token[5:], source, hotspot)
        else:
            self._start_drag("document", token, source, hotspot)

    def _start_drag(self, kind, item_id, source, hotspot):
        if self._dragging:
            return
        self._dragging = True
        drag = QDrag(source)
        drag.setMimeData(self.encode_drag(kind, item_id))
        drag.setPixmap(source.grab())
        drag.setHotSpot(hotspot)
        try:
            # 只有主窗口内合法落点才提交移动；拖出窗口或按 Escape 均保持原状。
            # 不再把 IgnoreAction 解释成创建浮窗或辅助工作区的请求。
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._dragging = False
            for host in self.hosts.values():
                host.preview.hide()
                # Windows 原生拖放取消时不一定补发 dragLeaveEvent；统一释放两侧临时落点。
                host.left_rail.reset_drag()
                host.right_rail.reset_drag()
            drag.deleteLater()

    def add_document(self, document_id, title, widget, host_id="main", icon="document", *,
                     can_close=None, on_closed=None, modified=False):
        """注册已有文档控件并显示；标识必须唯一，文档内容不进入布局配置。"""
        if document_id in self.documents or not document_id or document_id.startswith("tool:"):
            raise ValueError(f"文档标识重复：{document_id}")
        if host_id not in self.hosts or not isinstance(widget, QWidget):
            raise ValueError("文档内容或目标宿主无效")
        self.documents[document_id] = {"title": title, "widget": widget, "icon": icon,
                                       "can_close": can_close, "on_closed": on_closed,
                                       "modified": bool(modified), "closed": False, "host_id": host_id}
        self.move_document(document_id, host_id)
        self.set_document_modified(document_id, modified)

    def register_document(self, spec: DocumentSpec, host_id="main"):
        """通过声明式规格注入业务文档；工厂失败时不注册半成品。"""
        if spec.id in self.documents or host_id not in self.hosts:
            raise ValueError("文档重复或宿主不存在")
        widget = spec.factory()
        self.add_document(spec.id, spec.title, widget, host_id, spec.icon,
                          can_close=spec.can_close, on_closed=spec.on_closed, modified=spec.modified)
        return widget

    def set_document_title(self, document_id: str, title: str) -> None:
        """更新已注册文档的显示标题，并保留修改标记和控件实例。

        document_id 不存在时抛出 KeyError；title 必须是非空字符串，否则抛出
        ValueError。关闭后保留的文档也能改名，重新打开时使用新标题。该方法
        只更新元信息，不修改业务数据、文档归属或布局中的稳定标识。
        """
        if not isinstance(title, str) or not title.strip():
            raise ValueError("文档标题不能为空")
        doc = self.documents[document_id]
        doc["title"] = title
        self.set_document_modified(document_id, doc["modified"])

    def set_document_actions(self, document_id: str, actions: Iterable[QAction]) -> None:
        """设置当前文档在标签栏右侧显示的图标动作，传入空序列可清除。

        document_id 必须已注册，否则抛出 KeyError；actions 按迭代顺序展示，
        含非 QAction 对象时抛出 TypeError，原有配置保持不变。动作由调用方
        持有和管理生命周期，必须在文档注销或清除这些入口前保持有效；框架
        不改变其父对象、不复制动作，也不另外配置快捷键。

        按钮直接跟随原动作的图标、提示和启用状态。每个编辑组仅展示当前
        文档的动作，切换标签、分屏、组内迁移、关闭后重开及布局恢复时
        自动同步；隐藏文档的配置仍然保留。此接口仅限 Qt GUI 线程调用，
        不修改文档内容、修改标记或布局快照，也不取得动作的所有权。
        """
        doc = self.documents[document_id]
        values = tuple(actions)
        if any(not isinstance(action, QAction) for action in values):
            raise TypeError("文档操作必须全部为 QAction")
        doc["header_actions"] = values
        for host in self.hosts.values():
            for group in host.editor.groups:
                if group.currentWidget() is doc["widget"]:
                    group._sync_document_actions()

    def set_document_modified(self, document_id, modified=True):
        """设置业务修改标记并更新所有框架中的标签；状态不包含文档实际内容。"""
        doc = self.documents[document_id]
        doc["modified"] = bool(modified)
        for host in self.hosts.values():
            location = host.editor.find_token(document_id)
            if location:
                location[0].setTabText(location[1], doc["title"] + (" *" if modified else ""))

    def can_close_document(self, document_id):
        """业务关闭决策由回调负责；无决策的已修改文档默认拒绝关闭。"""
        doc = self.documents[document_id]
        callback = doc["can_close"]
        try:
            allowed = bool(callback()) if callback else not doc["modified"]
        except Exception as error:
            self.message.emit(f"文档关闭检查失败：{error}")
            return False
        if not allowed:
            self.message.emit(f"关闭已取消：{doc['title']}")
        return allowed

    def can_close_documents(self):
        """主窗口退出前检查所有仍打开的业务文档；任一拒绝即保留整个工作台。"""
        return all(self.can_close_document(key) for key, doc in list(self.documents.items()) if not doc["closed"])

    def close_document(self, document_id, *, dispose=False):
        """关闭标签并保留内容，或显式销毁并注销。取消时不修改容器及父对象。"""
        doc = self.documents[document_id]
        if not doc["closed"] and not self.can_close_document(document_id):
            return False
        for host in self.hosts.values():
            location = host.editor.find_token(document_id)
            if location:
                location[0].removeTab(location[1])
                break
        doc["widget"].hide()
        doc["widget"].setParent(self.parking)
        first_close = not doc["closed"]
        doc["closed"] = True
        if first_close:
            if doc["on_closed"]:
                try:
                    doc["on_closed"]()
                except Exception as error:
                    self.message.emit(f"文档关闭通知失败：{error}")
            self.document_closed.emit(document_id)
        if dispose:
            doc["widget"].deleteLater()
            del self.documents[document_id]
        self.schedule_save()
        return True

    def open_document(self, document_id, host_id="main"):
        """重新打开已关闭的文档，继续使用同一内容实例和业务修改状态。"""
        self.move_document(document_id, host_id)

    def move_document(self, document_id, host_id, group_id="", index=-1):
        """将同一文档实例移到目标编辑组；index=-1 追加，未知框架或文档报错。"""
        if host_id not in self.hosts or document_id not in self.documents:
            raise ValueError("文档或目标框架不存在")
        doc = self.documents[document_id]
        doc["closed"] = False
        doc["host_id"] = host_id
        self.hosts[host_id].editor.group(group_id).add_token(document_id, doc["widget"], doc["title"], self.theme.icon(doc["icon"], 16), index)
        self.set_document_modified(document_id, doc["modified"])
        self.schedule_save()

    def capture_layout(self):
        """生成纯数据快照，仅包含主窗口六区、编辑器完整标签顺序和面板状态。"""
        return {
            "schema_version": 1,
            "theme": self.theme.name,
            "settings": asdict(self.settings),
            "tools": [asdict(state) for state in self.states.values()] + [copy.deepcopy(item) for key, item in self._pending_tool_states.items() if key not in self.states],
            "active": self.active_id,
            "hosts": {host_id: {"sizes": copy.deepcopy(host._sizes),
                                "region_sizes": copy.deepcopy(host.region_sizes),
                                "editor": host.editor.snapshot(),
                                "current_group": self._current_editor_group(host),
                                "geometry": [host.window().x(), host.window().y(), host.window().width(), host.window().height()]}
                      for host_id, host in self.hosts.items()},
        }

    def _current_editor_group(self, host):
        """记录最后使用文档所在组，供旧多窗口会话合并选择稳定的标签接收位置。"""
        location = host.editor.find_token(self._last_editor.get("main", ""))
        if location:
            return location[0].group_id
        state = self.states.get(self.active_id)
        return state.editor_group if state and state.position == "editor" else host.editor.groups[0].group_id

    def set_default_layout(self):
        """由宿主在初始注册和排布完成后调用，独立保存内建默认快照。

        默认模板仅存在内存中，不从会话文件读取，也不随用户调整自动覆盖。
        """
        self._default_snapshot = copy.deepcopy(self.capture_layout())

    def restore_default_layout(self):
        """恢复宿主建立的默认模板；尚未建立模板时不改变当前状态。

        复用快照恢复的预检、迁移和延迟保存流程，模板自身不会被后续修改污染。
        """
        if self._default_snapshot is not None:
            self.restore_layout(self._default_snapshot)

    def restore_layout(self, layout: dict):
        """恢复版本化快照字典；不再接受布局名称，非法输入抛出 ValueError。

        深拷贝并完整预检后才迁移 QWidget，既不修改调用方快照，也避免无效数据
        破坏当前控件归属；恢复成功后继续通过会话自动保存记录当前排布。
        """
        data = copy.deepcopy(layout)
        self._validate_snapshot(data)
        data = self._normalize_snapshot(data)
        self._restore_snapshot(data)
        self.schedule_save()

    def _validate_snapshot(self, data):
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("tools"), list) or not isinstance(data.get("hosts"), dict) or "main" not in data["hosts"]:
            raise ValueError("不支持或损坏的布局格式")
        if data.get("theme") not in ("light", "dark"):
            raise ValueError("布局主题无效")
        if not isinstance(data.get("active", ""), str):
            raise ValueError("活动工具标识无效")
        if not isinstance(data.get("settings", {}), dict) or not all(isinstance(value, bool) for value in data.get("settings", {}).values()):
            raise ValueError("布局选项无效")
        if len(data["hosts"]) > 32 or len(data["tools"]) > 1000:
            raise ValueError("布局内容过大")
        seen = set()
        for item in data["tools"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("id") in seen:
                raise ValueError("工具窗口状态无效或标识重复")
            seen.add(item.get("id"))
            if item.get("position") != "editor":
                DockPosition(item.get("position"))
            DockPosition(item.get("last_position", DockPosition.LEFT_TOP))
            # 仅持久化读取接受旧模式；公开接口继续通过 ViewMode 严格拒绝废弃值。
            if item.get("mode") not in {mode.value for mode in ViewMode} | {"float", "window"}:
                raise ValueError("工具窗口显示模式无效")
            if item.get("host_id") not in data["hosts"]:
                raise ValueError("布局引用不存在的框架")
            # 新快照不再保存面板几何；旧字段若存在仍先校验，避免坏数据部分恢复。
            geometry = item.get("geometry")
            if "geometry" in item and (not isinstance(geometry, list) or len(geometry) != 4 or not all(isinstance(value, int) and abs(value) <= 1000000 for value in geometry) or min(geometry[2:]) <= 0):
                raise ValueError("旧浮窗尺寸无效")
            if not isinstance(item.get("order"), int) or not isinstance(item.get("extent", 300), int):
                raise ValueError("窗口顺序或尺寸无效")
            if not all(isinstance(item.get(key, False), bool) for key in ("visible", "available", "grouped_tabs")) or not isinstance(item.get("editor_group", ""), str):
                raise ValueError("工具窗口的可见性或分组无效")
        def validate_editor(node, depth=0):
            if not isinstance(node, dict) or depth > 12:
                raise ValueError("编辑器分屏树无效")
            if node.get("kind") == "tabs":
                if not isinstance(node.get("tokens"), list) or not all(isinstance(token, str) for token in node["tokens"]):
                    raise ValueError("编辑器标签无效")
                if not isinstance(node.get("current", 0), int) or not isinstance(node.get("id"), str):
                    raise ValueError("编辑器当前页或标识无效")
            elif node.get("kind") == "split":
                if not isinstance(node.get("children"), list) or len(node["children"]) > 16:
                    raise ValueError("编辑器分屏过多")
                sizes = node.get("sizes", [])
                if not isinstance(sizes, list) or not all(isinstance(value, int) and 0 <= value <= 1000000 for value in sizes):
                    raise ValueError("编辑器比例无效")
                for child in node["children"]:
                    validate_editor(child, depth + 1)
            else:
                raise ValueError("编辑器容器类型无效")
        for host in data["hosts"].values():
            if not isinstance(host, dict):
                raise ValueError("框架状态无效")
            geometry = host.get("geometry", [0, 0, 1080, 760])
            if not isinstance(geometry, list) or len(geometry) != 4 or not all(isinstance(value, int) and abs(value) <= 1000000 for value in geometry) or min(geometry[2:]) <= 0:
                raise ValueError("框架几何无效")
            if not isinstance(host.get("sizes", {}), dict):
                raise ValueError("框架比例无效")
            for values in host.get("sizes", {}).values():
                if not isinstance(values, list) or not all(isinstance(value, int) and 0 <= value <= 1000000 for value in values):
                    raise ValueError("框架比例无效")
            region_sizes = host.get("region_sizes", {})
            if not isinstance(region_sizes, dict) or region_sizes.keys() - {"dock", "overlay"}:
                raise ValueError("区域尺寸类别无效")
            for values in region_sizes.values():
                if not isinstance(values, dict) or values.keys() - {"left", "right", "bottom"}:
                    raise ValueError("区域尺寸位置无效")
                if any(type(value) is not int or not 1 <= value <= 1000000 for value in values.values()):
                    raise ValueError("区域尺寸必须为有效的正整数")
            if "current_group" in host and not isinstance(host["current_group"], str):
                raise ValueError("编辑器当前组标识无效")
            validate_editor(host.get("editor"))

    def _normalize_snapshot(self, data):
        """将已预检的版本 1 快照迁移为区域尺寸模型，始终返回独立副本。

        新尺寸字段允许部分缺失。旧宿主的非零外部尺寸优先，工具尺寸仅用于一次性
        回填；活动、可见工具优先，同优先级沿用保存顺序。迁移不执行工厂或改变界面。
        """
        data = copy.deepcopy(data)

        def valid(value):
            return type(value) is int and 1 <= value <= 1000000

        for host_id, host in data["hosts"].items():
            region_sizes = default_region_sizes()
            peers = sorted((item for item in data["tools"] if item["host_id"] == host_id),
                           key=lambda item: (item["id"] != data.get("active"), not item.get("visible", False)))
            old_sizes = host.get("sizes", {})
            for region, splitter, index in [("left", "horizontal", 0), ("right", "horizontal", 2),
                                             ("bottom", "vertical", 1)]:
                old_values = old_sizes.get(splitter, [])
                previous = old_values[index] if index < len(old_values) else 0
                extent = next((item["extent"] for item in peers if valid(item.get("extent"))
                               and (item.get("last_position", "left_top") if item["position"] == "editor"
                                    else item["position"]).startswith(region + "_")), None)
                if valid(previous):
                    region_sizes["dock"][region] = previous
                elif extent is not None:
                    region_sizes["dock"][region] = extent
                if extent is not None:
                    region_sizes["overlay"][region] = extent
            for kind, values in host.get("region_sizes", {}).items():
                region_sizes[kind].update(values)
            host["region_sizes"] = region_sizes
        data.get("settings", {}).pop("remember_size", None)
        # 旧布局允许保留已移除的名称开关；迁移后不再应用或写回该字段。
        data.get("settings", {}).pop("show_names", None)
        self._merge_legacy_hosts(data)
        for item in data["tools"]:
            item.pop("extent", None)
            item.pop("geometry", None)
        return data

    def _merge_legacy_hosts(self, data):
        """将已校验的旧多宿主快照合并为单主窗口，不创建控件或修改运行时状态。

        保留主分屏树及各组当前标签；辅助宿主按文件顺序追加到主窗口记录的当前组。
        旧文件未记录当前组时，优先使用活动工具标签所属组，否则使用第一个标签组。
        所有标签全局去重，未注册工具也在纯数据层完成模式和归属迁移。
        """
        main = data["hosts"]["main"]

        def leaves(node):
            if node["kind"] == "tabs":
                yield node
            else:
                for child in node["children"]:
                    yield from leaves(child)

        groups = list(leaves(main["editor"]))
        if not groups:
            # 合法的空分屏树也必须有可接收标签的组；标识固定且只存在于本快照中。
            group = {"kind": "tabs", "id": "main-restored", "tokens": [], "current": 0}
            main["editor"] = group
            groups = [group]
        active = next((item for item in data["tools"] if item["id"] == data.get("active")
                       and item["host_id"] == "main" and item["position"] == "editor"), None)
        target_id = main.get("current_group", active.get("editor_group") if active else None)
        target = next((group for group in groups if group["id"] == target_id), groups[0])
        seen = set()
        locations = {}

        def append_tokens(group, tokens):
            for token in tokens:
                if token not in seen:
                    seen.add(token)
                    group["tokens"].append(token)
                    locations[token] = group["id"]

        # 去重后重新查找当前标签索引，避免前面的重复项移除导致选中项改变。
        for group in groups:
            tokens, current = group["tokens"], group.get("current", 0)
            selected = tokens[current] if 0 <= current < len(tokens) else None
            group["tokens"] = []
            append_tokens(group, tokens)
            group["current"] = group["tokens"].index(selected) if selected in group["tokens"] else 0
        for host_id, host in data["hosts"].items():
            if host_id != "main":
                for group in leaves(host["editor"]):
                    append_tokens(target, group["tokens"])

        migrated = []
        occupied = set()
        for item in data["tools"]:
            old_host, old_mode = item["host_id"], item["mode"]
            item["host_id"] = "main"
            if old_mode in ("float", "window"):
                item["mode"] = ViewMode.DOCK_PINNED.value
                if item["position"] != "editor":
                    item["position"] = item.get("last_position", item["position"])
            if item["position"] == "editor":
                token = "tool:" + item["id"]
                if token not in locations:
                    append_tokens(target, [token])
                item["editor_group"] = locations[token]
                group = next(group for group in groups if group["id"] == item["editor_group"])
                item["order"] = group["tokens"].index(token)
            elif item.get("visible") and item.get("available", True):
                if old_host != "main" or old_mode in ("float", "window"):
                    migrated.append(item)
                elif item["mode"] != ViewMode.UNDOCK:
                    occupied.add(item["position"])
        # 合并后同格不能同时显示多个停靠面板；优先原位置，再使用同边空位和其他空位。
        # 原主窗口的位置优先，避免辅助窗口面板挤走用户原本正在使用的面板。
        for item in migrated:
            if item["mode"] == ViewMode.UNDOCK:
                continue
            position = item["position"]
            candidates = sorted(DockPosition, key=lambda value: (value != position,
                                value.split("_")[0] != position.split("_")[0]))
            free = next((value.value for value in candidates if value not in occupied), None)
            if free is None:
                # 六个位置都已占用时追加为编辑器标签，不能通过隐藏来丢失原可见面板。
                token = "tool:" + item["id"]
                append_tokens(target, [token])
                item.update(position="editor", mode=ViewMode.DOCK_PINNED.value,
                            editor_group=target["id"], order=target["tokens"].index(token))
            else:
                item["position"] = item["last_position"] = free
                occupied.add(free)
        data["hosts"] = {"main": main}

    def _restore_snapshot(self, data):
        # 恢复前先创建即将显示的已注册工具；业务工厂失败时尚未拆卸任何现有容器。
        for item in data["tools"]:
            if item["id"] in self.specs and item.get("visible") and item.get("available", True):
                self.panel(item["id"])
        self.refreshing = True
        try:
            self._detach_panels()
            for doc in self.documents.values():
                doc["widget"].hide()
                doc["widget"].setParent(self.parking)
            self.settings = LayoutSettings(**{key: bool(value) for key, value in data.get("settings", {}).items() if key in {field.name for field in fields(LayoutSettings)}})
            self.theme = self.theme.with_options(name=data["theme"], compact=self.settings.compact)
            for host_id, host_data in data["hosts"].items():
                host = self.hosts[host_id]
                host.region_sizes = copy.deepcopy(host_data["region_sizes"])
                sizes = host_data.get("sizes", {})
                for key, values in sizes.items():
                    if key in host._sizes and isinstance(values, list) and all(isinstance(v, int) and v >= 0 for v in values):
                        host._sizes[key] = values
                host.editor.restore_structure(host_data["editor"])
            valid_fields = {field.name for field in fields(ToolState)}
            self._pending_tool_states = {}
            for state in self.states.values():
                state.visible = False
                state.host_id = "main"
            for item in data["tools"]:
                if item["id"] in self.states:
                    self.states[item["id"]] = ToolState(**{key: value for key, value in item.items() if key in valid_fields})
                else:
                    self._pending_tool_states[item["id"]] = {key: value for key, value in item.items() if key in valid_fields}
            for host in self.hosts.values():
                host.editor.restore_documents()
            for document_id, doc in self.documents.items():
                doc["host_id"] = "main"
                doc["closed"] = not any(host.editor.find_token(document_id) for host in self.hosts.values())
                self.set_document_modified(document_id, doc["modified"])
            self.active_id = data.get("active", "") if data.get("active") in self.states else ""
        finally:
            self.refreshing = False
        self.refresh()
        self.theme_changed.emit(self.theme.name)

    def schedule_save(self):
        if not self.closing and not self.refreshing:
            self._save_timer.start()

    def save_session(self):
        """仅保存当前会话快照；默认 JSON 存储采用原子替换。

        保留版本 1 的会话外层及快照格式，不再写入命名布局集合或当前布局名称。
        读取旧配置后的下一次正常保存也会自然清除这些废弃字段。
        """
        if self.closing or not self.hosts:
            return
        payload = {"schema_version": 1, "session": self.capture_layout()}
        try:
            self.layout_store.save(payload)
        except OSError as error:
            self.message.emit(f"布局未能保存：{error}")

    def load_session(self):
        """加载保存的会话并返回成功标记；损坏配置回到内建模板，不执行配置中的代码。"""
        try:
            payload = self.layout_store.load()
            if payload is None:
                return False
            if payload.get("schema_version") != 1:
                raise ValueError("不支持的会话版本")
            # 旧文件中的命名布局字段已经废弃；只校验实际会话，避免废弃数据损坏
            # 导致仍然有效的窗口排布、主题和尺寸无法恢复。
            self._validate_snapshot(payload["session"])
            self._restore_snapshot(self._normalize_snapshot(payload["session"]))
            return True
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            if self._default_snapshot:
                self._restore_snapshot(copy.deepcopy(self._default_snapshot))
            self.message.emit(f"布局文件无效，已恢复默认布局：{error}")
            return False

    def shutdown(self):
        """应用退出前调用：先保存，再断开全局事件与计时器，最后释放隐藏内容。"""
        if self.closing:
            return
        self.save_session()
        self.closing = True
        self._save_timer.stop()
        app = QApplication.instance()
        app.removeEventFilter(self)
        app.focusChanged.disconnect(self._focus_changed)
        self.parking.deleteLater()
