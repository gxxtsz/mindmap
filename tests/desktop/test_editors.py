"""文档卡片的交互回归：关闭占位、键盘访问、排序及迁移均使用真实 QWidget。"""

import pytest
from mindmap_docking import DockTarget
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTabBar


def add_peer(docking):
    """添加第二份业务内容，返回两份文档仍处于同一组的页签栏。"""
    docking.manager.add_document("peer_doc", "另一份文档", QPlainTextEdit("第二份内容"))
    group = docking.host.editor.groups[0]
    group.setCurrentIndex(0)
    QApplication.processEvents()
    return group, group.tabBar()


def close_button(bar, index):
    return bar.tabButton(index, QTabBar.ButtonPosition.RightSide)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_hover_and_keyboard_close_feedback_keep_tab_geometry(docking, qtbot, theme, compact):
    """悬停和键盘聚焦均能找到关闭入口，显示图形不能触发文字或标签宽度变化。"""
    group, bar = add_peer(docking)
    docking.manager.set_theme(theme)
    docking.manager.set_option("compact", compact)
    qtbot.mouseMove(docking.document, QPoint(20, 40))
    QApplication.processEvents()
    buttons = [close_button(bar, index) for index in range(2)]
    rectangles = [bar.tabRect(index) for index in range(2)]
    assert [button._revealed for button in buttons] == [True, False]
    assert all(button.isVisible() for button in buttons)

    qtbot.mouseMove(bar, bar.tabRect(1).center())
    qtbot.waitUntil(lambda: buttons[1]._revealed)
    assert [bar.tabRect(index) for index in range(2)] == rectangles
    qtbot.mouseMove(docking.document, QPoint(20, 40))
    qtbot.waitUntil(lambda: not buttons[1]._revealed)

    buttons[1].setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(buttons[1].hasFocus)
    assert buttons[1]._revealed
    assert [bar.tabRect(index) for index in range(2)] == rectangles
    docking.document.setFocus()
    qtbot.waitUntil(lambda: not buttons[1]._revealed)
    assert group.currentWidget() is docking.document


def test_reordered_close_button_keeps_its_document_and_business_guard(docking, qtbot):
    """重排后按稳定 token 关闭；业务拒绝时保留原组与内容，再允许键盘关闭。"""
    group, bar = add_peer(docking)
    manager = docking.manager
    content = manager.documents["peer_doc"]["widget"]
    content.setPlainText("拒绝关闭时保留")
    bar.moveTab(1, 0)
    assert bar.tabData(0) == "peer_doc"
    manager.set_document_modified("peer_doc", True)
    button = close_button(bar, 0)
    button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.keyClick(button, Qt.Key.Key_Space)
    assert group.count() == 2
    assert not manager.documents["peer_doc"]["closed"]
    assert content.toPlainText() == "拒绝关闭时保留"

    manager.set_document_modified("peer_doc", False)
    qtbot.keyClick(button, Qt.Key.Key_Space)
    assert group.count() == 1
    assert bar.tabData(0) == "doc"
    assert manager.documents["peer_doc"]["closed"]
    manager.open_document("peer_doc")
    assert group.currentWidget() is content


@pytest.mark.parametrize("orientation", [Qt.Orientation.Horizontal, Qt.Orientation.Vertical])
def test_split_theme_and_restore_keep_documents_and_close_routing(docking, qtbot, orientation):
    """文档与编辑器内工具可各自分屏；切换主题和恢复后关闭仍路由到正确实例。"""
    group, bar = add_peer(docking)
    manager, area = docking.manager, docking.host.editor
    peer = manager.documents["peer_doc"]["widget"]
    area.split_token("peer_doc", orientation)
    manager.move("alpha", DockTarget("editor"))
    tool_content = manager.panel("alpha").content
    area.split_token("tool:alpha", Qt.Orientation.Vertical)
    snapshot = manager.capture_layout()
    before = {token: area.find_token(token)[0].group_id for token in ["doc", "peer_doc", "tool:alpha"]}
    manager.set_theme("dark")
    manager.set_option("compact", True)
    manager.restore_layout(snapshot)
    assert {token: area.find_token(token)[0].group_id for token in before} == before
    assert area.find_token("doc")[0].widget(area.find_token("doc")[1]) is docking.document
    assert area.find_token("peer_doc")[0].widget(area.find_token("peer_doc")[1]) is peer
    assert manager.panel("alpha").content is tool_content
    tool_group, index = area.find_token("tool:alpha")
    qtbot.mouseClick(close_button(tool_group.tabBar(), index), Qt.MouseButton.LeftButton)
    assert not manager.states["alpha"].visible
    assert area.find_token("tool:alpha") is None
    assert manager.panel("alpha").content is tool_content
