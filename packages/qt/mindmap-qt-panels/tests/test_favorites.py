"""独立收藏面板的真实列表交互、动态路径、只读状态和订阅释放。"""

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_panels import SubmapFavoritesPanel
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@pytest.fixture
def favorite_panel(qtbot):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "同名页面")
    first = engine.create_submap(owner)
    nested_owner = engine.add_child(engine.page_document(first).root_id, "同名页面")
    second = engine.create_submap(nested_owner)
    panel = SubmapFavoritesPanel(engine)
    qtbot.addWidget(panel)
    panel.resize(230, 280)
    panel.show()
    yield engine, panel, owner, first, second
    panel.dispose()


def test_empty_and_populated_list_emit_requests_without_mutating_engine(favorite_panel, qtbot):
    engine, panel, owner, first, second = favorite_panel
    assert panel.empty_label.isVisible()
    for page in (first, second):
        engine.set_submap_favorite(page, True)
    engine.mark_saved()
    before, revision = engine.document, engine.revision
    assert panel.model.rowCount() == 2 and panel.list_view.isVisible()
    assert [row[0].page_id for row in panel.model.rows] == [second, first]
    assert panel.model.index(0, 0).data(Qt.ItemDataRole.ToolTipRole) == (
        "同名页面\n中心主题 › 同名页面 › 同名页面")
    requested = []
    panel.navigate_requested.connect(requested.append)
    panel.set_current_page_id(second)
    QApplication.processEvents()
    index = panel.model.index(1, 0)
    qtbot.mouseClick(panel.list_view.viewport(), Qt.MouseButton.LeftButton,
                     pos=panel.list_view.visualRect(index).center())
    assert requested == [first]
    panel.list_view.setFocus()
    qtbot.keyClick(panel.list_view, Qt.Key.Key_Return)
    assert requested[-1] == first
    assert engine.document == before and engine.revision == revision


def test_names_paths_and_current_page_stay_synchronized(favorite_panel):
    engine, panel, owner, first, second = favorite_panel
    engine.set_submap_favorite(second, True)
    panel.set_current_page_id(second)
    engine.update_node(owner, text="改名后的父页")
    assert panel.model.rows[0][1] == "中心主题 › 改名后的父页 › 同名页面"
    assert panel.list_view.currentIndex().isValid()
    engine.remove_submap(owner)
    assert panel.empty_label.isVisible() and panel.model.rowCount() == 0
    engine.undo()
    assert panel.model.rowCount() == 1 and panel.model.rows[0][0].page_id == second
    panel.set_current_page_id(None)
    assert not panel.list_view.currentIndex().isValid()


def test_readonly_and_dispose_reject_stale_menu_callbacks(favorite_panel):
    engine, panel, owner, first, second = favorite_panel
    engine.set_submap_favorite(first, True)
    requested = []
    panel.unfavorite_requested.connect(requested.append)
    panel.remove_action.setData(first)
    panel.set_editable(False)
    panel.remove_action.trigger()
    assert not requested and not panel.remove_action.isEnabled()
    panel.set_editable(True)
    panel.remove_action.trigger()
    assert requested == [first] and engine.is_submap_favorite(first)
    panel.close_popups()
    panel.remove_action.trigger()
    assert requested == [first]
    panel.set_ui_colors(ui_colors(Theme.dark()))
    panel.dispose()
    panel.dispose()
    engine.set_submap_favorite(second, True)
    assert panel.model.rowCount() == 1
    panel.remove_action.setData(second)
    panel.remove_action.trigger()
    assert requested == [first]
