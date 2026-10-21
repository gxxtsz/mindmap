"""全局设置的真实交互、整图作用范围、事务回滚与菜单生命周期验收。"""

from copy import deepcopy
from dataclasses import replace

import pytest
from mindmap_engine import (
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
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QCheckBox


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


def menu_action(menu, key, value=None):
    """按公开菜单中的分组和值找到动作，不依赖隐藏兼容表单的激活信号。"""
    if key == "compact":
        return next(action for action in menu.actions() if action.objectName() == "settings_compact")
    sub = next(action.menu() for action in menu.actions()
               if action.menu() is not None and action.menu().objectName() == "settings_" + key)
    return next(action for action in sub.actions() if action.data() == value)


def choose(field, value):
    """通过共用菜单实际提交，兼容表单仅用于定位所属控制器。"""
    panel = field.parentWidget()
    while not hasattr(panel, "create_menu"):
        panel = panel.parentWidget()
    key = next(name for name in ("wheel", "direction", "compact", "branch", "theme")
               if getattr(panel, name) is field)
    menu = panel.create_menu()
    menu.popup(QPoint(20, 20))
    QApplication.processEvents()
    action = menu_action(menu, key, value)
    if isinstance(field, QCheckBox) and action.isChecked() == value:
        menu.close()
    else:
        action.trigger()
        menu.close()
    QApplication.processEvents()
    menu.deleteLater()


def test_settings_entry_defaults_shortcut_and_minimal_toolbar(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.settings
    before, revision = canvas.engine.document, canvas.engine.revision
    assert list(panel.sections) == ["滚轮", "布局", "分支", "外观"]
    assert set(workspace.tools) == {"outline", "properties"}
    default_branch = menu_action(workspace.settings_menu, "branch", "round")
    assert default_branch.isChecked() and default_branch.parent().title() == "连接线样式：圆角正交"
    assert workspace.tools["properties"].visible and not panel.isVisible()
    assert not workspace.settings_button.icon().isNull()
    assert [action.text() for action in canvas.toolbar.actions() if not action.isSeparator()] == [
        "撤销", "重做", "适应窗口", "回到中心",
    ]
    workspace.activateWindow()
    canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(canvas.view, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
    assert workspace.settings_menu.isVisible() and workspace.tools["properties"].visible
    qtbot.keyClick(workspace.settings_menu, Qt.Key.Key_Escape)
    assert not workspace.settings_menu.isVisible()
    panel.sync()
    assert canvas.engine.document == before and canvas.engine.revision == revision
    assert not canvas.engine.is_dirty


@pytest.mark.parametrize("area", ["left", "right", "bottom"])
def test_settings_removed_and_old_preferences_ignored(workspace, area):
    before = workspace.canvas.engine.document
    saved = workspace.save_state()
    old = deepcopy(saved)
    old["tools"]["settings"] = dict(saved["tools"]["properties"], area=area, visible=True, floating=True)
    old["order"][area].insert(0, "settings")
    workspace.restore_state(old)
    assert workspace.save_state() == saved
    for method in (workspace.show_tool_window, workspace.hide_tool_window):
        with pytest.raises(MindMapError, match="工具窗口不存在"):
            method("settings")
    assert workspace.canvas.engine.document == before


@pytest.mark.parametrize("toolbar", [True, False])
def test_settings_gear_available_without_canvas_toolbar(qtbot, toolbar):
    widget = MindMapWorkspace(toolbar=toolbar)
    qtbot.addWidget(widget)
    widget.resize(1000, 700)
    widget.show()
    QApplication.processEvents()
    try:
        assert widget.settings_button.isVisible()
        assert widget.canvas.toolbar.isVisible() == toolbar
        point = widget.settings_button.mapTo(widget, QPoint())
        assert point.x() > widget.width() // 2
        assert point.y() < widget.canvas.y()
        qtbot.mouseClick(widget.settings_button, Qt.MouseButton.LeftButton)
        assert widget.settings_button.menu().isVisible()
        entry = widget.settings_button.menu().actions()[0]
        assert entry.text() == "导图设置" and entry.menu() is widget.settings_menu
        widget.settings_button.menu().close()
    finally:
        widget.dispose()


def test_wheel_keyboard_and_api_do_not_enter_history(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.settings
    before, revision = canvas.engine.document, canvas.engine.revision
    choose(panel.wheel, "zoom")
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
    before, revision = canvas.engine.document, canvas.engine.revision
    if key == "compact":
        choose(panel.compact, value)
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
    before, revision, selected = engine.document, engine.revision, canvas.selected_node_ids()
    choose(panel.branch, "round")
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
    before, revision = canvas.engine.document, canvas.engine.revision
    workspace.show_settings_menu()
    assert menu_action(workspace.settings_menu, "branch", "__mixed__").isVisible()
    qtbot.keyClick(workspace.settings_menu, Qt.Key.Key_Escape)
    assert canvas.engine.document == before and canvas.engine.revision == revision
    # 全局入口即使再次选择根节点已有的样式，也必须覆盖其他节点的局部差异。
    choose(panel.branch, "round")
    assert panel.branch.currentData() == "round"
    assert all(node.style["branch"]["kind"] == "round" for node in canvas.engine.document.nodes.values())
    choose(panel.branch, None)
    assert all("branch" not in node.style for node in canvas.engine.document.nodes.values())
    assert canvas.engine.undo() and panel.branch.currentData() == "round"


def test_global_mutation_flushes_visible_property_input_first(workspace):
    canvas, panel = workspace.canvas, workspace.settings
    workspace.show_settings_menu()
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
        choose(panel.compact, value)
    else:
        choose(getattr(panel, key), value)
    assert len(errors) == 1
    assert canvas.engine.document == before and canvas.engine.revision == revision
    assert not canvas.engine.can_undo and not canvas.engine.is_dirty
    assert panel.direction.currentData() == "right"
    assert not panel.compact.isChecked() and panel.theme.currentData() == "light"
    assert panel.branch.currentData() == "round"


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


def test_settings_popup_preserves_text_draft_and_normal_focus_still_commits(workspace, qtbot):
    """菜单临时借用焦点不提交正文，随后真正切换控件仍遵守原失焦提交规则。"""
    canvas = workspace.canvas
    canvas.begin_edit(canvas.engine.root_id)
    canvas.editor.setPlainText("菜单期间保留的草稿")
    before = canvas.engine.document
    workspace.show_settings_menu()
    QApplication.processEvents()
    assert canvas.editor is not None and canvas.engine.document == before
    assert not menu_action(workspace.settings_menu, "theme", "dark").isEnabled()
    workspace.settings_menu.close()
    QApplication.processEvents()
    QApplication.processEvents()
    assert canvas.editor is not None and canvas.engine.document == before
    workspace.activateWindow()
    canvas.view.setFocus()
    QApplication.processEvents()
    workspace.tools["outline"].button.setFocus()
    qtbot.waitUntil(lambda: canvas.editor is None)
    assert canvas.engine.document.root.text == "菜单期间保留的草稿"


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
    choose(panel.compact, True)
    assert canvas.engine.document.layout == LayoutConfig(
        algorithm_id="settings-test", direction="left", compact=True, options={"保留": 1}
    )
    assert not panel.direction.model().item(panel.direction.findData("down")).isEnabled()
    assert not menu_action(workspace.settings_menu, "direction", "down").isEnabled()
    unsupported = ConfigurableLayout()
    unsupported.info = LayoutInfo(
        "unsupported", "不支持全局布局设置", LayoutConfig(algorithm_id="unsupported")
    )
    canvas.layout_registry.register(unsupported)
    canvas.set_layout(unsupported.info.default_config)
    assert not panel.direction.isEnabled() and not panel.compact.isEnabled() and not panel.branch.isEnabled()
    assert panel.theme.isEnabled() and panel.wheel.isEnabled()
    assert "不支持" in panel.branch.toolTip()
    assert not menu_action(workspace.settings_menu, "branch", "arrow").isEnabled()
    assert "不支持" in menu_action(workspace.settings_menu, "branch", "arrow").toolTip()


def test_custom_theme_and_external_sync_do_not_write_defaults(workspace):
    canvas, panel = workspace.canvas, workspace.settings
    custom = Theme.dark()
    custom.name = "自定义深色"
    custom.accent = "#ff9933"
    canvas.engine.set_theme(custom)
    before, revision = canvas.engine.document, canvas.engine.revision
    assert panel.theme.currentData() == "custom"
    assert menu_action(workspace.settings_menu, "theme", "custom").isChecked()
    assert not menu_action(workspace.settings_menu, "theme", "custom").isEnabled()
    panel.sync()
    panel.set_theme(custom)
    workspace.settings_menu.close()
    assert canvas.engine.document == before and canvas.engine.revision == revision
    choose(panel.theme, "light")
    assert canvas.engine.document.theme == Theme()
    assert canvas.engine.undo() and canvas.engine.document.theme == custom
    canvas.engine.replace_document(MindMapDocument.new("另一份导图"))
    assert panel.theme.currentData() == "light" and panel.branch.currentData() == "round"


@pytest.mark.parametrize("transition", ["cancel", "hide", "replace", "readonly", "editing", "dispose"])
def test_stale_global_menu_cannot_commit(workspace, transition):
    canvas = workspace.canvas
    workspace.show_settings_menu()
    menu = workspace.settings_menu
    assert menu.isVisible()
    stale = menu_action(menu, "branch", "arrow")
    if transition == "cancel":
        menu.close()
        QApplication.processEvents()
    elif transition == "hide":
        workspace.hide()
    elif transition == "replace":
        canvas.engine.replace_document(MindMapDocument.new("替换后的文档"))
    elif transition == "readonly":
        canvas.set_editable(False)
    elif transition == "editing":
        canvas.begin_edit(canvas.engine.root_id)
    else:
        workspace.dispose()
    assert not menu.isVisible()
    before, revision = canvas.engine.document, canvas.engine.revision
    stale.trigger()
    assert canvas.engine.document == before and canvas.engine.revision == revision


def test_menu_values_follow_history_capabilities_and_theme(workspace):
    canvas, menu = workspace.canvas, workspace.settings_menu
    workspace.show_settings_menu()
    assert menu_action(menu, "direction", "right").isChecked()
    menu_action(menu, "direction", "left").trigger()
    assert menu_action(menu, "direction", "left").isChecked()
    canvas.engine.undo()
    assert menu_action(menu, "direction", "right").isChecked()
    canvas.engine.set_theme(Theme.dark())
    assert menu_action(menu, "theme", "dark").isChecked()
    canvas.set_editable(False)
    assert not menu_action(menu, "direction", "left").isEnabled()
    assert menu_action(menu, "wheel", "zoom").isEnabled()


def test_parent_menu_summaries_follow_changes_history_and_mixed_state(workspace):
    """上级摘要直接反映当前配置，查看、悬停及外部同步均不引入额外历史。"""
    canvas, menu = workspace.canvas, workspace.settings_menu

    def title(key):
        return menu_action(menu, key, "right" if key == "direction" else None).parent().title()

    assert title("direction") == "布局方向：向右"
    assert menu_action(menu, "compact").text() == "紧凑布局：未启用"
    choose(workspace.settings.direction, "left")
    assert title("direction") == "布局方向：向左"
    canvas.engine.undo()
    assert title("direction") == "布局方向：向右"
    canvas.set_wheel_mode("zoom")
    wheel = menu_action(menu, "wheel", "zoom")
    assert wheel.parent().title() == "滚轮行为：缩放"
    canvas.engine.set_branch_style([workspace.ids[0]], "arrow", scope="level")
    assert title("branch") == "连接线样式：多种样式"
    custom = Theme.dark()
    custom.accent = "#ff9933"
    canvas.engine.set_theme(custom)
    assert menu_action(menu, "theme", "custom").parent().title() == "导图主题：自定义"


@pytest.mark.parametrize("dark", [False, True])
def test_selected_and_hover_rows_have_distinct_pixels_and_right_checkmark(workspace, qtbot, dark):
    """采样真实菜单像素：已选中与悬停使用不同底色，右侧勾选独立于左侧图标。"""
    canvas = workspace.canvas
    canvas.engine.set_theme(Theme.dark() if dark else Theme())
    before, revision = canvas.engine.document, canvas.engine.revision
    workspace.show_settings_menu()
    selected = menu_action(workspace.settings_menu, "direction", "right")
    hovered = menu_action(workspace.settings_menu, "direction", "left")
    idle = menu_action(workspace.settings_menu, "direction", "both")
    menu = selected.parent()
    menu.popup(QPoint(250, 60))
    QApplication.processEvents()
    qtbot.mouseMove(menu, pos=menu.actionGeometry(hovered).center())
    menu.setActiveAction(hovered)
    QApplication.processEvents()
    image = menu.grab().toImage()
    ratio = image.devicePixelRatio()

    def color_at(point):
        return image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))

    def background(action):
        rect = menu.actionGeometry(action)
        return color_at(QPoint(rect.left() + 39, rect.center().y()))

    assert background(selected) == QColor("#29466c" if dark else "#dce9ff")
    assert background(hovered) == QColor("#363a42" if dark else "#f0f1f3")
    assert background(idle) == QColor("#2b2d30" if dark else "#ffffff")
    assert selected.font().bold() and not hovered.font().bold()
    rect = menu.actionGeometry(selected)
    marker = QColor("#93c5fd" if dark else "#2563eb")
    assert any(color_at(QPoint(x, y)) == marker
               for x in range(rect.right() - 27, rect.right() - 12)
               for y in range(rect.center().y() - 5, rect.center().y() + 6))
    assert selected.isChecked() and not hovered.isChecked()
    assert canvas.engine.document == before and canvas.engine.revision == revision
    # 键盘定位与鼠标悬停共用临时灰底，Enter 仍通过原生 QAction 提交选项。
    qtbot.keyClick(menu, Qt.Key.Key_Return)
    assert canvas.engine.document.layout.direction == "left"
    canvas.engine.undo()
    assert selected.isChecked() and not hovered.isChecked()


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
