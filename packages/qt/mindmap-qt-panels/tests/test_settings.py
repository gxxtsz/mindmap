"""全局设置通过独立宿主协议编辑，组件测试不需要交互画布。"""

from mindmap_core import Theme
from mindmap_qt_panels import MindMapPanels


def test_global_settings_use_host_transactions_and_preserve_wheel_preference(panel_host_factory):
    host = panel_host_factory()
    child = host.engine.add_child(host.engine.root_id, "子主题")
    host.engine.replace_document(host.engine.document)
    panels = MindMapPanels(host)
    try:
        settings = panels.settings
        original = host.engine.document
        for field, value in [(settings.direction, "down"), (settings.branch, "arrow"), (settings.theme, "dark")]:
            index = field.findData(value)
            field.setCurrentIndex(index)
            field.activated.emit(index)
        document = host.engine.document
        assert document.layout.direction == "down" and document.theme == Theme.dark()
        assert document.nodes[child].style["branch"]["kind"] == "arrow"
        for _ in range(3):
            assert host.engine.undo()
        assert host.engine.document == original and not host.engine.can_undo
        index = settings.wheel.findData("zoom")
        settings.wheel.setCurrentIndex(index)
        settings.wheel.activated.emit(index)
        assert host.wheel_mode == "zoom" and not host.engine.can_undo
    finally:
        panels.dispose()


def test_public_popup_close_invalidates_late_action(panel_host_factory):
    host = panel_host_factory()
    panels = MindMapPanels(host)
    try:
        menu = panels.settings.create_menu(host)
        menu.popup(host.pos())
        sub = next(action.menu() for action in menu.actions()
                   if action.menu() and action.menu().objectName() == "settings_direction")
        stale = next(action for action in sub.actions() if action.data() == "down")
        before = host.engine.document
        panels.close_popups()
        stale.trigger()
        assert not menu.isVisible() and host.engine.document == before
    finally:
        panels.dispose()
