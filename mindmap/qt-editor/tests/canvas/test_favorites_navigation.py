"""收藏星标、节点入口、跨页历史及输入法保护的原生画布交互。"""

import pytest
from mindmap_core import GraphGroup, GraphNode, MindMapEngine, MindMapError, NodeGraph, Theme
from mindmap_core.formats.graph import format_graph_text
from mindmap_core.node_pages import graph_page_id
from mindmap_qt_editor.canvas import MindMapWidget
from mindmap_qt_editor.panels import MindMapPanels
from mindmap_qt_editor.theme import ui_colors
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QMenu


@pytest.fixture
def favorite_canvas(qtbot):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "收藏入口")
    page = engine.create_submap(owner)
    canvas = MindMapWidget(engine, toolbar=False)
    panels = MindMapPanels(canvas)
    qtbot.addWidget(canvas)
    canvas.resize(900, 600)
    canvas.show()
    QApplication.processEvents()
    yield canvas, panels, owner, page
    panels.properties.discard_pending()
    canvas.finish_edit(commit=False)
    panels.dispose()
    canvas.dispose()


def test_node_action_and_current_page_star_share_state(favorite_canvas, qtbot):
    canvas, panels, owner, page = favorite_canvas
    star = canvas.path_navigation.favorite_button
    assert not star.isVisible()
    canvas.select_nodes([owner])
    action = canvas.action("favorite_submap")
    assert action.isVisible() and action.isEnabled() and action.text() == "收藏子导图"
    action.trigger()
    assert canvas.current_page_id is None and canvas.engine.is_submap_favorite(page)
    assert action.text() == "取消收藏子导图"
    canvas.enter_submap(owner)
    assert star.isVisible() and star.isChecked() and not star.icon().isNull()
    qtbot.mouseClick(star, Qt.MouseButton.LeftButton)
    assert canvas.current_page_id == page and not canvas.engine.is_submap_favorite(page)
    assert not star.isChecked()
    canvas.engine.undo()
    assert star.isChecked()
    canvas.set_ui_colors(ui_colors(Theme.dark()))
    canvas.resize(290, 500)
    QApplication.processEvents()
    assert canvas.path_navigation.favorite_button is star and star.isVisible() and star.isChecked()
    canvas.return_to_parent_page()
    assert not star.isVisible()
    assert canvas.action("favorite_submap").text() == "取消收藏子导图"


def test_node_favorite_targets_its_child_page_and_detail_star_is_hidden(favorite_canvas):
    canvas, panels, owner, page = favorite_canvas
    canvas.enter_submap(owner)
    child = canvas.engine.add_child(canvas.document_view.root_id, "嵌套入口")
    nested = canvas.engine.create_submap(child)
    canvas.select_nodes([child])
    canvas.action("favorite_submap").trigger()
    assert canvas.engine.is_submap_favorite(nested) and not canvas.engine.is_submap_favorite(page)
    assert not canvas.path_navigation.favorite_button.isChecked()
    detail = canvas.create_detail_page()
    assert canvas.current_page_id == detail
    assert not canvas.path_navigation.favorite_button.isVisible()
    canvas.navigate_to(nested)
    canvas.set_editable(False)
    assert canvas.path_navigation.favorite_button.isChecked()
    assert not canvas.path_navigation.favorite_button.isEnabled()
    with pytest.raises(MindMapError, match="只读"):
        canvas.set_submap_favorite(nested, False)
    canvas.navigate_to(page, child)
    assert not canvas.action("favorite_submap").isEnabled()
    canvas.enter_submap(child)
    assert canvas.current_page_id == nested


def test_favorite_commits_valid_property_input_and_preserves_ime_drafts(favorite_canvas, qtbot):
    canvas, panels, owner, page = favorite_canvas
    canvas.select_nodes([owner])
    field = panels.properties.fields["style.font_size"]
    field.setValue(29)
    panels.properties.timer.stop()
    canvas.set_submap_favorite(page, True)
    assert canvas.engine.get_node(owner).style["font_size"] == 29
    assert canvas.engine.is_submap_favorite(page)
    canvas.begin_edit(owner)
    canvas.editor.setPlainText("未提交的节点名称")
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    before = canvas.engine.document
    with pytest.raises(MindMapError, match="候选"):
        canvas.set_submap_favorite(page, False)
    assert canvas.is_composing and canvas.engine.document == before
    assert not canvas.path_navigation.favorite_button.isEnabled()
    canvas.finish_edit(commit=False)
    line = panels.properties.fields["style.font_family"].lineEdit()
    QApplication.sendEvent(line, QInputMethodEvent("字体候选", []))
    with pytest.raises(MindMapError, match="候选"):
        canvas.set_submap_favorite(page, False)
    assert panels.is_composing and canvas.engine.document == before
    QApplication.sendEvent(line, QInputMethodEvent())
    canvas.set_submap_favorite(page, False)
    assert not canvas.engine.is_submap_favorite(page)
    canvas.enter_submap(owner)
    QApplication.processEvents()
    line = panels.properties.fields["style.font_family"].lineEdit()
    QApplication.sendEvent(line, QInputMethodEvent("候选", []))
    qtbot.mouseClick(canvas.path_navigation.favorite_button, Qt.MouseButton.LeftButton)
    assert not canvas.path_navigation.favorite_button.isChecked()
    assert not canvas.engine.is_submap_favorite(page) and panels.is_composing
    QApplication.sendEvent(line, QInputMethodEvent())


