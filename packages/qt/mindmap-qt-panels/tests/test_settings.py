"""全局设置通过独立宿主协议编辑，组件测试不需要交互画布。"""

import pytest
from mindmap_core import LayoutConfig, MindMapDocument, MindMapError, Theme
from mindmap_layouts import resolve_tree_spacing
from mindmap_qt_panels import MindMapPanels
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication


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


@pytest.fixture
def settings_panels(panel_host_factory):
    """真实面板与独立宿主组合，退出时主动停止防抖与顶层弹层。"""
    host = panel_host_factory()
    panels = MindMapPanels(host)
    yield host, panels
    panels.dispose()


@pytest.mark.parametrize("direction", ["both", "right", "left", "down"])
def test_axes_presets_noop_history_and_legacy_defaults(settings_panels, direction):
    host, panels = settings_panels
    host.engine.set_layout(LayoutConfig(direction=direction))
    panel = panels.settings
    before, revision = host.engine.document, host.engine.revision
    expected = (22, 64) if direction == "down" else (64, 22)
    assert (panel.horizontal.value(), panel.vertical.value()) == expected
    panel.sync()
    panel.preset_buttons[1].click()
    assert host.engine.document == before and host.engine.revision == revision
    for index in (0, 2):
        revision = host.engine.revision
        panel.preset_buttons[index].click()
        gaps = panel.SPACING_PRESETS[index][1]
        assert resolve_tree_spacing(host.engine.document.layout) == gaps
        assert host.engine.revision == revision + 1
        assert panel.preset_buttons[index].isChecked()
        assert host.engine.document.layout.compact == (index == 0)
        assert host.engine.undo()
        assert host.engine.document == before
    assert host.engine.document.layout.options == {}


def test_numbers_debounce_together_and_flush_pending_properties(settings_panels, qtbot):
    host, panels = settings_panels
    panel = panels.settings
    before, revision = host.engine.document, host.engine.revision
    panels.properties.fields["style.font_size"].setValue(22)
    panel.horizontal.setValue(83.5)
    panel.vertical.setValue(25.5)
    assert host.engine.document == before and panel.timer.isActive()
    panels.flush_pending_edits()
    assert resolve_tree_spacing(host.engine.document.layout) == (83.5, 25.5)
    assert host.engine.document.root.style["font_size"] == 22
    assert host.engine.revision == revision + 2
    assert not any(button.isChecked() for button in panel.preset_buttons)
    assert host.engine.undo() and host.engine.document.layout == before.layout
    assert host.engine.document.root.style["font_size"] == 22
    assert host.engine.undo() and host.engine.document == before
    panel.horizontal.setValue(90)
    panel.horizontal.setValue(91)
    panel.vertical.setValue(30)
    revision = host.engine.revision
    qtbot.waitUntil(lambda: host.engine.revision > revision, timeout=2000)
    assert host.engine.revision == revision + 1
    assert resolve_tree_spacing(host.engine.document.layout) == (91, 30)


def test_down_axis_edit_and_direction_change_keep_logical_spacing(settings_panels):
    host, panels = settings_panels
    panel = panels.settings
    host.engine.set_layout(LayoutConfig(direction="down"))
    panel.horizontal.setValue(15.5)
    panel.vertical.setValue(100)
    panels.flush_pending_edits()
    assert resolve_tree_spacing(host.engine.document.layout) == (100, 15.5)
    index = panel.direction.findData("left")
    panel.direction.setCurrentIndex(index)
    panel.direction.activated.emit(index)
    assert (panel.horizontal.value(), panel.vertical.value()) == (100, 15.5)
    assert resolve_tree_spacing(host.engine.document.layout) == (100, 15.5)
    # 外部文件可包含更多小数位，显示舍入不能把自定义间距误判为“紧凑”。
    host.engine.set_layout(LayoutConfig(options={"level_gap": 32.04, "sibling_gap": 10}))
    assert panel.horizontal.value() == 32
    assert not any(button.isChecked() for button in panel.preset_buttons)


