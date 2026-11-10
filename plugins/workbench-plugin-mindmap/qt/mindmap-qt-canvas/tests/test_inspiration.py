"""独立想法空态、完整迁移、导航、剪贴板与单根大纲验收。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_panels import MindMapPanels
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox, QTreeWidget


@pytest.fixture
def canvas(qtbot):
    widget = MindMapWidget(MindMapEngine(), toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(1100, 700)
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.dispose()


def test_empty_board_create_details_and_outline(canvas):
    panels = MindMapPanels(canvas)
    canvas.show_inspiration()
    assert canvas.current_page_kind == "inspiration"
    assert not canvas.node_items
    assert panels.outline.tree_model.rowCount() == 0
    a = canvas.add_inspiration((120, 30))
    canvas.finish_edit()
    b = canvas.add_inspiration((-300, -50))
    canvas.finish_edit()
    assert set(canvas.node_items) == {b}
    assert panels.outline.tree_model.rowCount() == 1
    assert canvas.layout_result.nodes[b].x == 0
    canvas.show_inspiration(a)
    assert set(canvas.node_items) == {a}
    assert panels.outline.tree_model.rowCount() == 1
    assert canvas.layout_result.nodes[a].x == 0
    assert canvas.engine.document.inspiration.positions[a] == (120, 30)
    canvas.select_nodes([a])
    detail = canvas.create_detail_page()
    assert canvas.current_page_id == detail
    canvas.return_to_parent_page()
    assert canvas.current_page_id == canvas.engine.inspiration_id
    assert canvas.current_inspiration_root_id == a
    assert [entry.text for entry in canvas.path_navigation.entries] == [canvas.engine.get_node(a).text]
    panels.dispose()


def test_transfer_descendants_and_undo_across_pages(canvas):
    e = canvas.engine
    branch = e.add_child(e.root_id, "源分支")
    child = e.add_child(branch, "随行孩子")
    canvas.select_nodes([branch])
    canvas.move_selected_to_inspiration()
    assert canvas.current_page_id == e.inspiration_id
    assert set(canvas.node_items) == {branch, child}
    e.attach_inspirations([branch], e.root_id)
    assert canvas.current_page_id is None
    assert child in canvas.node_items
    e.undo()
    assert canvas.current_page_id == e.inspiration_id
    assert child in canvas.node_items
    e.undo()
    assert canvas.current_page_id is None
    assert child in canvas.node_items
    canvas.show_inspiration()
    assert not canvas.node_items


def test_paste_without_selection_and_delete_last_root(canvas):
    canvas.show_inspiration()
    QApplication.clipboard().setText("临时想法")
    ids = canvas.paste()
    assert len(ids) == 1
    assert canvas.document_view.root_ids == tuple(ids)
    assert canvas.engine.get_node(ids[0]).text == "临时想法"
    canvas.delete_selected()
    assert not canvas.node_items
    canvas.engine.undo()
    assert ids[0] in canvas.node_items


def test_idea_drag_reparents_children_but_blank_roots_and_branches_cancel(canvas, qtbot, caplog):
    e = canvas.engine
    a = e.add_inspiration("甲", position=(-300, 0))
    b = e.add_inspiration("乙", position=(100, 0))
    child = e.add_child(a, "孩子")
    sibling = e.add_child(a, "接收主题")
    canvas.show_inspiration(a)
    canvas.view.centerOn(0, 0)
    control = canvas.view.node_drag

    def drag(key, destination):
        canvas.select_nodes([key])
        origin = QPointF(*canvas.layout_result.nodes[key].center)
        assert control.begin(origin)
        point = canvas.view.mapFromScene(destination)
        control.update(point)
        canvas.grab()  # 同时验收空白落点提示的绘制路径。
        drop = control.finish(point)
        assert drop is not None
        return control.commit(drop)

    original = e.document
    assert b not in canvas.node_items
    assert drag(child, QPointF(*canvas.layout_result.nodes[sibling].center))
    assert e.parent_id(child) == sibling
    assert e.document.inspiration.positions == original.inspiration.positions
    e.undo()
    assert e.document == original
    for key in (a, child):
        canvas.select_nodes([key])
        assert control.begin(QPointF(*canvas.layout_result.nodes[key].center))
        blank = canvas.view.mapFromScene(QPointF(-400, 400))
        control.update(blank)
        assert control._hint_text() == ""
        assert control.finish(blank) is None
        assert e.document == original
    canvas.select_nodes([a])
    origin = QPointF(*canvas.layout_result.nodes[a].center)
    qtbot.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton,
                     pos=canvas.view.mapFromScene(origin))
    assert canvas.view.press_node == a
    assert control.begin(origin)
    control.update(canvas.view.mapFromScene(QPointF(-200, 200)))
    qtbot.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert not control.active
    assert e.document == original
    assert not any(record.levelname == "ERROR" for record in caplog.records)


def test_blank_double_click_and_composing_switch_are_protected(canvas, qtbot):
    canvas.show_inspiration()
    point = canvas.view.viewport().rect().center()
    qtbot.mouseDClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.editor is not None
    event = QInputMethodEvent("输入中", [])
    canvas.scene.sendEvent(canvas.editor, event)
    assert canvas.is_composing
    qtbot.mouseClick(canvas.path_navigation.back_button, Qt.MouseButton.LeftButton)
    assert canvas.current_page_kind == "inspiration"
    assert not hasattr(canvas.path_navigation, "inspiration_button")
    assert canvas.path_navigation.back_button.toolTip() == "返回主导图"
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent())
    canvas.finish_edit(commit=False)
    qtbot.mouseClick(canvas.path_navigation.back_button, Qt.MouseButton.LeftButton)
    assert canvas.current_page_id is None and not canvas.path_navigation.back_button.isEnabled()


def test_target_dialog_selects_submap_and_preserves_owned_pages(canvas, caplog):
    e = canvas.engine
    owner = e.add_child(e.root_id, "接收入口")
    submap = e.create_submap(owner)
    target = e.document.submaps[submap].root_id
    source = e.add_inspiration("游离主题", position=(0, 0))
    detail = e.create_detail_page(source)
    canvas.show_inspiration()
    canvas.select_nodes([source])

    def choose():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        pages = dialog.findChild(QComboBox)
        pages.setCurrentIndex(pages.findData(submap))
        tree = dialog.findChild(QTreeWidget)
        tree.setCurrentItem(tree.topLevelItem(0))
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        buttons.button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, choose)
    canvas.attach_selected_inspirations()
    assert e.parent_id(source) == target
    assert canvas.current_page_id == submap
    assert e.get_node(source).detail_page_id == detail
    e.undo()
    canvas.set_editable(False)
    snapshot = e.document
    with pytest.raises(MindMapError):
        canvas.add_inspiration()
    assert e.document == snapshot
    assert not any(record.levelname == "ERROR" for record in caplog.records)