def test_favorites_do_not_reset_page_views_and_undo_uses_origin_page(favorite_canvas):
    canvas, panels, owner, page = favorite_canvas
    canvas.enter_submap(owner)
    QApplication.processEvents()
    canvas.view.zoom(1.4)
    before = canvas.view.transform()
    canvas.set_submap_favorite(page, True)
    assert canvas.view.transform() == before
    canvas.navigate_to(None)
    canvas.engine.undo()
    assert canvas.current_page_id == page and not canvas.engine.is_submap_favorite(page)
    assert canvas.view.transform() == before


def _click_node_menu(canvas, qtbot, key, text):
    """通过真实画布右键打开菜单并点击指定的节点收藏动作。"""
    node = canvas.node_items[key]
    point = canvas.view.mapFromScene(node.sceneBoundingRect().center())
    clicked = []

    def choose():
        menus = canvas.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if not menus:
            return
        menu = menus[-1]
        action = next((action for action in menu.actions() if action.text() == text), None)
        if action is not None and action.isEnabled():
            qtbot.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
            clicked.append(text)
        else:
            menu.close()

    QTimer.singleShot(0, choose)
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.RightButton, pos=point)
    assert clicked == [text]


def test_tree_and_graph_node_favorite_from_canvas_menu(favorite_canvas, qtbot):
    canvas, panels, owner, page = favorite_canvas
    engine, tree = canvas.engine, panels.outline
    revision = engine.revision
    tree.set_favorites_only(True)
    assert engine.revision == revision
    _click_node_menu(canvas, qtbot, owner, "收藏节点")
    assert engine.get_node(owner).favorite and canvas.node_items[owner].visual.favorite
    assert not engine.is_submap_favorite(page)
    assert tree.tree_model.index_for(owner).isValid()
    _click_node_menu(canvas, qtbot, owner, "取消收藏节点")
    assert not engine.get_node(owner).favorite and tree.tree_model.rowCount() == 0

    engine.set_node_graph(owner, NodeGraph(
        (GraphNode("b", "先声明"), GraphNode("g", "分组标题"), GraphNode("a", "组内普通节点")),
        groups=(GraphGroup("g", ("a",)),),
    ))
    canvas.navigate_to(graph_page_id(owner))
    QApplication.processEvents()
    canvas.fit_to_view()
    QApplication.processEvents()
    _click_node_menu(canvas, qtbot, "g", "收藏节点")
    _click_node_menu(canvas, qtbot, "b", "收藏节点")
    model = tree.tree_model
    assert [model.node_id(model.index(row, 0)) for row in range(model.rowCount())] == ["b", "g"]
    assert model.rowCount(model.index_for("g")) == 0
    assert canvas.node_items["g"].group_title and canvas.node_items["g"].visual.favorite
    assert canvas.apply_graph_text(format_graph_text(canvas.graph).replace("先声明", "保留局部标识后改名"))
    assert model.index_for("b").data() == "保留局部标识后改名"
    _click_node_menu(canvas, qtbot, "b", "取消收藏节点")
    assert model.rowCount() == 1
    assert engine.undo() and model.index_for("b").isValid()
    assert engine.redo() and not model.index_for("b").isValid()
    canvas.select_nodes(["g"])
    canvas.ungroup_graph_node()
    assert model.index_for("g").isValid() and canvas.document_view.nodes["g"].favorite
    assert not canvas.node_items["g"].group_title
    canvas.navigate_to(page)
    assert tree.favorites_only and model.rowCount() == 0
    canvas.action("favorite_node").trigger()
    assert model.rowCount() == 1 and engine.page_document(page).root.favorite
    detail = engine.create_detail_page(owner)
    canvas.navigate_to(detail)
    canvas.action("favorite_node").trigger()
    assert model.rowCount() == 1 and engine.page_document(detail).root.favorite
    thought = engine.add_inspiration("独立想法", position=(0, 0))
    canvas.show_inspiration(thought)
    canvas.action("favorite_node").trigger()
    assert model.index_for(thought).isValid() and engine.get_node(thought).favorite


def test_node_favorite_rejects_uncommitted_ime_and_multiselect(favorite_canvas):
    canvas, panels, owner, _page = favorite_canvas
    action = canvas.action("favorite_node")
    canvas.select_nodes([owner, canvas.engine.root_id])
    assert not action.isVisible()
    canvas.select_nodes([owner])
    canvas.begin_edit(owner)
    canvas.editor.setPlainText("当前草稿")
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    before = canvas.engine.document
    with pytest.raises(MindMapError, match="输入法候选"):
        canvas.toggle_node_favorite()
    assert canvas.engine.document == before and canvas.is_composing and not action.isEnabled()
    canvas.finish_edit(commit=False)
    field = panels.properties.fields["style.font_family"].lineEdit()
    QApplication.sendEvent(field, QInputMethodEvent("字体候选", []))
    with pytest.raises(MindMapError, match="输入法候选"):
        canvas.toggle_node_favorite()
    assert canvas.engine.document == before and panels.is_composing
    QApplication.sendEvent(field, QInputMethodEvent())
    action.trigger()
    assert canvas.engine.get_node(owner).favorite


def test_cut_paste_starts_new_node_without_favorite_and_undo_restores_source(favorite_canvas):
    """通过实际剪贴板入口验证剪切生成新节点及原节点撤销后的收藏。"""
    canvas, _panels, owner, _page = favorite_canvas
    engine = canvas.engine
    engine.set_node_favorite(owner, True)
    canvas.select_nodes([owner])
    canvas.cut()
    [new] = canvas.paste()
    assert new != owner and not engine.get_node(new).favorite
    assert engine.undo() and engine.undo()
    assert engine.get_node(owner).favorite
