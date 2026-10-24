"""灵感停靠侧栏的真实控件事件、原生拖放交接及多文件保存隔离。"""

import pytest
from mindmap_formats import load_document
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QDragEnterEvent, QDropEvent, QInputMethodEvent, QMouseEvent
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMessageBox


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
            point = canvas.view.mapFromScene(QPointF(*canvas.layout_result.nodes[e.root_id].center))
            enter = QDragEnterEvent(point, action, self.mime,
                                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(canvas.view.viewport(), enter)
            assert enter.isAccepted()
            drop = QDropEvent(QPointF(point), action, self.mime,
                              Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(canvas.view.viewport(), drop)
            assert drop.isAccepted()
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
    assert canvas.view.node_drag._reflow is not None
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert load_document(window.files.path) == e.document
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
            self.mime = mime

        def exec(self, action):
            point = canvas.view.mapFromScene(QPointF(*canvas.layout_result.nodes[e.root_id].center))
            enter = QDragEnterEvent(point, action, self.mime,
                                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(canvas.view.viewport(), enter)
            assert enter.isAccepted() and canvas.view.node_drag.external_active
            # 模拟 Windows 直接返回取消，不向画布补发 dragLeaveEvent。
            return Qt.DropAction.IgnoreAction

        def deleteLater(self):
            pass

    monkeypatch.setattr("mindmap_qt_panels.inspiration.QDrag", NativeDrag)
    panel.list_view.setCurrentRow(0)
    panel.list_view.startDrag(Qt.DropAction.MoveAction)
    assert not canvas.view.node_drag.active
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


def _open_item_menu(panel, qtbot, row=1, *, keyboard=False):
    """发送实际的列表右键或键盘菜单事件，不直接调用面板内部菜单入口。"""
    view = panel.list_view
    item = view.item(row)
    point = view.visualItemRect(item).center()
    if keyboard:
        view.setCurrentRow(row)
    else:
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=point)
    reason = QContextMenuEvent.Reason.Keyboard if keyboard else QContextMenuEvent.Reason.Mouse
    event = QContextMenuEvent(reason, point, view.viewport().mapToGlobal(point))
    QApplication.sendEvent(view.viewport(), event)
    assert event.isAccepted() and panel._menu.isVisible()


def _click_menu_action(panel, qtbot, action):
    """点击原生菜单项，覆盖 Qt 先隐藏菜单、再发出动作信号的真实顺序。"""
    qtbot.mouseClick(panel._menu, Qt.MouseButton.LeftButton,
                     pos=panel._menu.actionGeometry(action).center())


@pytest.mark.parametrize("keyboard", [False, True])
def test_context_menu_targets_clicked_idea_without_early_navigation(window, qtbot, monkeypatch, keyboard):
    e, canvas = window.engine, window.canvas
    roots = [e.add_inspiration("同名想法", position=(n * 300, 0)) for n in range(2)]
    canvas.show_inspiration(roots[0])
    panel = window.active_session.inspiration
    QApplication.processEvents()
    monkeypatch.setattr(window.manager, "tool_menu", lambda *_: pytest.fail("列表菜单不应冒泡到停靠层"))
    revision = e.revision
    _open_item_menu(panel, qtbot, keyboard=keyboard)
    assert [action.text() for action in panel._menu.actions()] == ["加入导图…", "", "删除想法"]
    assert panel._menu.defaultAction() is None
    assert canvas.current_inspiration_root_id == roots[0] and e.revision == revision
    assert panel._current_root == roots[0]
    assert panel.list_view.currentRow() == (1 if keyboard else 0)
    monkeypatch.setattr(canvas, "choose_inspiration_target", lambda: e.root_id)
    _click_menu_action(panel, qtbot, panel.attach_action)
    assert e.parent_id(roots[1]) == e.root_id and e.revision == revision + 1
    assert e.document.inspiration.root_ids == [roots[0]]
    assert panel._menu_root is None


@pytest.mark.parametrize("keyboard", [False, True])
def test_context_menu_cancel_and_empty_area_keep_selection(window, qtbot, monkeypatch, keyboard):
    e, canvas = window.engine, window.canvas
    first = e.add_inspiration("当前想法", position=(0, 0))
    e.add_inspiration("右键想法", position=(300, 0))
    canvas.show_inspiration(first)
    panel = window.active_session.inspiration
    QApplication.processEvents()
    monkeypatch.setattr(window.manager, "tool_menu", lambda *_: pytest.fail("空白菜单不应冒泡到停靠层"))
    original = e.document
    _open_item_menu(panel, qtbot, keyboard=keyboard)
    qtbot.keyClick(panel._menu, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: panel._menu_root is None)
    assert canvas.current_inspiration_root_id == first
    assert panel.list_view.currentRow() == (1 if keyboard else 0) and e.document == original
    for empty in (False, True):
        if empty:
            e.remove_nodes(list(e.document.inspiration.root_ids))
        receiver = panel.empty_label if empty else panel.list_view.viewport()
        point = receiver.rect().bottomRight() - QPoint(10, 10)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, receiver.mapToGlobal(point))
        QApplication.sendEvent(receiver, event)
        assert panel._empty_menu.isVisible() and panel._empty_menu.actions() == [panel.new_action]
        panel.close_popups()


def test_context_menu_delete_preserves_other_ideas_and_undo(window, qtbot, monkeypatch):
    e, canvas = window.engine, window.canvas
    first = e.add_inspiration("当前想法", position=(0, 0))
    target = e.add_inspiration("待删除想法", position=(300, 0))
    child = e.add_child(target, "子主题")
    detail = e.create_detail_page(child)
    canvas.show_inspiration(first)
    panel = window.active_session.inspiration
    QApplication.processEvents()
    original = e.document
    _open_item_menu(panel, qtbot)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    _click_menu_action(panel, qtbot, panel.delete_action)
    assert e.document == original
    _open_item_menu(panel, qtbot)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    _click_menu_action(panel, qtbot, panel.delete_action)
    assert target not in e.document.nodes and first in e.document.nodes
    assert detail not in e.document.detail_pages
    e.undo()
    assert e.document == original and e.get_node(child).detail_page_id == detail
    e.redo()
    assert target not in e.document.nodes


def test_context_menu_attach_moves_exact_root_with_owned_content(window, qtbot, monkeypatch):
    e, canvas = window.engine, window.canvas
    first = e.add_inspiration("当前想法", position=(0, 0))
    target = e.add_inspiration("待整理想法", position=(300, 0))
    child = e.add_child(target, "内容随行")
    detail = e.create_detail_page(child)
    canvas.show_inspiration(first)
    panel = window.active_session.inspiration
    QApplication.processEvents()
    original = e.document
    monkeypatch.setattr(canvas, "choose_inspiration_target", lambda: None)
    _open_item_menu(panel, qtbot)
    _click_menu_action(panel, qtbot, panel.attach_action)
    assert e.document == original
    canvas.show_inspiration(first)
    monkeypatch.setattr(canvas, "choose_inspiration_target", lambda: e.root_id)
    _open_item_menu(panel, qtbot)
    _click_menu_action(panel, qtbot, panel.attach_action)
    assert e.parent_id(target) == e.root_id
    assert e.document.inspiration.root_ids == [first]
    assert e.get_node(child).detail_page_id == detail
    e.undo()
    assert e.document == original
    e.redo()
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert load_document(window.files.path) == e.document


@pytest.mark.parametrize("keyboard", [False, True])
def test_context_menu_readonly_composition_and_stale_session_are_guarded(window, qtbot, tmp_path, keyboard):
    session = window.active_session
    e, canvas, panel = session.engine, session.canvas, session.inspiration
    first = e.add_inspiration("当前想法", position=(0, 0))
    target = e.add_inspiration("受保护想法", position=(300, 0))
    canvas.show_inspiration(first)
    QApplication.processEvents()
    canvas.set_editable(False)
    _open_item_menu(panel, qtbot)
    assert not any(action.isEnabled() for action in (panel.attach_action, panel.delete_action))
    panel.close_popups()

    def navigate(row):
        """通过仍保留的列表单击或 Enter 导航，覆盖只读浏览与输入法保护。"""
        if keyboard:
            panel.list_view.setCurrentRow(row)
            qtbot.keyClick(panel.list_view, Qt.Key.Key_Return)
        else:
            point = panel.list_view.visualItemRect(panel.list_view.item(row)).center()
            qtbot.mouseClick(panel.list_view.viewport(), Qt.MouseButton.LeftButton, pos=point)

    original, revision = e.document, e.revision
    navigate(1)
    assert canvas.current_inspiration_root_id == target
    assert e.document == original and e.revision == revision
    canvas.set_editable(True)
    canvas.begin_edit(target)
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("输入中", []))
    assert session.is_composing
    _open_item_menu(panel, qtbot, row=0)
    assert not any(action.isEnabled() for action in (panel.attach_action, panel.delete_action))
    panel.close_popups()
    navigate(0)
    assert canvas.current_inspiration_root_id == target and canvas.is_composing
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent())
    canvas.finish_edit(commit=False)
    _open_item_menu(panel, qtbot)
    original = e.document
    assert window.new_document(tmp_path / "另一个文件.mindmap")
    assert not panel._menu.isVisible() and panel._menu_root is None
    panel.delete_action.trigger()
    panel.delete_requested.emit(target)
    assert e.document == original and not window.engine.document.inspiration.root_ids


def test_context_menu_invalidates_when_target_is_removed_or_document_replaced(window, qtbot):
    e, canvas = window.engine, window.canvas
    first = e.add_inspiration("保留", position=(0, 0))
    target = e.add_inspiration("过期目标", position=(300, 0))
    canvas.show_inspiration(first)
    panel = window.active_session.inspiration
    QApplication.processEvents()
    _open_item_menu(panel, qtbot)
    e.remove_nodes([target])
    assert not panel._menu.isVisible() and panel._menu_root is None
    e.undo()
    _open_item_menu(panel, qtbot)
    e.replace_document(e.document)
    assert not panel._menu.isVisible() and panel._menu_root is None
    panel.delete_action.trigger()
    assert target in e.document.nodes
