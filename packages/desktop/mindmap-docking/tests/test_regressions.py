"""由真实界面审查发现的边界回归，确保单主窗口及尺寸记忆不破坏现有状态。"""

import copy

import pytest
from mindmap_docking import DockPosition, DockTarget
from PySide6.QtWidgets import QApplication


def test_title_press_does_not_refresh_or_release_source(docking, qtbot):
    from PySide6.QtCore import Qt
    manager = docking.manager
    manager.activate("alpha")
    header = manager.panel("alpha").header
    events = []
    manager.layout_changed.connect(lambda: events.append(True))
    qtbot.mousePress(header, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(header, Qt.MouseButton.LeftButton)
    assert not events
    assert header.isVisible()


def test_switching_tools_uses_latest_shared_region_size(docking):
    manager, host = docking.manager, docking.host
    manager.activate("alpha", focus=False)
    QApplication.processEvents()
    host.horizontal.setSizes([220, host.horizontal.width() - host.horizontal.handleWidth() - 220, 0])
    host._splitter_moved()
    assert host.zones[DockPosition.LEFT_TOP].width() == 220
    manager.activate("peer", focus=False)
    assert host.zones[DockPosition.LEFT_TOP].width() == 220
    host.horizontal.setSizes([380, host.horizontal.width() - host.horizontal.handleWidth() - 380, 0])
    host._splitter_moved()
    assert host.zones[DockPosition.LEFT_TOP].width() == 380
    manager.activate("alpha", focus=False)
    assert host.zones[DockPosition.LEFT_TOP].width() == 380
    manager.activate("peer", focus=False)
    assert host.zones[DockPosition.LEFT_TOP].width() == 380





def test_selected_document_survives_tool_refresh(docking):
    manager, host = docking.manager, docking.host
    manager.move("alpha", DockTarget("editor"))
    group = host.editor.groups[0]
    manager.move_document("doc", "main")
    manager.active_id = ""
    token = group.tabBar().tabData(group.currentIndex())
    manager.set_theme("dark")
    assert group.tabBar().tabData(group.currentIndex()) == token == "doc"
    snapshot = manager.capture_layout()
    manager.restore_layout(snapshot)
    restored = host.editor.groups[0]
    assert restored.tabBar().tabData(restored.currentIndex()) == "doc"


@pytest.mark.parametrize("damage", ["settings", "host_geometry", "split_sizes", "current", "active"])
def test_deep_snapshot_damage_is_rejected_atomically(docking, damage):
    manager = docking.manager
    before = manager.capture_layout()
    invalid = copy.deepcopy(before)
    if damage == "settings":
        invalid["settings"] = []
    elif damage == "host_geometry":
        invalid["hosts"]["main"]["geometry"] = ["bad", 1, 2, 3]
    elif damage == "split_sizes":
        invalid["hosts"]["main"]["editor"]["sizes"] = ["bad"]
    elif damage == "current":
        invalid["hosts"]["main"]["editor"]["children"][0]["current"] = "bad"
    else:
        invalid["active"] = []
    with pytest.raises(ValueError):
        manager.restore_layout(invalid)
    assert manager.capture_layout() == before


def test_mixed_editor_tab_order_survives_theme_and_restore(docking):
    manager, host = docking.manager, docking.host
    manager.move("alpha", DockTarget("editor"))
    manager.move("beta", DockTarget("editor"))
    group = host.editor.groups[0]
    def tokens():
        return [group.tabBar().tabData(n) for n in range(group.count())]
    bar = group.tabBar()
    bar.moveTab(tokens().index("tool:alpha"), 1)
    bar.moveTab(tokens().index("tool:beta"), 0)
    before = tokens()
    manager.set_theme("dark")
    assert tokens() == before
    snapshot = manager.capture_layout()
    manager.restore_layout(snapshot)
    group = host.editor.groups[0]
    assert tokens() == before



def test_hiding_panel_returns_to_last_document_in_other_split(docking, qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPlainTextEdit
    manager, host = docking.manager, docking.host
    second = QPlainTextEdit("最后编辑的文档")
    manager.add_document("second", "second.py", second)
    host.editor.split_token("second", Qt.Orientation.Horizontal)
    docking.window.activateWindow()
    second.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is second)
    QApplication.processEvents()
    manager.activate("alpha")
    qtbot.waitUntil(lambda: QApplication.focusWidget() is manager.panel("alpha").content)
    manager.hide("alpha")
    qtbot.waitUntil(lambda: QApplication.focusWidget() is second)



def test_rail_reorder_accounts_for_removed_source(docking):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QDropEvent
    manager, rail = docking.manager, docking.host.left_rail
    manager.activate("alpha", focus=False)
    QApplication.processEvents()
    peer = rail._buttons["peer"]
    point = peer.mapTo(rail, QPoint(peer.width() // 2, peer.height() // 4))
    # QDropEvent 仅借用 QMimeData 指针；必须保持 Python 强引用直到事件处理结束。
    mime = manager.encode_drag("tool", "alpha")
    drop = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    rail.dropEvent(drop)
    order = sorted((state for state in manager.states.values() if state.position == DockPosition.LEFT_TOP), key=lambda state: state.order)
    assert [state.id for state in order] == ["alpha", "peer"]
