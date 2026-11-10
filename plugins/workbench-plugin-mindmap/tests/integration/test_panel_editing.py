"""按真实能力归属迁移的组件与行为回归。"""


import pytest
from mindmap_core import BranchStyle, LayoutConfig, MindMapDocument, MindMapError
from mindmap_formats import load_document, save_document
from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QColor, QInputMethodEvent
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QPlainTextEdit,
    QScrollArea,
    QStyle,
    QStyleOptionSpinBox,
    QTabWidget,
)


def choose(combo, value):
    """模拟用户明确激活选项；仅刷新 currentIndex 不应当变成用户修改。"""
    index = combo.findData(value)
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def edit_line(qtbot, field, value):
    field.setFocus()
    field.selectAll()
    qtbot.keyClicks(field, value)


def test_properties_action_and_editable_notifications(workspace, qtbot):
    canvas = workspace.canvas
    canvas.set_editable(False)
    assert canvas.action("properties").isEnabled()
    workspace.hide_tool_window("properties")
    canvas.action("properties").trigger()
    assert workspace.properties.isVisible()
    assert not any(child.isVisible() for child in canvas.findChildren(QDialog))
    assert not workspace.properties.fields["style.font_family"].isEnabled()
    canvas.set_editable(True)
    assert workspace.properties.fields["style.font_family"].isEnabled()
    canvas.begin_edit(canvas.engine.root_id)
    assert not workspace.properties.fields["style.font_family"].isEnabled()
    canvas.action("properties").trigger()
    assert canvas.editor is None and workspace.properties.fields["style.font_family"].isEnabled()


def test_no_selection_and_unmodified_form_do_not_write(workspace):
    engine = workspace.canvas.engine
    before = engine.document
    workspace.properties.sync()
    workspace.show_tool_window("properties", focus=False)
    workspace.flush_pending_edits()
    assert engine.document == before and not engine.can_undo
    workspace.canvas.select_nodes([])
    assert workspace.properties.empty_state.isVisible()
    assert not workspace.properties.summary.isVisible()
    assert not workspace.properties.scroll.isVisible()


