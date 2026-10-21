"""停靠交互回归：位置、模式、焦点、拖放边界和 QWidget 生命周期。"""

import copy

import pytest
from mindmap_docking import DockPosition, DockTarget, ViewMode
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTabWidget


@pytest.mark.parametrize("position", list(DockPosition))
@pytest.mark.parametrize("mode", list(ViewMode))
def test_six_positions_and_three_modes_keep_content(docking, qtbot, position, mode):
    """六个目标与三种模式的组合必须可达，迁移不能替换业务内容。"""
    manager, host = docking.manager, docking.host
    assert docking.calls == {}
    manager.move("alpha", DockTarget(position))
    content = manager.panel("alpha").content
    content.setPlainText("模式切换中保存的文本")
    manager.set_view_mode("alpha", mode)
    qtbot.waitUntil(lambda: manager.states["alpha"].visible)
    assert manager.states["alpha"].position == position
    assert manager.states["alpha"].mode == mode
    assert manager.panel("alpha").content is content
    assert content.toPlainText() == "模式切换中保存的文本"
    assert docking.calls == {"alpha": 1}
    panel = manager.panel("alpha")
    if mode in (ViewMode.DOCK_PINNED, ViewMode.DOCK_UNPINNED):
        assert host.zones[position].currentWidget() is panel
        assert host.zones[position].isVisible()
    elif mode == ViewMode.UNDOCK:
        assert host.overlays["alpha"].isVisible()
        assert not host.zones[position].isVisible()


def test_same_zone_is_exclusive_but_two_groups_coexist(docking):
    manager, host = docking.manager, docking.host
    manager.activate("alpha", focus=False)
    manager.activate("beta", focus=False)
    assert manager.states["alpha"].visible
    assert manager.states["beta"].visible
    manager.activate("peer", focus=False)
    assert not manager.states["alpha"].visible
    assert manager.states["peer"].visible
    assert manager.states["beta"].visible
    assert host.zones[DockPosition.LEFT_TOP].currentWidget() is manager.panel("peer")
    assert host.zones[DockPosition.LEFT_BOTTOM].currentWidget() is manager.panel("beta")


def test_reorder_and_notification_preserve_single_factory(docking):
    manager = docking.manager
    manager.specs["alpha"].notification = True
    manager.activate("alpha")
    content = manager.panel("alpha").content
    content.setPlainText("只创建一次")
    assert not manager.specs["alpha"].notification
    manager.move("peer", DockTarget(DockPosition.LEFT_TOP, index=0))
    manager.move("alpha", DockTarget(DockPosition.LEFT_TOP, index=0))
    peers = sorted((s for s in manager.states.values() if s.position == DockPosition.LEFT_TOP),
                   key=lambda state: state.order)
    assert [state.id for state in peers] == ["alpha", "peer"]
    manager.hide("alpha")
    manager.activate("alpha")
    assert manager.panel("alpha").content is content
    assert content.toPlainText() == "只创建一次"
    assert docking.calls["alpha"] == 1


def test_overlay_does_not_reduce_editor_geometry(docking, qtbot):
    manager, host = docking.manager, docking.host
    QApplication.processEvents()
    before = host.editor.geometry()
    manager.set_view_mode("alpha", ViewMode.UNDOCK)
    manager.activate("alpha")
    qtbot.waitUntil(lambda: host.overlays["alpha"].isVisible())
    QApplication.processEvents()
    assert host.editor.geometry() == before
    manager.activate("beta")
    assert not manager.states["alpha"].visible
    assert manager.states["beta"].visible


def test_widescreen_and_side_by_side_change_geometry(docking, qtbot):
    manager, host = docking.manager, docking.host
    for tool_id in ("alpha", "beta", "gamma"):
        manager.activate(tool_id, focus=False)
    QApplication.processEvents()
    old_left_height, old_bottom_width = host.left.height(), host.bottom.width()
    manager.set_option("widescreen", True)
    QApplication.processEvents()
    assert host.left.height() > old_left_height
    assert host.bottom.width() < old_bottom_width
    manager.set_option("left_side_by_side", True)
    QApplication.processEvents()
    assert host.left.orientation() == Qt.Orientation.Horizontal
    assert host.right.orientation() == Qt.Orientation.Vertical
    top = host.zones[DockPosition.LEFT_TOP]
    bottom = host.zones[DockPosition.LEFT_BOTTOM]
    assert top.x() < bottom.x()
    assert abs(top.y() - bottom.y()) <= 1





def test_unpinned_owns_popup_until_editor_receives_focus(docking, qtbot):
    """真实 QMenu 弹出期间不得误隐藏；真正进入编辑器后释放停靠空间。"""
    manager = docking.manager
    manager.set_view_mode("alpha", ViewMode.DOCK_UNPINNED)
    manager.activate("alpha")
    panel = manager.panel("alpha")
    qtbot.waitUntil(lambda: QApplication.focusWidget() is panel.content)
    menu = manager.tool_menu("alpha", panel)
    menu.popup(panel.mapToGlobal(QPoint(20, 20)))
    qtbot.waitUntil(lambda: QApplication.activePopupWidget() is menu)
    QApplication.processEvents()
    assert manager.states["alpha"].visible
    menu.close()
    docking.window.activateWindow()
    docking.document.setFocus()
    qtbot.waitUntil(lambda: not manager.states["alpha"].visible)
    assert not docking.host.zones[DockPosition.LEFT_TOP].isVisible()
    menu.deleteLater()