@pytest.mark.parametrize("transition", ["undo", "replace", "readonly", "editing", "dispose"])
def test_external_transition_cancels_spacing_draft(settings_panels, transition, qtbot):
    host, panels = settings_panels
    panel = panels.settings
    host.engine.update_node(host.engine.root_id, text="已提交文字")
    panel.horizontal.setValue(120)
    assert panel.timer.isActive()
    if transition == "undo":
        host.engine.undo()
    elif transition == "replace":
        host.engine.replace_document(MindMapDocument.new("替换文档"))
    elif transition == "readonly":
        host.editable = False
        host.editable_changed.emit(False)
    elif transition == "editing":
        host.is_editing = True
        host.editing_changed.emit(True)
    else:
        panels.dispose()
    before, revision = host.engine.document, host.engine.revision
    qtbot.wait(450)
    assert host.engine.document == before and host.engine.revision == revision
    assert not panel.timer.isActive()


@pytest.mark.parametrize("failure", ["validation", "guard"])
def test_spacing_failure_rolls_back_document_and_fields(settings_panels, failure, monkeypatch):
    host, panels = settings_panels
    errors = []
    host.error_occurred.connect(errors.append)
    if failure == "validation":
        def reject(document):
            raise MindMapError("拒绝间距候选")
        monkeypatch.setattr(host, "validate_document", reject)
    else:
        host.engine.before_operation = lambda *_: False
    before, revision = host.engine.document, host.engine.revision
    panels.settings.horizontal.setValue(100)
    panels.flush_pending_edits()
    assert errors and host.engine.document == before and host.engine.revision == revision
    assert panels.settings.horizontal.value() == 64 and not host.engine.can_undo


def test_menu_only_contains_wheel_and_does_not_change_document(settings_panels, qtbot):
    """公开菜单直接承载滚轮选项，文档布局和外观只在侧栏中展示。"""
    host, panels = settings_panels
    menu = panels.settings.create_menu(host)
    before, revision = host.engine.document, host.engine.revision
    assert list(panels.settings.sections) == ["导图布局", "导图外观"]
    assert [action.text() for action in menu.actions()] == ["上下滚动", "缩放"]
    assert all(action.menu() is None and action.isCheckable() for action in menu.actions())
    assert menu.title() == "滚轮行为：上下滚动"
    menu.popup(host.pos())
    action = menu.actions()[1]
    qtbot.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
    assert host.wheel_mode == "zoom" and menu.title() == "滚轮行为：缩放"
    assert menu.actions()[1].isChecked() and not menu.actions()[0].isChecked()
    assert host.engine.document == before and host.engine.revision == revision


def test_spacing_composition_is_not_forced_by_save(settings_panels):
    host, panels = settings_panels
    field = panels.settings.horizontal
    before, revision = host.engine.document, host.engine.revision
    QApplication.sendEvent(field, QInputMethodEvent("输入候选", []))
    field.setValue(100)
    assert panels.is_composing
    panels.flush_pending_edits()
    assert host.engine.document == before and host.engine.revision == revision
    QApplication.sendEvent(field, QInputMethodEvent("", []))
    assert not panels.is_composing
    panels.flush_pending_edits()
    assert resolve_tree_spacing(host.engine.document.layout) == (100, 22)
    QApplication.sendEvent(field, QInputMethodEvent("旧文档候选", []))
    assert panels.is_composing
    host.engine.replace_document(MindMapDocument.new("替换输入目标"))
    assert not panels.is_composing
    assert field.value() == 64


def test_public_popup_close_invalidates_late_action(panel_host_factory):
    host = panel_host_factory()
    panels = MindMapPanels(host)
    try:
        panels.settings.direction.showPopup()
        menu = panels.settings.direction.popup_menu
        stale = next(action for action in menu.actions() if action.data() == "down")
        before = host.engine.document
        panels.close_popups()
        stale.trigger()
        assert not menu.isVisible() and host.engine.document == before
    finally:
        panels.dispose()
