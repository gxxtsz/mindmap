"""全局设置的真实交互、整图作用范围、事务回滚与停靠偏好验收。"""

from copy import deepcopy
from dataclasses import replace

import pytest
from mindmap_engine import (
    BranchStyle,
    LayoutConfig,
    LayoutInfo,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Theme,
    TreeLayout,
    load_document,
    save_document,
)
from mindmap_qt import MindMapWidget, MindMapWorkspace
from mindmap_qt.property_controls import MIXED
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication


@pytest.fixture
def workspace(qtbot):
    engine = MindMapEngine()
    first = engine.add_child(engine.root_id, "分支甲")
    second = engine.add_child(engine.root_id, "分支乙")
    leaf = engine.add_child(first, "叶节点")
    engine.replace_document(engine.document)
    widget = MindMapWorkspace(engine)
    widget.ids = first, second, leaf
    qtbot.addWidget(widget)
    widget.resize(1280, 820)
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.canvas.finish_edit(commit=False)
    widget.dispose()


def choose(combo, value):
    """模拟明确激活选项；快照同步只设置索引，不应当提交操作。"""
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def test_settings_entry_defaults_shortcut_and_minimal_toolbar(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.settings
    before, revision = canvas.engine.document, canvas.engine.revision
    assert list(panel.sections) == ["滚轮", "布局", "分支", "外观"]
    assert not workspace.tools["settings"].visible
    assert workspace.tools["settings"].area == "right"
    assert workspace.tools["settings"].width == 360
    assert workspace.tools["properties"].visible
    assert not workspace.tools["settings"].action.icon().isNull()
    assert [action.text() for action in canvas.toolbar.actions() if not action.isSeparator()] == [
        "撤销",
        "重做",
        "适应窗口",
        "回到中心",
    ]
    workspace.activateWindow()
    canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(canvas.view, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
    assert workspace.tools["settings"].visible and not workspace.tools["properties"].visible
    assert panel.wheel.hasFocus()
    qtbot.mouseClick(workspace.tools["settings"].button, Qt.MouseButton.LeftButton)
    assert not workspace.tools["settings"].visible
    panel.sync()
    assert canvas.engine.document == before and canvas.engine.revision == revision
    assert not canvas.engine.is_dirty


@pytest.mark.parametrize("area", ["left", "right", "bottom"])
def test_settings_dock_float_and_old_preferences(workspace, area):
    before = workspace.canvas.engine.document
    workspace._move_tool("settings", area)
    workspace._move_tool("settings", area, floating=True, position=QPoint(999999, -999999))
    record = workspace.tools["settings"]
    assert QApplication.primaryScreen().availableGeometry().contains(record.dock.geometry())
    saved = workspace.save_state()
    record.dock.close()
    assert not record.visible
    workspace.show_tool_window("settings", focus=False)
    assert record.visible and record.floating
    workspace.restore_state(saved)
    assert record.area == area and record.floating and record.visible
    old = deepcopy(saved)
    old["tools"].pop("settings")
    for order in old["order"].values():
        if "settings" in order:
            order.remove("settings")
    workspace.restore_state(old)
    assert record.area == "right" and not record.visible and not record.floating
    assert workspace.canvas.engine.document == before


def test_wheel_keyboard_and_api_do_not_enter_history(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.settings
    workspace.show_tool_window("settings")
    before, revision = canvas.engine.document, canvas.engine.revision
    qtbot.keyClick(panel.wheel, Qt.Key.Key_Down)
    assert canvas.wheel_mode == "zoom" and canvas.action("wheel_zoom").isChecked()
    canvas.set_wheel_mode("scroll")
    assert panel.wheel.currentData() == "scroll"
    assert canvas.engine.document == before and canvas.engine.revision == revision
    assert not canvas.engine.can_undo and not canvas.engine.is_dirty
    other = MindMapWorkspace()
    qtbot.addWidget(other)
    try:
        choose(panel.wheel, "zoom")
        assert other.settings.wheel.currentData() == "scroll"
    finally:
        other.dispose()


@pytest.mark.parametrize("key,value", [("direction", "down"), ("compact", True), ("theme", "dark")])
def test_document_settings_history_save_and_reopen(workspace, qtbot, tmp_path, key, value):
    canvas, panel = workspace.canvas, workspace.settings
    workspace.show_tool_window("settings")
    before, revision = canvas.engine.document, canvas.engine.revision
    if key == "compact":
        qtbot.mouseClick(panel.compact, Qt.MouseButton.LeftButton, pos=QPoint(8, panel.compact.height() // 2))
    else:
        choose(getattr(panel, key), value)
    after = canvas.engine.document
    assert after != before and canvas.engine.revision == revision + 1
    assert canvas.engine.is_dirty
    if key == "theme":
        assert after.theme == Theme.dark()
    else:
        assert getattr(after.layout, key) == value
    path = tmp_path / "全局设置.mindmap"
    save_document(after, path)
    assert load_document(path) == after
    assert canvas.engine.undo() and canvas.engine.document == before and not canvas.engine.is_dirty
    assert canvas.engine.redo() and canvas.engine.document == after
    canvas.engine.replace_document(load_document(path))
    if key == "compact":
        assert panel.compact.isChecked()
    else:
        assert getattr(panel, key).currentData() == value


def test_global_branch_covers_folded_and_unfocused_nodes_and_new_nodes(workspace, qtbot):
    canvas, panel, engine = workspace.canvas, workspace.settings, workspace.canvas.engine
    first, second, leaf = workspace.ids
    engine.update_node(first, style={"font_size": 20, "扩展": {"保留": True}})
    engine.set_branch_style([second], "arrow")
    engine.set_expanded(first, False)
    engine.replace_document(engine.document)
    canvas.focus_node(second)
    canvas.select_nodes([second])
    workspace.show_tool_window("settings")
    before, revision, selected = engine.document, engine.revision, canvas.selected_node_ids()
    panel.scroll.ensureWidgetVisible(panel.branch)
    panel.branch.showPopup()
    qtbot.mouseClick(panel.branch.popup_menu.buttons[BranchStyle.ROUND], Qt.MouseButton.LeftButton)
    after = engine.document
    assert all(node.style["branch"]["kind"] == "round" for node in after.nodes.values())
    assert after.nodes[first].style["扩展"] == {"保留": True}
    assert after.nodes[first].style["font_size"] == 20 and not after.nodes[first].expanded
    assert canvas.focus_id == second and canvas.selected_node_ids() == selected
    assert engine.revision == revision + 1
    assert engine.undo() and engine.document == before and not engine.can_undo
    assert engine.redo() and engine.document == after
    created = [engine.add_child(leaf), engine.add_sibling(second), engine.insert_parent(first)]
    assert all(engine.get_node(key).style["branch"]["kind"] == "round" for key in created)


def test_local_override_mixed_reselect_reset_and_cancel(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.settings
    first, _, _ = workspace.ids
    choose(panel.branch, "round")
    canvas.select_nodes([first])
    canvas.set_branch_style("line", scope="level")
    assert panel.branch.value() is MIXED
    workspace.show_tool_window("settings")
    before, revision = canvas.engine.document, canvas.engine.revision
    panel.scroll.ensureWidgetVisible(panel.branch)
    panel.branch.showPopup()
    qtbot.keyClick(panel.branch.popup_menu, Qt.Key.Key_Escape)
    assert canvas.engine.document == before and canvas.engine.revision == revision
    # 全局入口即使再次选择根节点已有的样式，也必须覆盖其他节点的局部差异。
    choose(panel.branch, "round")
    assert panel.branch.currentData() == "round"
    assert all(node.style["branch"]["kind"] == "round" for node in canvas.engine.document.nodes.values())
    choose(panel.branch, None)
    assert all("branch" not in node.style for node in canvas.engine.document.nodes.values())
    assert canvas.engine.undo() and panel.branch.currentData() == "round"


def test_global_mutation_flushes_floating_property_input_first(workspace):
    canvas, panel = workspace.canvas, workspace.settings
    workspace._move_tool("settings", "right", floating=True)
    assert workspace.tools["properties"].visible
    original = canvas.engine.document
    workspace.properties.fields["style.font_size"].setValue(22)
    assert workspace.properties.timer.isActive()
    choose(panel.direction, "down")
    assert canvas.engine.document.layout.direction == "down"
    assert canvas.engine.document.root.style["font_size"] == 22
    assert canvas.engine.undo()
    assert canvas.engine.document.layout == original.layout
    assert canvas.engine.document.root.style["font_size"] == 22
    assert canvas.engine.undo() and canvas.engine.document == original


@pytest.mark.parametrize(
    "key,value", [("direction", "left"), ("compact", True), ("theme", "dark"), ("branch", "arrow")]
)
@pytest.mark.parametrize("failure", ["validation", "guard"])
def test_failed_global_change_restores_controls_and_document(workspace, monkeypatch, key, value, failure):
    canvas, panel = workspace.canvas, workspace.settings
    before, revision = canvas.engine.document, canvas.engine.revision
    errors = []
    canvas.error_occurred.connect(errors.append)
    if failure == "validation":

        def reject(document):
            raise MindMapError("拒绝候选布局")

        monkeypatch.setattr(canvas, "validate_document", reject)
    else:
        canvas.engine.before_operation = lambda operation, arguments: False
    if key == "compact":
        panel.compact.click()
    else:
        choose(getattr(panel, key), value)
    assert len(errors) == 1
    assert canvas.engine.document == before and canvas.engine.revision == revision
    assert not canvas.engine.can_undo and not canvas.engine.is_dirty
    assert panel.direction.currentData() == "right"
    assert not panel.compact.isChecked() and panel.theme.currentData() == "light"
    assert panel.branch.currentData() is None


def test_readonly_and_text_editing_only_allow_wheel(workspace):
    canvas, panel = workspace.canvas, workspace.settings
    fields = panel.direction, panel.compact, panel.branch, panel.theme
    for readonly in (False, True):
        if readonly:
            canvas.set_editable(False)
        else:
            canvas.begin_edit(workspace.ids[0])
        before = canvas.engine.document
        assert all(not field.isEnabled() for field in fields) and panel.wheel.isEnabled()
        choose(panel.direction, "left")
        choose(panel.branch, "arrow")
        choose(panel.theme, "dark")
        choose(panel.wheel, "zoom")
        assert canvas.wheel_mode == "zoom" and canvas.engine.document == before
        if readonly:
            canvas.set_editable(True)
        else:
            canvas.finish_edit(commit=False)
        assert all(field.isEnabled() for field in fields)


class ConfigurableLayout(TreeLayout):
    """测试扩展声明的能力及专有配置，不把界面迁移变成内置树布局的硬编码。"""

    info = LayoutInfo(
        "settings-test",
        "设置测试布局",
        LayoutConfig(algorithm_id="settings-test", options={"保留": 1}),
        directions=("right", "left"),
        supports_compact=True,
        supports_branch_styles=True,
    )

    def validate_config(self, config):
        config.validate()

    def layout(self, document, measurements=None, **kwargs):
        candidate = deepcopy(document)
        candidate.layout = replace(document.layout, algorithm_id="tree", options={})
        return TreeLayout().layout(candidate, measurements, **kwargs)


def test_extension_options_and_capabilities_survive_changes(workspace):
    canvas, panel = workspace.canvas, workspace.settings
    canvas.layout_registry.register(ConfigurableLayout())
    canvas.set_layout(ConfigurableLayout.info.default_config)
    choose(panel.direction, "left")
    panel.compact.click()
    assert canvas.engine.document.layout == LayoutConfig(
        algorithm_id="settings-test", direction="left", compact=True, options={"保留": 1}
    )
    assert not panel.direction.model().item(panel.direction.findData("down")).isEnabled()
    unsupported = ConfigurableLayout()
    unsupported.info = LayoutInfo(
        "unsupported", "不支持全局布局设置", LayoutConfig(algorithm_id="unsupported")
    )
    canvas.layout_registry.register(unsupported)
    canvas.set_layout(unsupported.info.default_config)
    assert not panel.direction.isEnabled() and not panel.compact.isEnabled() and not panel.branch.isEnabled()
    assert panel.theme.isEnabled() and panel.wheel.isEnabled()
    assert "不支持" in panel.branch.toolTip()


def test_custom_theme_and_external_sync_do_not_write_defaults(workspace):
    canvas, panel = workspace.canvas, workspace.settings
    custom = Theme.dark()
    custom.name = "自定义深色"
    custom.accent = "#ff9933"
    canvas.engine.set_theme(custom)
    before, revision = canvas.engine.document, canvas.engine.revision
    assert panel.theme.currentData() == "custom"
    panel.sync()
    panel.set_theme(custom)
    workspace.show_tool_window("settings")
    workspace.hide_tool_window("settings")
    assert canvas.engine.document == before and canvas.engine.revision == revision
    choose(panel.theme, "light")
    assert canvas.engine.document.theme == Theme()
    assert canvas.engine.undo() and canvas.engine.document.theme == custom
    canvas.engine.replace_document(MindMapDocument.new("另一份导图"))
    assert panel.theme.currentData() == "light" and panel.branch.currentData() is None


@pytest.mark.parametrize("transition", ["hide", "replace", "readonly", "editing", "dispose"])
def test_stale_global_branch_popup_cannot_commit(workspace, transition):
    canvas, panel = workspace.canvas, workspace.settings
    workspace.show_tool_window("settings")
    panel.scroll.ensureWidgetVisible(panel.branch)
    panel.branch.showPopup()
    assert panel.branch.popup_menu.isVisible()
    stale = panel.branch.popup_menu.buttons[BranchStyle.ARROW]
    if transition == "hide":
        workspace.hide_tool_window("settings")
    elif transition == "replace":
        canvas.engine.replace_document(MindMapDocument.new("替换后的文档"))
    elif transition == "readonly":
        canvas.set_editable(False)
    elif transition == "editing":
        canvas.begin_edit(canvas.engine.root_id)
    else:
        workspace.dispose()
    assert not panel.branch.popup_menu.isVisible()
    before, revision = canvas.engine.document, canvas.engine.revision
    stale.click()
    assert canvas.engine.document == before and canvas.engine.revision == revision


def test_standalone_keeps_wheel_and_branch_interfaces(qtbot):
    canvas = MindMapWidget()
    qtbot.addWidget(canvas)
    try:
        canvas.action("wheel_zoom").trigger()
        canvas.set_branch_style("round")
        assert canvas.wheel_mode == "zoom" and canvas.action("wheel_mode").menu() is canvas.wheel_menu
        assert canvas.engine.document.root.style["branch"]["kind"] == "round"
        assert canvas.action("wheel_mode") not in canvas.toolbar.actions()
    finally:
        canvas.dispose()
