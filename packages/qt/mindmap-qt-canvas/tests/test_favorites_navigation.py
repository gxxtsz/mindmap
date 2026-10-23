"""收藏星标、节点入口、跨页历史及输入法保护的原生画布交互。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError, Theme
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_panels import MindMapPanels
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication


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
