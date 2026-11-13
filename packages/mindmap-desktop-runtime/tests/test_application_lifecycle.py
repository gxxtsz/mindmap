"""直接装配导图主体的失败回滚、资源释放和桌面服务边界。"""

import pytest
from mindmap_desktop_api.qt import CommandSpec, MenuContribution, MenuLocation, PanelSpec, StatusItemSpec
from mindmap_desktop_runtime import DesktopRuntimeWindow
from PySide6.QtWidgets import QLabel


def test_runtime_requires_an_application_factory(qtbot, tmp_path):
    """没有主体工厂时不能启动空窗口，不能退回动态发现同级业务。"""
    with pytest.raises(TypeError, match="application_factory"):
        DesktopRuntimeWindow(storage_dir=tmp_path, restore=False)


def test_failed_application_rolls_back_services_and_aborts_startup(qtbot, tmp_path):
    contexts, released = [], []

    def broken(context):
        contexts.append(context)
        context.own(lambda: released.append("已清理"))
        context.commands.register(CommandSpec("broken.command", "测试动作", lambda: None))
        context.ui.register_panel(PanelSpec("broken.panel", "测试面板", QLabel, default_visible=True))
        context.ui.register_status(StatusItemSpec("broken.status", QLabel("状态")))
        context.ui.contribute_menu(MenuContribution(MenuLocation.VIEW, lambda menu: menu.addAction("不应残留")))
        raise ValueError("主体初始化失败")

    with pytest.raises(ValueError, match="主体初始化失败"):
        DesktopRuntimeWindow(storage_dir=tmp_path, restore=False, application_factory=broken)
    window = contexts[0].ui.parent
    assert released == ["已清理"] and not contexts[0]._owned
    assert "broken.command" not in window.manager.actions
    assert "broken.panel" not in window.manager.specs
    assert not window.status_items and not window.menu_contributions


def test_application_and_owned_resources_are_released_once(qtbot, tmp_path, application_factory):
    events = []

    def create(context):
        context.own(lambda: events.append("资源一"))
        context.own(lambda: events.append("资源二"))
        application = application_factory(context)
        dispose = application.dispose

        def release():
            events.append("主体")
            dispose()

        application.dispose = release
        return application

    window = DesktopRuntimeWindow(storage_dir=tmp_path, restore=False, application_factory=create)
    qtbot.addWidget(window)
    assert not hasattr(window, "plugins") and not hasattr(window.context, "editors")
    window.dispose()
    window.dispose()
    assert events == ["主体", "资源二", "资源一"]


def test_invalid_application_reports_contract_error_and_releases_resources(qtbot, tmp_path):
    """工厂返回不完整对象时，错误应指向应用协议，已登记资源仍须释放。"""
    released = []

    def invalid(context):
        context.own(lambda: released.append("已清理"))
        return object()

    with pytest.raises(TypeError, match="应用工厂必须返回完整的导图应用"):
        DesktopRuntimeWindow(storage_dir=tmp_path, restore=False, application_factory=invalid)
    assert released == ["已清理"]


def test_panel_suspension_keeps_layout_and_validates_entire_batch(window):
    """暂停导图面板保留布局偏好，批量参数有误时不改变任何面板状态。"""
    ui = window.context.ui
    action = ui.register_panel(PanelSpec("sample.panel", "测试面板", QLabel,
                                         shortcut="Ctrl+Alt+J", default_visible=True))
    assert ui.panel_visible("sample.panel") and action.isEnabled()
    with pytest.raises(KeyError):
        ui.set_panels_suspended(("sample.panel", "missing"), True)
    assert ui.panel_visible("sample.panel") and action.isEnabled()
    ui.set_panels_suspended(("sample.panel",), True)
    assert ui.panel_visible("sample.panel") and not action.isEnabled()
    assert not ui.activate_panel("sample.panel", focus=False)
    ui.set_panels_suspended(("sample.panel",), False)
    assert ui.panel_visible("sample.panel") and action.isEnabled()
    assert ui.activate_panel("sample.panel", focus=False)