def test_debounce_cursor_and_one_undo(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    text = panel.fields["style.font_family"].lineEdit()
    text.setFocus()
    text.selectAll()
    qtbot.keyClicks(text, "Arial")
    position = text.cursorPosition()
    assert "font_family" not in engine.document.root.style
    qtbot.waitUntil(lambda: engine.document.root.style.get("font_family") == "Arial", timeout=1200)
    assert text.hasFocus() and text.cursorPosition() == position
    assert engine.undo() and "font_family" not in engine.document.root.style
    assert not engine.can_undo
    assert panel.fields["style.font_family"].value() == ""
    assert engine.redo() and text.text() == "Arial"


def test_switch_selection_flushes_to_original_node(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, _ = workspace.ids
    canvas.select_nodes([a])
    edit_line(qtbot, panel.fields["style.font_family"].lineEdit(), "Arial")
    canvas.select_nodes([b])
    assert canvas.engine.get_node(a).style["font_family"] == "Arial"
    assert "font_family" not in canvas.engine.get_node(b).style
    assert canvas.selected_node_ids() == [b]
    assert panel.fields["style.font_family"].value() == ""
    qtbot.wait(450)
    assert "font_family" not in canvas.engine.get_node(b).style


@pytest.mark.parametrize("operation", ["undo", "reset", "delete", "readonly", "dispose"])
def test_external_changes_cancel_pending(workspace, qtbot, operation):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, _ = workspace.ids
    canvas.engine.update_node(b, note="可撤销")
    canvas.select_nodes([a])
    edit_line(qtbot, panel.fields["style.font_family"].lineEdit(), "Pending")
    if operation == "undo":
        canvas.engine.undo()
    elif operation == "reset":
        canvas.engine.replace_document(MindMapDocument.new("新文档"))
    elif operation == "delete":
        canvas.engine.remove_nodes([a])
    elif operation == "readonly":
        canvas.set_editable(False)
    else:
        workspace.dispose()
    document = canvas.engine.document
    qtbot.wait(450)
    assert canvas.engine.document == document


def test_ime_preedit_is_not_committed(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    text = panel.fields["style.font_family"].lineEdit()
    text.setFocus()
    text.selectAll()
    qtbot.keyClicks(text, "SimSun")
    QApplication.sendEvent(text, QInputMethodEvent("候选", []))
    assert panel.composing
    qtbot.wait(450)
    workspace.flush_pending_edits()
    assert "font_family" not in engine.document.root.style
    commit = QInputMethodEvent()
    commit.setCommitString("中文")
    QApplication.sendEvent(text, commit)
    assert not panel.composing
    workspace.flush_pending_edits()
    assert "中文" in engine.document.root.style["font_family"]


def test_multiselect_only_changed_style_and_branch_scope(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, leaf = workspace.ids
    canvas.engine.update_node(a, style={"font_size": 12, "extension": {"参数": 1}, "background": "#ff0000"})
    canvas.engine.update_node(b, style={"font_size": 18, "italic": True, "background": "#00ff00"})
    canvas.engine.set_branch_style([leaf], "arrow")
    canvas.engine.replace_document(canvas.engine.document)
    canvas.select_nodes([a, b])
    assert "text" not in panel.fields
    assert panel.fields["style.font_size"].lineEdit().placeholderText() == "多种值"
    assert panel.fields["style.italic"].property("mixed")
    panel.scroll.ensureWidgetVisible(panel.fields["style.bold"])
    qtbot.mouseClick(panel.fields["style.bold"], Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
    assert all(canvas.engine.get_node(key).style["bold"] for key in (a, b))
    assert canvas.engine.get_node(a).style["extension"] == {"参数": 1}
    assert canvas.engine.get_node(a).style["background"] != canvas.engine.get_node(b).style["background"]
    assert canvas.engine.get_node(leaf).style["branch"]["kind"] == "arrow"
    assert canvas.engine.undo() and not canvas.engine.can_undo
    choose(panel.scope, "level")
    assert not canvas.engine.can_undo
    choose(panel.fields["branch.kind"], "round")
    assert canvas.engine.get_node(leaf).style["branch"]["kind"] == "arrow"
    assert canvas.engine.undo() and not canvas.engine.can_undo
    choose(panel.scope, "subtree")
    choose(panel.fields["branch.kind"], "arc2")
    assert all(canvas.engine.get_node(key).style["branch"]["kind"] == "arc2" for key in (a, b, leaf))
    assert canvas.engine.undo() and not canvas.engine.can_undo


def test_invalid_color_and_hook_rejection_are_atomic(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    color = panel.fields["style.background"]
    before = engine.document
    color.select_color("#zzzzzz")
    workspace.flush_pending_edits()
    assert engine.document == before and not engine.can_undo
    assert panel.errors["style.background"].text()
    color.select_color("#123456")
    workspace.flush_pending_edits()
    assert engine.document.root.style["background"] == "#123456"
    assert not panel.errors["style.background"].text()
    engine.before_operation = lambda name, params: False if name == "update_node" else True
    before = engine.document
    color.select_color("#abcdef")
    workspace.flush_pending_edits()
    assert engine.document == before
    assert panel.errors["style.background"].text()


def test_input_shortcuts_stay_in_fields_and_hiding_flushes(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    text = panel.fields["style.font_family"].lineEdit()
    text.setFocus()
    text.selectAll()
    qtbot.keyClicks(text, "abcd")
    qtbot.keyClick(text, Qt.Key.Key_Return)
    qtbot.keyClick(text, Qt.Key.Key_Tab)
    assert len(engine.document.nodes) == 4
    edit_line(qtbot, text, "Arial")
    workspace.hide_tool_window("properties")
    assert engine.document.root.style["font_family"] == "Arial"


def test_export_flushes_valid_pending_and_contains_no_workspace_state(workspace, tmp_path):
    panel, canvas = workspace.properties, workspace.canvas
    panel.fields["style.font_size"].setValue(22)
    for suffix in ("svg", "png", "pdf"):
        target = tmp_path / ("导图." + suffix)
        canvas.export(target)
        assert target.stat().st_size > 100
    assert canvas.engine.document.root.style["font_size"] == 22
    assert "根主题" in (tmp_path / "导图.svg").read_text(encoding="utf-8")
    assert canvas.engine.document.metadata == {}


def test_outline_reveal_collapsed_outside_navigation_and_undo(workspace):
    canvas, engine = workspace.canvas, workspace.canvas.engine
    a, b, leaf = workspace.ids
    engine.set_expanded(a, False)
    engine.set_expanded(engine.root_id, False)
    engine.replace_document(engine.document)
    canvas.navigate_to(None, b)
    zoom = canvas.view.transform().m11()
    workspace.panels.reveal_nodes([leaf])
    assert canvas.focus_id is None
    assert canvas.selected_node_ids() == [leaf]
    assert engine.get_node(a).expanded
    assert canvas.view.transform().m11() == zoom
    assert engine.undo() and not engine.get_node(a).expanded and not engine.can_undo


def test_outline_selection_and_view_expansion_are_independent(workspace):
    canvas, tree = workspace.canvas, workspace.outline
    a, b, leaf = workspace.ids
    engine = canvas.engine
    before = engine.document
    tree.collapse(tree.tree_model.index_for(a))
    tree.expand(tree.tree_model.index_for(a))
    assert engine.document == before
    canvas.select_nodes([a, b])
    assert {tree.tree_model.node_id(index) for index in tree.selectionModel().selectedRows()} == {a, b}
    tree.selectionModel().select(
        tree.tree_model.index_for(leaf), QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    assert canvas.selected_node_ids() == [leaf]
    engine.update_node(leaf, text="大纲更新")
    assert tree.tree_model.index_for(leaf).data() == "大纲更新"
    engine.move_nodes([leaf], b)
    assert tree.tree_model.parent(tree.tree_model.index_for(leaf)).data() == "乙"
    engine.remove_nodes([leaf])
    assert not tree.tree_model.index_for(leaf).isValid()
    engine.undo()
    assert tree.tree_model.index_for(leaf).data() == "大纲更新"


def test_readonly_outline_hidden_target_keeps_selection(workspace):
    canvas, engine = workspace.canvas, workspace.canvas.engine
    a, b, leaf = workspace.ids
    engine.set_expanded(a, False)
    canvas.select_nodes([b])
    canvas.set_editable(False)
    before = engine.document
    workspace.panels.reveal_nodes([leaf])
    assert canvas.selected_node_ids() == [b] and engine.document == before
    workspace.panels.reveal_nodes([a])
    assert canvas.selected_node_ids() == [a]


def test_disabled_field_eligibility_and_unsupported_layout(workspace):
    from mindmap_core import LayoutInfo
    from mindmap_layouts import TreeLayout

    class Custom(TreeLayout):
        info = LayoutInfo("custom", "自定义", LayoutConfig(algorithm_id="custom"))

        def layout(self, document, measurements=None, **kwargs):
            document.layout = LayoutConfig()
            return TreeLayout().layout(document, measurements, **kwargs)

    canvas, panel = workspace.canvas, workspace.properties
    a, b, leaf = workspace.ids
    canvas.select_nodes([a, b])
    assert panel.fields["direction"].isEnabled()
    assert not panel.fields["image_width"].isEnabled()
    canvas.select_nodes([a, leaf])
    assert not panel.fields["direction"].isEnabled()
    canvas.layout_registry.register(Custom())
    canvas.set_layout(LayoutConfig(algorithm_id="custom"))
    assert not panel.fields["branch.kind"].isEnabled() and not panel.scope.isEnabled()


def test_branch_scope_synchronizes_without_overwriting_font_input(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.properties
    edit_line(qtbot, panel.fields["style.font_family"].lineEdit(), "Arial")
    canvas.set_branch_scope("level")
    assert panel.scope.currentData() == "level"
    assert panel.fields["style.font_family"].lineEdit().text() == "Arial"
    workspace.flush_pending_edits()
    assert canvas.engine.document.root.style["font_family"] == "Arial"


def test_mixed_number_can_be_set_to_existing_internal_value(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, _ = workspace.ids
    canvas.engine.update_node(a, style={"font_size": 12})
    canvas.engine.update_node(b, style={"font_size": 18})
    canvas.select_nodes([a])
    canvas.select_nodes([a, b])
    number = panel.fields["style.font_size"]
    edit_line(qtbot, number.lineEdit(), "12")
    workspace.flush_pending_edits()
    assert canvas.engine.get_node(a).style["font_size"] == 12
    assert canvas.engine.get_node(b).style["font_size"] == 12


def test_candidate_geometry_failure_preserves_document_and_selection(workspace, monkeypatch):
    canvas, panel = workspace.canvas, workspace.properties
    before, selection = canvas.engine.document, canvas.selected_node_ids()

    def reject(document):
        raise MindMapError("候选几何无效")

    monkeypatch.setattr(canvas, "validate_document", reject)
    panel.fields["style.font_size"].setValue(22)
    workspace.flush_pending_edits()
    assert canvas.engine.document == before and canvas.selected_node_ids() == selection
    assert not canvas.engine.can_undo
    assert "几何" in panel.errors["style.font_size"].text()


def test_cancel_ime_resumes_pending_input(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    text = panel.fields["style.font_family"].lineEdit()
    edit_line(qtbot, text, "SimSun")
    QApplication.sendEvent(text, QInputMethodEvent("候选", []))
    qtbot.wait(450)
    assert "font_family" not in engine.document.root.style
    QApplication.sendEvent(text, QInputMethodEvent())
    qtbot.waitUntil(lambda: engine.document.root.style.get("font_family") == "SimSun", timeout=1200)


@pytest.mark.parametrize("replace", [False, True])
def test_external_delete_or_reset_during_inline_edit(workspace, replace):
    """结束编辑信号先于选择刷新时，表单不得访问已经删除的旧节点标识。"""
    canvas = workspace.canvas
    a, _, _ = workspace.ids
    canvas.begin_edit(a)
    if replace:
        canvas.engine.replace_document(MindMapDocument.new("替换文档"))
    else:
        canvas.engine.remove_nodes([a])
    assert canvas.editor is None
    assert workspace.properties._ids == canvas.selected_node_ids()
    assert workspace.properties.fields["style.font_family"].isEnabled()


def test_single_page_and_style_edit_preserve_all_node_content(workspace, qtbot, tmp_path):
    """移除内容界面只影响入口，样式提交和文件往返不能清空既有内容或扩展字段。"""
    canvas, panel = workspace.canvas, workspace.properties
    a, _, _ = workspace.ids
    canvas.engine.update_node(
        a,
        text="**正文**",
        note="保留备注",
        tags=["标签"],
        icons=["★"],
        hyperlink="https://example.com",
        text_format="markdown",
        metadata={"自定义": [1]},
        style={"extension": {"保留": True}},
    )
    canvas.engine.replace_document(canvas.engine.document)
    canvas.select_nodes([a])
    before = canvas.engine.get_node(a)
    assert panel.findChild(QTabWidget) is None
    assert panel.findChild(QPlainTextEdit) is None
    assert len(panel.findChildren(QScrollArea)) == 1
    assert list(panel.sections) == ["字体", "主题", "分支"]
    assert not {"text", "note", "tags", "icons", "hyperlink", "text_format"}.intersection(panel.fields)
    qtbot.mouseClick(panel.fields["style.bold"], Qt.MouseButton.LeftButton)
    before.style["bold"] = True
    assert canvas.engine.get_node(a) == before
    path = tmp_path / "样式及原内容.mindmap"
    save_document(canvas.engine.document, path)
    assert load_document(path) == canvas.engine.document
    assert canvas.engine.undo() and not canvas.engine.can_undo
    assert canvas.engine.get_node(a).note == "保留备注"


def test_font_steps_are_immediate_and_respect_limits(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    assert panel.fields["style.font_size"].value() == 13
    qtbot.mouseClick(panel.increase_size, Qt.MouseButton.LeftButton)
    assert engine.document.root.style["font_size"] == 14
    assert engine.undo() and not engine.can_undo
    qtbot.mouseClick(panel.decrease_size, Qt.MouseButton.LeftButton)
    assert engine.document.root.style["font_size"] == 12
    for size, button in ((96, panel.increase_size), (6, panel.decrease_size)):
        panel.fields["style.font_size"].setValue(size)
        workspace.flush_pending_edits()
        assert not button.isEnabled()
        assert engine.document.root.style["font_size"] == size


def test_number_vertical_buttons_keep_native_step_behavior(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    field = panel.fields["style.font_size"]
    option = QStyleOptionSpinBox()
    field.initStyleOption(option)
    for control, expected in ((QStyle.SubControl.SC_SpinBoxUp, 14), (QStyle.SubControl.SC_SpinBoxDown, 13)):
        rect = field.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, field)
        assert rect.width() > 0 and rect.height() > 0
        qtbot.mouseClick(field, Qt.MouseButton.LeftButton, pos=rect.center())
        workspace.flush_pending_edits()
        assert engine.document.root.style["font_size"] == expected


def test_mixed_font_controls_only_apply_explicit_values(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, _ = workspace.ids
    canvas.engine.update_node(a, style={"font_size": 12, "italic": True})
    canvas.engine.update_node(b, style={"font_size": 18})
    canvas.engine.replace_document(canvas.engine.document)
    canvas.select_nodes([a, b])
    assert not panel.increase_size.isEnabled() and not panel.decrease_size.isEnabled()
    assert panel.fields["style.italic"].property("mixed")
    qtbot.mouseClick(panel.fields["style.italic"], Qt.MouseButton.LeftButton)
    assert all(canvas.engine.get_node(key).style["italic"] for key in (a, b))
    assert canvas.engine.undo() and not canvas.engine.can_undo
    edit_line(qtbot, panel.fields["style.font_size"].lineEdit(), "16")
    workspace.flush_pending_edits()
    assert panel.increase_size.isEnabled() and panel.decrease_size.isEnabled()
    qtbot.mouseClick(panel.increase_size, Qt.MouseButton.LeftButton)
    assert all(canvas.engine.get_node(key).style["font_size"] == 17 for key in (a, b))
    assert canvas.engine.undo()
    assert all(canvas.engine.get_node(key).style["font_size"] == 16 for key in (a, b))


def test_font_picker_search_selection_enter_and_restore_default(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    picker = panel.fields["style.font_family"]
    family = picker.itemData(1)
    choose(picker, family)
    assert engine.document.root.style["font_family"] == family
    assert engine.undo() and not engine.can_undo
    edit_line(qtbot, picker.lineEdit(), "Uninstalled custom font")
    qtbot.keyClick(picker.lineEdit(), Qt.Key.Key_Return)
    workspace.flush_pending_edits()
    assert engine.document.root.style["font_family"] == "Uninstalled custom font"
    choose(picker, "")
    assert "font_family" not in engine.document.root.style


def test_custom_width_enable_disable_and_undo_preserve_unrelated_style(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    engine.update_node(engine.root_id, style={"extension": {"宽度之外": True}})
    engine.replace_document(engine.document)
    assert not panel.width_enabled.isChecked() and not panel.fields["style.width"].isEnabled()
    qtbot.mouseClick(panel.width_enabled, Qt.MouseButton.LeftButton)
    assert engine.document.root.style == {"extension": {"宽度之外": True}, "width": 280}
    assert engine.undo() and not engine.can_undo
    qtbot.mouseClick(panel.width_enabled, Qt.MouseButton.LeftButton)
    panel.fields["style.width"].setValue(340)
    workspace.flush_pending_edits()
    qtbot.mouseClick(panel.width_enabled, Qt.MouseButton.LeftButton)
    assert engine.document.root.style == {"extension": {"宽度之外": True}}
    assert not panel.fields["style.width"].isEnabled()
    assert engine.undo() and engine.document.root.style["width"] == 340
    assert panel.width_enabled.isChecked() and panel.fields["style.width"].isEnabled()


def test_mixed_custom_width_presence_and_values(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, _ = workspace.ids
    canvas.engine.update_node(a, style={"width": 350})
    canvas.engine.replace_document(canvas.engine.document)
    canvas.select_nodes([a, b])
    assert panel.width_enabled.checkState() == Qt.CheckState.PartiallyChecked
    assert panel.fields["style.width"].lineEdit().placeholderText() == "多种值"
    workspace.flush_pending_edits()
    assert not canvas.engine.can_undo
    qtbot.mouseClick(panel.width_enabled, Qt.MouseButton.LeftButton)
    assert all(canvas.engine.get_node(key).style["width"] == 280 for key in (a, b))
    assert canvas.engine.undo() and not canvas.engine.can_undo
    edit_line(qtbot, panel.fields["style.width"].lineEdit(), "300")
    workspace.flush_pending_edits()
    assert all(canvas.engine.get_node(key).style["width"] == 300 for key in (a, b))
    qtbot.mouseClick(panel.width_enabled, Qt.MouseButton.LeftButton)
    assert all("width" not in canvas.engine.get_node(key).style for key in (a, b))


def test_color_dialog_cancel_accept_restore_and_stale_result(workspace, monkeypatch):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, _ = workspace.ids
    canvas.select_nodes([a, b])
    color = panel.fields["style.background"]
    before = canvas.engine.document
    monkeypatch.setattr(QColorDialog, "getColor", lambda *args: QColor())
    color.choose_action.trigger()
    assert canvas.engine.document == before and not canvas.engine.can_undo
    monkeypatch.setattr(QColorDialog, "getColor", lambda *args: QColor("#235486"))
    color.choose_action.trigger()
    assert all(canvas.engine.get_node(key).style["background"] == "#235486" for key in (a, b))
    assert canvas.engine.undo() and not canvas.engine.can_undo
    color.choose_action.trigger()
    color.reset_action.trigger()
    assert all("background" not in canvas.engine.get_node(key).style for key in (a, b))
    assert canvas.engine.undo()
    assert all(canvas.engine.get_node(key).style["background"] == "#235486" for key in (a, b))
    canvas.select_nodes([a])
    before = canvas.engine.document

    def select_another_node(*args):
        canvas.select_nodes([b])
        return QColor("#ff0000")

    monkeypatch.setattr(QColorDialog, "getColor", select_another_node)
    color.choose_action.trigger()
    assert canvas.selected_node_ids() == [b] and canvas.engine.document == before


def open_branch_grid(panel, qtbot):
    """通过实际下拉按钮打开网格，覆盖自定义弹层与组合框值模型之间的连接。"""
    picker = panel.fields["branch.kind"]
    panel.scroll.ensureWidgetVisible(picker)
    QApplication.processEvents()
    qtbot.mouseClick(picker, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(picker.popup_menu.isVisible)
    return picker, picker.popup_menu


def test_property_branch_grid_click_commit_switch_and_reset(workspace, qtbot):
    panel, canvas = workspace.properties, workspace.canvas
    before = canvas.engine.document
    picker, menu = open_branch_grid(panel, qtbot)
    assert len(menu.buttons) == 11 and menu.scope is None
    menu.buttons[BranchStyle.ARROW2].entered.emit()
    assert canvas.engine.document == before and not canvas.engine.can_undo
    qtbot.mouseClick(menu.buttons[BranchStyle.ARROW2], Qt.MouseButton.LeftButton)
    assert not menu.isVisible() and picker.currentData() == "arrow2"
    assert all(node.style["branch"]["kind"] == "arrow2" for node in canvas.engine.document.nodes.values())
    assert all(branch.arrow is not None for branch in canvas.layout_result.branches.values())
    _, menu = open_branch_grid(panel, qtbot)
    assert menu.buttons[BranchStyle.ARROW2].isChecked()
    qtbot.mouseClick(menu.buttons[BranchStyle.LINE], Qt.MouseButton.LeftButton)
    assert all(branch.arrow is None for branch in canvas.layout_result.branches.values())
    _, menu = open_branch_grid(panel, qtbot)
    qtbot.mouseClick(menu.reset, Qt.MouseButton.LeftButton)
    assert all("branch" not in node.style for node in canvas.engine.document.nodes.values())
    assert canvas.engine.undo() and canvas.engine.document.root.style["branch"]["kind"] == "line"
    assert canvas.engine.undo() and canvas.engine.document.root.style["branch"]["kind"] == "arrow2"
    assert canvas.engine.undo() and not canvas.engine.can_undo and canvas.engine.document == before


def test_property_branch_grid_mixed_keyboard_and_cancel(workspace, qtbot):
    canvas, panel = workspace.canvas, workspace.properties
    a, b, leaf = workspace.ids
    canvas.engine.set_branch_style([a], "round", scope="level")
    canvas.engine.set_branch_style([b], "line", scope="level")
    canvas.engine.replace_document(canvas.engine.document)
    canvas.select_nodes([a, b])
    canvas.set_branch_scope("level")
    before = canvas.engine.document
    picker, menu = open_branch_grid(panel, qtbot)
    assert not any(button.isChecked() for button in menu.buttons.values())
    menu.buttons[BranchStyle.LINE].setFocus()
    qtbot.keyClick(menu.buttons[BranchStyle.LINE], Qt.Key.Key_Right)
    assert menu.buttons[BranchStyle.ARC].hasFocus()
    qtbot.keyClick(menu.buttons[BranchStyle.ARC], Qt.Key.Key_Escape)
    assert not menu.isVisible() and canvas.engine.document == before and not canvas.engine.can_undo
    picker.setFocus()
    qtbot.keyClick(picker, Qt.Key.Key_Space)
    assert menu.isVisible()
    menu.buttons[BranchStyle.ARC].setFocus()
    qtbot.keyClick(menu.buttons[BranchStyle.ARC], Qt.Key.Key_Return)
    assert not menu.isVisible()
    assert all(canvas.engine.get_node(key).style["branch"]["kind"] == "arc" for key in (a, b))
    assert canvas.engine.get_node(leaf).style["branch"]["kind"] == "round"
    assert canvas.engine.undo() and not canvas.engine.can_undo and canvas.engine.document == before


@pytest.mark.parametrize("transition", ["selection", "readonly", "hide", "replace", "dispose"])
def test_property_branch_grid_stale_menu_cannot_commit(workspace, qtbot, transition):
    canvas, panel = workspace.canvas, workspace.properties
    _, menu = open_branch_grid(panel, qtbot)
    if transition == "selection":
        canvas.select_nodes([workspace.ids[0]])
    elif transition == "readonly":
        canvas.set_editable(False)
    elif transition == "hide":
        workspace.hide_tool_window("properties")
    elif transition == "dispose":
        workspace.dispose()
    else:
        canvas.engine.replace_document(MindMapDocument.new("替换文档"))
    before = canvas.engine.document
    assert not menu.isVisible()
    menu.buttons[BranchStyle.ROUND].click()
    assert canvas.engine.document == before and not canvas.engine.can_undo
