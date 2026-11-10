"""按插件隔离的注册服务及 Qt 桥，资源清理由 PluginContext 统一拥有。"""

from collections.abc import Iterable

from PySide6.QtGui import QAction, QColor, QIcon
from workbench_api.qt import PanelSpec

from .ui.docking import ToolWindowSpec


class EditorRegistry:
    def __init__(self, window, context):
        self.window, self.context = window, context

    def register(self, provider):
        """注册资源提供者；冲突在改变注册表前拒绝，撤销只删除本次注册。"""
        if provider.id in self.window.providers:
            raise ValueError(f"编辑器标识重复：{provider.id}")
        used = {extension for _, item in self.window.providers.values() for extension in item.extensions}
        if used.intersection(provider.extensions):
            raise ValueError("文件扩展名已由其他编辑器提供")
        self.window.providers[provider.id] = (self.context.plugin_id, provider)
        return self.context.own(lambda: self.window.providers.pop(provider.id, None))


class CommandRegistry:
    def __init__(self, window, context):
        self.window, self.context = window, context

    def register(self, spec):
        action = self.window.manager.register_action(
            spec.id, spec.title, lambda: self.window.call(spec.callback), shortcut=spec.shortcut, icon=spec.icon)
        self.window.command_specs[spec.id] = spec

        def remove():
            self.window.command_specs.pop(spec.id, None)
            self.window.manager.unregister_action(spec.id)

        self.context.own(remove)
        return action


class UiService:
    """将插件的界面声明与操作转换为宿主调用，并在插件卸载时释放注册项。"""

    def __init__(self, window, context):
        self._window, self.context = window, context

    @property
    def parent(self):
        """仅作为对话框及控件的 QWidget 父对象使用。"""
        return self._window

    @property
    def compact(self) -> bool:
        """读取当前主题的紧凑模式，随宿主外观设置变化。"""
        return self._window.manager.theme.compact

    @property
    def restoring(self):
        """恢复屏障供插件暂停访问历史等副作用，避免把后台恢复当成用户导航。"""
        return self._window.state.restoring

    def register_panel(self, spec: PanelSpec) -> QAction | None:
        """登记面板工厂和默认布局。有快捷键时返回动作，注册项随插件清理。"""
        manager = self._window.manager
        manager.register_tool_window(ToolWindowSpec(
            spec.id, spec.title, spec.factory, spec.icon, spec.position, spec.shortcut,
            header_actions=spec.header_actions,
        ))

        def remove():
            if spec.default_visible:
                self._window.default_panel_ids.discard(spec.id)
            if spec.id in manager.specs:
                manager.unregister_tool_window(spec.id, keep_layout=False)

        self.context.own(remove)
        if spec.default_visible:
            self._window.default_panel_ids.add(spec.id)
            manager.activate(spec.id, focus=False)
        return manager.actions.get("tool:" + spec.id)

    def panel_visible(self, panel_id: str) -> bool:
        """查询已登记面板的布局显隐标志。暂时暂停面板不会改变此标志。"""
        return self._window.manager.states[panel_id].visible

    def activate_panel(self, panel_id: str, *, focus: bool = True) -> bool:
        """显示面板并沿用停靠规则。focus=False 时保留原输入焦点。"""
        return self._window.manager.activate(panel_id, focus=focus)

    def set_panels_suspended(self, panel_ids: Iterable[str], suspended: bool) -> None:
        """统一校验面板标识，再批量暂停或恢复，保留用户已有的布局偏好。"""
        self._window.manager.set_tools_suspended(panel_ids, suspended)

    def theme_color(self, token: str) -> QColor:
        """按颜色令牌查询当前主题，插件可直接用于 Qt 色板。"""
        return self._window.manager.theme.color(token)

    def theme_icon(self, name: str) -> QIcon:
        """按图标标识查询当前主题，插件可直接用于 Qt 动作和控件。"""
        return self._window.manager.theme.icon(name)

    def register_status(self, spec):
        if spec.id in self._window.status_items:
            raise ValueError("状态栏标识重复")
        self._window.status_items[spec.id] = spec
        self._window.status.addPermanentWidget(spec.widget)

        def remove():
            self._window.status_items.pop(spec.id, None)
            self._window.status.removeWidget(spec.widget)
            spec.widget.deleteLater()

        return self.context.own(remove)

    def contribute_menu(self, contribution):
        self._window.menu_contributions.append(contribution)
        return self.context.own(lambda: self._window.menu_contributions.remove(contribution))

    def subscribe_active(self, callback):
        self._window.documents.active_changed.connect(callback)
        return self.context.own(lambda: self._window.documents.active_changed.disconnect(callback))

    def subscribe_theme(self, callback):
        signal = self._window.manager.theme_changed
        signal.connect(callback)
        return self.context.own(lambda: signal.disconnect(callback))

    def subscribe_layout(self, callback):
        signal = self._window.manager.layout_changed
        signal.connect(callback)
        return self.context.own(lambda: signal.disconnect(callback))

    def show_message(self, message):
        self._window.show_status_message(message)

    def refresh(self):
        self._window.refresh_document()

    def action(self, identifier):
        return self._window.action(identifier)

    def open_directory(self, view_id=None):
        return self._window.open_file_directory(view_id)

    def remember_path(self, path):
        self._window.remember_path(path)

    def current_layout(self):
        return self._window.manager.capture_layout()


class ScopedDocuments:
    """注册操作绑定插件所有权；共享资源查找允许发现跨插件的路径冲突。"""

    def __init__(self, service, context):
        self.service, self.context = service, context

    @property
    def active(self):
        return self.service.active

    def ordered(self):
        return self.service.ordered()

    def find_path(self, path):
        return self.service.find_path(path)

    def activate_view(self, view_id: str) -> None:
        """切换到已有视图及其实际标签，沿用宿主的焦点和活动视图通知。"""
        self.service.activate_view(view_id)

    def register_view(self, provider_id, view, **kwargs):
        if self.service.window.providers[provider_id][0] != self.context.plugin_id:
            raise ValueError("插件不能以其他提供者的身份注册视图")
        registration = self.service.register_view(provider_id, view, **kwargs)
        self.context.own(registration)
        return registration

    def sync(self, view):
        self.service.sync(view)

    def remove_view(self, view_id):
        if self.service.owners.get(view_id) == self.context.plugin_id:
            self.service.remove_view(view_id)
