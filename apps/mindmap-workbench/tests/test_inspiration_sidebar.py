"""灵感停靠侧栏的真实控件事件、原生拖放交接及多文件保存隔离。"""

import pytest
from mindmap_formats import load_document
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QAbstractItemView, QApplication


@pytest.fixture
def window(qtbot, tmp_path):
    widget = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(widget)
    widget.resize(1300, 850)
    widget.show()
    assert widget.new_document(tmp_path / "灵感测试.mindmap")
    QApplication.processEvents()
    yield widget
    widget.close()


def test_single_selection_search_and_canvas_navigation(window, qtbot):
    canvas, e = window.canvas, window.engine
    a = e.add_inspiration("阅读计划", position=(-400, 0))
    child = e.add_child(a, "一章")
    e.create_detail_page(child)
    b = e.add_inspiration("工作安排", position=(600, 0))
    canvas.show_inspiration(a)
    panel = window.active_session.inspiration
    QApplication.processEvents()
    assert panel.isVisible()
    assert panel.list_view.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert panel.list_view.count() == 2
    assert "1 个子主题" in panel.list_view.item(0).text()
    point = panel.list_view.visualItemRect(panel.list_view.item(1)).center()
    qtbot.mousePress(panel.list_view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.current_inspiration_root_id == a
    qtbot.mouseRelease(panel.list_view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.current_inspiration_root_id == b and set(canvas.node_items) == {b}
    panel.search.setText("阅读")
    assert not panel.list_view.item(0).isHidden() and panel.list_view.item(1).isHidden()
    panel.search.setText("不存在")
    assert panel.empty_label.isVisible() and "没有匹配" in panel.empty_label.text()
    panel.search.clear()
    assert not hasattr(panel, "overview_button") and not hasattr(panel, "overview_requested")
    assert not hasattr(canvas.path_navigation, "inspiration_button")
    assert set(canvas.node_items) == {b}
    assert canvas.action("move_to_inspiration").text() == "转为独立想法"
    canvas.navigate_to(None)
    QApplication.processEvents()
    assert panel.isVisible()
    e.update_node(a, text="改名同步")
    assert "改名同步" in panel.list_view.item(0).text()


@pytest.mark.parametrize("cancel", [False, True])
def test_canvas_mouse_drag_hands_off_to_sidebar_once(window, qtbot, monkeypatch, cancel):
    canvas, e = window.canvas, window.engine
    root = e.add_child(e.root_id, "要拆出")
    child = e.add_child(root, "保留孩子")
    detail = e.create_detail_page(child)
    panel = window.active_session.inspiration
    window.manager.activate("inspiration", focus=False)
    canvas.view.setFocus()
    QApplication.processEvents()
    canvas.select_nodes([root])
    before, revision = e.document, e.revision
    invocations = []

    class NativeDrag:
        def __init__(self, source):
            self.mime = None

        def setMimeData(self, mime):
            self.mime = mime

        def exec(self, action):
            invocations.append(action)
            assert canvas.view.drag_ghost is None
            assert canvas.view.scene().mouseGrabberItem() is None
            if cancel:
                return Qt.DropAction.IgnoreAction
            point = panel.rect().center()
            enter = QDragEnterEvent(point, action, self.mime,
                                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(panel, enter)
            assert enter.isAccepted()
            drop = QDropEvent(QPointF(point), action, self.mime,
                              Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(panel, drop)
            assert drop.isAccepted()
            return action

        def deleteLater(self):
            pass

    monkeypatch.setattr("mindmap_qt_canvas.transfers.QDrag", NativeDrag)
    origin = canvas.view.mapFromScene(QPointF(*canvas.layout_result.nodes[root].center))
    qtbot.mousePress(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    assert canvas.view.press_node == root
    outside = canvas.view.viewport().mapFromGlobal(panel.mapToGlobal(panel.rect().center()))
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(outside),
                       QPointF(canvas.view.viewport().mapToGlobal(outside)), Qt.MouseButton.NoButton,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas.view.viewport(), move)
    qtbot.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    assert invocations == [Qt.DropAction.MoveAction]
    assert canvas.current_page_id is None
    assert not canvas.view.node_drag.active and canvas._native_branch_drag is None
    if cancel:
        assert e.document == before and e.revision == revision
    else:
        assert e.revision == revision + 1 and panel.list_view.count() == 1
        assert e.get_node(child).detail_page_id == detail
        assert root in e.document.inspiration.root_ids
        e.undo()
        assert e.document == before and root in canvas.node_items
        e.redo()
        assert canvas.current_page_id is None and root not in canvas.node_items


def test_sidebar_drag_does_not_navigate_and_accepts_main_target(window, qtbot, monkeypatch):
    canvas, e = window.canvas, window.engine
    root = e.add_inspiration("拖入主图", position=(400, 20))
    child = e.add_child(root, "孩子")
    detail = e.create_detail_page(child)
    panel = window.active_session.inspiration
    window.manager.activate("inspiration", focus=False)
    original, revision = e.document, e.revision
    used = []

    class NativeDrag:
        def __init__(self, source):
            self.mime = None

        def setMimeData(self, mime):
            self.mime = mime

        def exec(self, action):
            assert canvas.current_page_id is None
            used.append(True)
            assert canvas.receive_branch_drag(self.mime, e.root_id)
            return action

        def deleteLater(self):
            pass

    monkeypatch.setattr("mindmap_qt_panels.inspiration.QDrag", NativeDrag)
    panel.list_view.setCurrentRow(0)
    panel.list_view.startDrag(Qt.DropAction.MoveAction)
    assert used == [True] and canvas.current_page_id is None
    assert e.revision == revision + 1 and e.parent_id(root) == e.root_id
    assert e.get_node(child).detail_page_id == detail and child in canvas.node_items
    assert panel.list_view.count() == 0
    e.undo()
    assert e.document == original and canvas.current_page_id is None
    assert panel.list_view.count() == 1
    e.redo()
    assert panel.list_view.count() == 0 and child in canvas.node_items


def test_drag_autosave_and_multifile_sidebar_binding(window, tmp_path, qtbot):
    session = window.active_session
    canvas, e, panel = session.canvas, session.engine, session.inspiration
    root = e.add_child(e.root_id, "只属于第一份")
    canvas.select_nodes([root])
    assert canvas.detach_branch_drag(canvas.create_branch_drag())
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert load_document(session.files.path) == e.document
    canvas.show_inspiration(root)
    assert window.new_document(tmp_path / "另一份.mindmap")
    assert window.active_session.inspiration.list_view.count() == 0
    window.manager.activate_document(session.id)
    assert window._inspiration_stack.currentWidget() is panel
    assert window.canvas.current_inspiration_root_id == root
    e.undo()
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert root in load_document(session.files.path).root.children


def test_list_ctrl_click_keeps_single_selection(window, qtbot):
    e, canvas = window.engine, window.canvas
    roots = [e.add_inspiration(str(n), position=(n * 200, 0)) for n in range(3)]
    canvas.show_inspiration(roots[0])
    panel = window.active_session.inspiration
    QApplication.processEvents()
    for row in (0, 1, 2):
        point = panel.list_view.visualItemRect(panel.list_view.item(row)).center()
        qtbot.mouseClick(panel.list_view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, pos=point)
    assert len(panel.list_view.selectedItems()) <= 1
    assert canvas.current_inspiration_root_id == roots[-1]
    # 侧栏不提供整理菜单或批量动作；画布的多选操作仍由既有动作处理。
    assert panel.findChildren(QAbstractItemView) == [panel.list_view]


def test_native_cancel_without_leave_clears_preview(window, monkeypatch):
    canvas, e = window.canvas, window.engine
    e.add_inspiration("取消的想法", position=(0, 0))
    panel = window.active_session.inspiration
    original, revision = e.document, e.revision

    class NativeDrag:
        def __init__(self, source):
            pass

        def setMimeData(self, mime):
            pass

        def exec(self, action):
            canvas.view._show_branch_drop_preview(e.root_id)
            assert canvas.view._branch_drop_preview is not None
            # 模拟 Windows 直接返回取消，不向画布补发 dragLeaveEvent。
            return Qt.DropAction.IgnoreAction

        def deleteLater(self):
            pass

    monkeypatch.setattr("mindmap_qt_panels.inspiration.QDrag", NativeDrag)
    panel.list_view.setCurrentRow(0)
    panel.list_view.startDrag(Qt.DropAction.MoveAction)
    assert canvas.view._branch_drop_preview is None
    assert e.document == original and e.revision == revision


def test_autosave_during_drag_keeps_payload_valid(window, qtbot):
    canvas, e = window.canvas, window.engine
    root = e.add_inspiration("保存不打断拖动", position=(300, 0))
    mime = canvas.create_branch_drag([root], origin="sidebar")
    revision = e.revision
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert e.revision > revision
    assert canvas.can_receive_branch_drag(mime, e.root_id)
    assert canvas.receive_branch_drag(mime, e.root_id)
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert root in load_document(window.files.path).root.children