def test_group_tabs_switches_header_and_cycles_existing_pages(docking):
    def pages():
        tabs = QTabWidget()
        tabs.addTab(QPlainTextEdit("第一页"), "项目")
        tabs.addTab(QPlainTextEdit("第二页"), "范围")
        return tabs

    docking.register("views", DockPosition.RIGHT_TOP, factory=pages)
    manager = docking.manager
    manager.activate("views", focus=False)
    panel = manager.panel("views")
    page = panel.content.widget(0)
    assert panel.tabs.isVisible()
    manager.set_grouped_tabs("views", True)
    assert not panel.tabs.isVisible()
    assert panel.title_button.dropdown
    manager.cycle_content(1)
    assert panel.content.currentIndex() == 1
    manager.cycle_content(1)
    assert panel.content.currentWidget() is page
    manager.set_grouped_tabs("views", False)
    assert panel.tabs.isVisible()
    assert docking.calls["views"] == 1


def test_mime_rejects_foreign_session_and_malformed_payload(docking, system_factory):
    manager = docking.manager
    foreign = system_factory().manager
    mime = manager.encode_drag("tool", "alpha")
    assert manager.decode_drag(mime)["id"] == "alpha"
    assert foreign.decode_drag(mime) is None
    for raw in (b"{broken", b"[]", b"null"):
        invalid = QMimeData()
        invalid.setData("application/x-mindmap-dock-v1", raw)
        assert manager.decode_drag(invalid) is None
    assert manager.decode_drag(manager.encode_drag("tool", "unknown")) is None
    assert manager.decode_drag(QMimeData()) is None


def test_invalid_target_is_atomic(docking):
    manager = docking.manager
    manager.activate("alpha", focus=False)
    content = manager.panel("alpha").content
    before = copy.deepcopy(manager.capture_layout())
    with pytest.raises(ValueError):
        manager.move("alpha", DockTarget(DockPosition.RIGHT_BOTTOM, host_id="不存在"))
    assert manager.capture_layout() == before
    assert manager.panel("alpha").content is content


@pytest.mark.parametrize("x,y,expected", [
    (0.1, 0.2, DockPosition.LEFT_TOP), (0.1, 0.8, DockPosition.LEFT_BOTTOM),
    (0.9, 0.2, DockPosition.RIGHT_TOP), (0.9, 0.8, DockPosition.RIGHT_BOTTOM),
    (0.4, 0.9, DockPosition.BOTTOM_LEFT), (0.6, 0.9, DockPosition.BOTTOM_RIGHT),
    (0.5, 0.4, "editor"),
])
def test_qt_drop_events_commit_to_expected_target(docking, x, y, expected):
    """通过 Qt 事件通路检验目标计算与提交；不把此测试冒充真实鼠标拖动。"""
    host, manager = docking.host, docking.manager
    point = host.canvas.mapTo(host, QPoint(int(host.canvas.width() * x),
                                          int(host.canvas.height() * y)))
    mime = manager.encode_drag("tool", "alpha")
    enter = QDragEnterEvent(point, Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(host, enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(host, drop)
    assert drop.isAccepted()
    assert manager.states["alpha"].position == expected
    assert manager.states["alpha"].visible


def test_foreign_drop_event_leaves_layout_unchanged(docking, system_factory):
    other = system_factory()
    manager, host = docking.manager, docking.host
    before = copy.deepcopy(manager.capture_layout())
    event = QDropEvent(QPointF(100, 100), Qt.DropAction.MoveAction,
                       other.manager.encode_drag("tool", "alpha"),
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(host, event)
    assert not event.isAccepted()
    assert manager.capture_layout() == before


def test_cancelled_native_drag_rolls_back_without_detaching(docking, monkeypatch):
    """驱动原生拖动的取消返回路径，验证释放位置再远也不能提交已取消事务。"""
    from mindmap_docking import manager as manager_module

    manager = docking.manager
    manager.activate("alpha", focus=False)
    before = copy.deepcopy(manager.capture_layout())

    class CancelledDrag:
        def __init__(self, source):
            self.source = source

        def setMimeData(self, mime):
            assert manager.decode_drag(mime)["id"] == "alpha"

        def setPixmap(self, pixmap):
            pass

        def setHotSpot(self, hotspot):
            pass

        def exec(self, action):
            return Qt.DropAction.IgnoreAction

        def deleteLater(self):
            pass

    monkeypatch.setattr(manager_module, "QDrag", CancelledDrag)
    manager.start_tool_drag("alpha", manager.panel("alpha").header, QPoint(5, 5))
    assert manager.capture_layout() == before
    assert not manager._dragging


def test_unknown_tool_and_duplicate_registration_fail_without_replacement(docking):
    manager = docking.manager
    with pytest.raises(KeyError):
        manager.activate("missing")
    with pytest.raises(ValueError):
        docking.register("alpha")
    assert len(manager.states) == 4
