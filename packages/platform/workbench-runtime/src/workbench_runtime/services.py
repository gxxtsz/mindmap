"""按插件隔离的注册服务及 Qt 桥，资源清理由 PluginContext 统一拥有。"""

from workbench_docking import ToolWindowSpec


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
    """公开 UI 服务：允许使用公共停靠接口，业务不接触运行时的内部管理字段。"""

    def __init__(self, window, context):
        self._window, self.context = window, context

    @property
    def parent(self):
        """仅作为对话框及控件的 QWidget 父对象使用。"""
        return self._window

    @property
    def docking(self):
        """现有停靠组件的公共接口；新编辑器注册统一经过文档服务。"""
        return self._window.manager

    @property
    def restoring(self):
        """恢复屏障供插件暂停访问历史等副作用，避免把后台恢复当成用户导航。"""
        return self._window.state.restoring

    def register_panel(self, spec):
        manager = self.docking
        manager.register_tool_window(ToolWindowSpec(
            spec.id, spec.title, spec.factory, spec.icon, spec.position, spec.shortcut,
            header_actions=spec.header_actions,
        ))
        self.context.own(lambda: manager.unregister_tool_window(spec.id, keep_layout=False)
                         if spec.id in manager.specs else None)
        if spec.default_visible:
            self._window.default_panel_ids.add(spec.id)
            manager.activate(spec.id, focus=False)
        return manager.actions.get("tool:" + spec.id)

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
        self.docking.theme_changed.connect(callback)
        return self.context.own(lambda: self.docking.theme_changed.disconnect(callback))

    def subscribe_layout(self, callback):
        self.docking.layout_changed.connect(callback)
        return self.context.own(lambda: self.docking.layout_changed.disconnect(callback))

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
        return self.docking.capture_layout()


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
