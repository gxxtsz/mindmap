"""最近访问的独立面板验收：真实键鼠事件、稳定身份、高亮、主题与生命周期。"""

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_editor.panels import RecentPagesPanel
from mindmap_qt_editor.theme import ui_colors
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@pytest.fixture
def recent_panel(qtbot):
    engine = MindMapEngine()
    first = engine.create_submap(engine.add_child(engine.root_id, "同名页面"))
    second = engine.create_detail_page(engine.add_child(engine.root_id, "同名页面"))
    panel = RecentPagesPanel()
    qtbot.addWidget(panel)
    panel.resize(230, 280)
    panel.show()
    yield engine, panel, first, second
    panel.dispose()


def test_click_and_keyboard_navigate_only_on_request_and_keep_confirmed_highlight(recent_panel, qtbot):
    engine, panel, first, second = recent_panel
    assert panel.empty_label.isVisible()
    panel.set_entries([engine.page_summary(first), engine.page_summary(second)])
    panel.set_current_page(first)
    requests = []
    panel.navigate_requested.connect(lambda page, root: requests.append((page, root)))
    QApplication.processEvents()
    panel.list_view.setFocus()
    qtbot.keyClick(panel.list_view, Qt.Key.Key_Down)
    assert not requests and panel.current_key == (first, None)
    qtbot.keyClick(panel.list_view, Qt.Key.Key_Return)
    assert requests == [(second, None)] and panel.current_key == (first, None)
    panel.set_current_page(first, reset_selection=True)
    assert panel.list_view.currentIndex() == panel.model.index_for((first, None))
    index = panel.model.index_for((second, None))
    qtbot.mouseClick(panel.list_view.viewport(), Qt.MouseButton.LeftButton,
                     pos=panel.list_view.visualRect(index).center())
    assert requests == [(second, None), (second, None)]
    assert "详情页" in index.data(Qt.ItemDataRole.ToolTipRole)


def test_reorder_and_title_updates_preserve_keyboard_target_and_do_not_navigate(recent_panel, qtbot):
    engine, panel, first, second = recent_panel
    rows = [engine.page_summary(first), engine.page_summary(second)]
    panel.set_entries(rows)
    panel.set_current_page(first)
    panel.list_view.setCurrentIndex(panel.model.index_for((second, None)))
    requests = []
    panel.navigate_requested.connect(lambda *args: requests.append(args))
    panel.set_entries(list(reversed(rows)))
    assert panel.list_view.currentIndex() == panel.model.index_for((second, None))
    root = rows[1].path[-1].node_id
    engine.update_node(root, text="很长的中文页面名称\n" * 12)
    panel.set_entries([engine.page_summary(second), engine.page_summary(first)])
    assert panel.list_view.currentIndex() == panel.model.index_for((second, None))
    assert "很长的中文页面名称" in panel.model.index(0).data(Qt.ItemDataRole.ToolTipRole)
    for theme in (Theme(), Theme.dark()):
        panel.set_ui_colors(ui_colors(theme))
        panel.resize(140, 200)
        assert not panel.grab().isNull()
    assert not requests and panel.current_key == (first, None)
    panel.dispose()
    qtbot.keyClick(panel.list_view, Qt.Key.Key_Return)
    panel.set_entries([])
    assert not requests and panel.model.rowCount() == 2
