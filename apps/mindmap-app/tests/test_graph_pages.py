"""从产品入口验证图与树共用画布、编辑命令和停靠侧栏。"""

import pytest
from mindmap_app.app import build_window
from mindmap_core import GraphEdge, MindMapError
from mindmap_core.formats import load_document
from mindmap_core.formats.graph import format_graph_text, parse_graph_text
from mindmap_core.node_pages import graph_page_id
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
)

SAMPLE = '节点 a "入口"\n节点 b "服务"\na -> b'
THREE_NODES = '节点 a "入口"\n节点 b "服务"\n节点 c "依赖"'


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.resize(1200, 800)
    result.show()
    assert result.new_document(tmp_path / "主体.mindmap")
    return result


def _create_graph(window, text=SAMPLE):
    canvas = window.application.canvas
    canvas.action("node_graph").trigger()
    assert canvas.current_page_kind == "graph"
    if text is not None:
        canvas.apply_graph_text(text)
    return canvas


def _node_point(canvas, key):
    return canvas.view.mapFromScene(canvas.node_items[key].sceneBoundingRect().center())


def _edge_point(canvas, edge):
    path = canvas.branch_items[edge.key].path()
    return canvas.view.mapFromScene(path.pointAtPercent(0.5))


def _pointer_event(canvas, kind, position, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
    """通过视口发送鼠标事件，显式指定按钮状态，避免依赖系统鼠标所在窗口。"""
    viewport = canvas.view.viewport()
    event = QMouseEvent(kind, QPointF(position), QPointF(viewport.mapToGlobal(position)),
                        button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, event)


def _outlet_point(canvas, key):
    """按节点实际边界寻找出线把手，途经节点与把手之间的空隙。"""
    view = canvas.view
    rect = canvas.node_items[key].sceneBoundingRect()
    edge = view.mapFromScene(QPointF(rect.right() - 2, rect.center().y()))
    _pointer_event(canvas, QEvent.Type.MouseMove, _node_point(canvas, key))
    _pointer_event(canvas, QEvent.Type.MouseMove, edge + QPoint(4, 0))
    for dx in range(5, 26):
        point = edge + QPoint(dx, 0)
        handle = view.graph_edge_edit.handle_at(point)
        if handle is not None and handle.original is None and handle.fixed_node == key:
            return handle.center.toPoint()
    pytest.fail("节点右侧未显示出线把手")


def _endpoint_point(canvas, edge, *, source=False):
    return next(handle.center.toPoint() for handle in canvas.view.graph_edge_edit._handles()
                if handle.original == edge and handle.moving_source == source)


def _drag_edge(canvas, start, end, *, cancel=None):
    """从真实视口发送按下、移动、释放。取消事件插在预览和释放之间。"""
    view = canvas.view
    button = Qt.MouseButton.LeftButton
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, button, button)
    assert view.graph_edge_edit.active
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=button)
    if cancel is not None:
        cancel()
        assert not view.graph_edge_edit.active
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, button)
    assert not view.graph_edge_edit.active


def test_graph_uses_same_scene_and_view_with_badge_and_recent_navigation(window, qtbot):
    canvas = window.application.canvas
    qtbot.waitUntil(lambda: not canvas._view_reset_pending)
    owner = canvas.engine.add_child(canvas.engine.root_id, "依赖关系")
    canvas.select_nodes([owner])
    canvas.view.zoom(1.2)
    viewport, scene = canvas.view, canvas.scene
    before = canvas.capture_page_view_state()
    assert canvas.action("node_graph").text() == "添加图"
    _create_graph(window)
    assert canvas.view is viewport and canvas.scene is scene
    assert canvas.path_navigation.full_path.endswith("依赖关系 › 图")
    assert window.documents.active.document.recent_pages.entries[0].kind == "graph"
    canvas.fit_to_view()
    graph_state = canvas.capture_page_view_state()
    canvas.path_navigation.back_button.click()
    assert canvas.action("node_graph").text() == "打开图"
    assert canvas.selected_node_ids() == [owner]
    assert canvas.zoom_factor == pytest.approx(before["zoom"])
    badge = canvas.node_items[owner].graph_control
    canvas.view.ensureVisible(badge, 40, 40)
    qtbot.mouseClick(viewport.viewport(), Qt.MouseButton.LeftButton,
                     pos=viewport.mapFromScene(badge.sceneBoundingRect().center()))
    assert canvas.current_page_id == graph_page_id(owner)
    assert canvas.capture_page_view_state()["zoom"] == graph_state["zoom"]
    canvas.return_to_parent_page()
    window.documents.active._navigate_recent_page(graph_page_id(owner), None)
    assert canvas.current_page_id == graph_page_id(owner) and canvas.view is viewport


def test_text_editor_reports_lines_and_keeps_draft_then_submits_once(window):
    canvas = _create_graph(window)
    canvas.set_graph_hidden_nodes({"a"})
    revision = canvas.engine.revision
    failures = []

    def operate():
        dialog = QApplication.activeModalWidget()
        try:
            editor = dialog.findChild(QPlainTextEdit, "graph_text_editor")
            buttons = dialog.findChild(QDialogButtonBox)
            apply = next(button for button in buttons.buttons() if button.text() == "应用")
            draft = '节点 a "待修正"\na -> missing'
            editor.setPlainText(draft)
            apply.click()
            assert editor.toPlainText() == draft
            assert "第 2 行" in dialog.findChild(QLabel, "graph_text_error").text()
            assert canvas.engine.revision == revision and len(canvas.graph.nodes) == 2
            editor.setPlainText('节点 a "已修正"\na -> a')
            apply.click()
        except BaseException as error:
            failures.append(error)
            dialog.reject()

    QTimer.singleShot(0, operate)
    canvas.action("graph_text").trigger()
    assert not failures, failures
    assert canvas.engine.revision == revision + 1
    assert [node.title for node in canvas.graph.nodes] == ["已修正"]
    assert canvas.graph.hidden_node_ids == {"a"}
    assert canvas.graph.edges == (GraphEdge("a", "a"),)
    canvas.action("undo").trigger()
    assert canvas.graph.edges == (GraphEdge("a", "b"),)


def test_keyboard_inline_edit_and_clipboard_use_graph_ids(window, qtbot):
    canvas = _create_graph(window, None)
    canvas.view.setFocus()
    qtbot.keyClick(canvas.view, Qt.Key.Key_Tab)
    assert canvas.is_editing and len(canvas.graph.nodes) == 1
    QApplication.sendEvent(canvas.view, QKeyEvent(QEvent.Type.KeyPress, 0, Qt.KeyboardModifier.NoModifier, "甲"))
    qtbot.keyClick(canvas.view, Qt.Key.Key_Return)
    qtbot.keyClick(canvas.view, Qt.Key.Key_Tab)
    assert canvas.is_editing
    qtbot.keyClick(canvas.view, Qt.Key.Key_Return)
    assert canvas.graph.edges == (GraphEdge("n1", "n2"),)
    qtbot.keyClick(canvas.view, Qt.Key.Key_Enter)
    assert canvas.is_editing and len(canvas.graph.nodes) == 3
    qtbot.keyClick(canvas.view, Qt.Key.Key_Return)
    canvas.select_nodes(["n1", "n2"])
    window.action("app.copy").trigger()
    fragment = parse_graph_text(QApplication.clipboard().text())
    assert fragment.edges == (GraphEdge("n1", "n2"),)
    assert fragment.nodes[0].title == "甲"
    window.action("app.paste").trigger()
    assert canvas.selected_node_ids() == ["n4", "n5"]
    assert GraphEdge("n4", "n5") in canvas.graph.edges
    qtbot.keyClick(canvas.view, Qt.Key.Key_Delete)
    assert [node.id for node in canvas.graph.nodes] == ["n1", "n2", "n3"]
    assert canvas.graph.edges == (GraphEdge("n1", "n2"),)
    window.action("app.undo").trigger()
    assert GraphEdge("n4", "n5") in canvas.graph.edges
    window.action("app.cut").trigger()
    assert [node.id for node in canvas.graph.nodes] == ["n1", "n2", "n3"]


def test_add_edge_dialog_and_edge_context_menu(window, qtbot):
    canvas = _create_graph(window, '节点 a "入口"\n节点 b "服务"')
    canvas.select_nodes(["a", "b"])
    choices = []

    def add():
        dialog = QApplication.activeModalWidget()
        source, target = dialog.findChildren(QComboBox)
        choices.extend((source.currentData(), target.currentData()))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()

    QTimer.singleShot(0, add)
    canvas.action("graph_add_edge").trigger()
    assert choices == ["a", "b"]
    assert canvas.graph.edges == (GraphEdge("a", "b"),)
    canvas.fit_to_view()
    path = canvas.branch_items["a->b"].path()
    pos = canvas.view.mapFromScene(path.pointAtPercent(0.5))

    actions = []

    def remove():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            actions.extend(action.text() for action in menu.actions())
            qtbot.keyClick(menu, Qt.Key.Key_Down)
            qtbot.keyClick(menu, Qt.Key.Key_Return)

    QTimer.singleShot(0, remove)
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.RightButton, pos=pos)
    assert actions == ["删除连线"]
    assert not canvas.graph.edges


def test_canvas_add_reconnect_both_endpoints_and_delete_with_undo(window, qtbot):
    canvas = _create_graph(window, THREE_NODES)
    view = canvas.view
    controls = view.graph_edge_edit
    viewport = view.viewport()
    canvas.center_root()
    before = canvas.engine.revision
    start = _outlet_point(canvas, "a")
    end = _node_point(canvas, "b")
    baseline = QImage(viewport.size(), QImage.Format.Format_ARGB32)
    viewport.render(baseline)
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=Qt.MouseButton.LeftButton)
    # 检查目标节点附近实际出现高亮，同时场景及引擎仍只有正式数据。
    preview = QImage(viewport.size(), QImage.Format.Format_ARGB32)
    viewport.render(preview)
    target_rect = view.mapFromScene(canvas.node_items["b"].sceneBoundingRect()).boundingRect().adjusted(-6, -6, 6, 6)
    assert preview.copy(target_rect) != baseline.copy(target_rect)
    assert controls.active and not canvas.branch_items and canvas.engine.revision == before
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton)
    edge = GraphEdge("a", "b")
    assert canvas.graph.edges == (edge,) and canvas.engine.revision == before + 1
    assert canvas.layout_result.nodes["a"].y < canvas.layout_result.nodes["b"].y

    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=_node_point(canvas, "c"))
    assert controls.selected_edge is None and canvas.selected_node_ids() == ["c"]
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=_edge_point(canvas, edge))
    assert controls.selected_edge == edge and not canvas.selected_node_ids()
    assert not canvas.action("copy").isEnabled() and canvas.action("delete").isEnabled()
    _drag_edge(canvas, _endpoint_point(canvas, edge, source=True), _node_point(canvas, "c"))
    moved_source = GraphEdge("c", "b")
    assert canvas.graph.edges == (moved_source,) and controls.selected_edge == moved_source
    assert canvas.engine.revision == before + 2

    _drag_edge(canvas, _endpoint_point(canvas, moved_source), _node_point(canvas, "a"))
    moved_target = GraphEdge("c", "a")
    assert canvas.graph.edges == (moved_target,) and canvas.engine.revision == before + 3
    assert "c -> a" in format_graph_text(canvas.graph)
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert not canvas.graph.edges and len(canvas.graph.nodes) == 3
    assert controls.selected_edge is None and canvas.engine.revision == before + 4
    for edges in ((moved_target,), (moved_source,), (edge,), ()):
        canvas.action("undo").trigger()
        assert canvas.graph.edges == edges
    for edges in ((edge,), (moved_source,), (moved_target,), ()):
        canvas.action("redo").trigger()
        assert canvas.graph.edges == edges


def test_canvas_edge_invalid_drops_self_loop_and_reverse_edge(window, qtbot):
    canvas = _create_graph(window, THREE_NODES + "\na -> b\na -> c")
    view = canvas.view
    viewport = view.viewport()
    canvas.center_root()
    initial = canvas.graph
    revision = canvas.engine.revision
    blank = QPoint(24, 24)
    _drag_edge(canvas, _outlet_point(canvas, "a"), _node_point(canvas, "b"))
    _drag_edge(canvas, _outlet_point(canvas, "b"), blank)
    _drag_edge(canvas, _outlet_point(canvas, "b"), QPoint(-30, -30))
    _drag_edge(canvas, _outlet_point(canvas, "b"), _node_point(canvas, "a"),
               cancel=lambda: qtbot.keyClick(view, Qt.Key.Key_Escape))
    edge = GraphEdge("a", "b")
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=_edge_point(canvas, edge))
    for target in ("b", "c"):
        _drag_edge(canvas, _endpoint_point(canvas, edge), _node_point(canvas, target))
        assert view.graph_edge_edit.selected_edge == edge
    assert canvas.graph == initial and canvas.engine.revision == revision
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=blank)
    assert view.graph_edge_edit.selected_edge is None

    _drag_edge(canvas, _outlet_point(canvas, "b"), _node_point(canvas, "a"))
    assert canvas.graph.edges == (*initial.edges, GraphEdge("b", "a"))
    _drag_edge(canvas, _outlet_point(canvas, "a"), _node_point(canvas, "a"))
    assert canvas.graph.edges[-1] == GraphEdge("a", "a")
    assert canvas.engine.revision == revision + 2


def test_small_zoom_separates_endpoint_hit_areas(window, qtbot):
    canvas = _create_graph(window, '节点 a "入口"\n节点 b "服务"\na -> a')
    view = canvas.view
    edge = GraphEdge("a", "a")
    view.zoom(0.1)
    canvas.center_root()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_edge_point(canvas, edge))
    controls = view.graph_edge_edit
    assert controls.selected_edge == edge
    for source in (True, False):
        point = _endpoint_point(canvas, edge, source=source)
        assert controls.handle_at(point).moving_source is source
    source = _endpoint_point(canvas, edge, source=True)
    # 放大后再次测量命中，保留屏幕像素热区并在正常倍率改接这条自环。
    view.zoom(10)
    canvas.center_root()
    assert _endpoint_point(canvas, edge, source=True) != source
    _drag_edge(canvas, _endpoint_point(canvas, edge, source=True), _node_point(canvas, "b"))
    assert canvas.graph.edges == (GraphEdge("b", "a"),)


@pytest.mark.parametrize("interruption", ["捕获丢失", "只读", "离开图页", "图已更新"])
def test_interrupted_edge_drag_cannot_commit_on_release(window, interruption):
    canvas = _create_graph(window, THREE_NODES)
    view = canvas.view
    canvas.center_root()
    revision = canvas.engine.revision
    owner = canvas.engine.root_id

    def interrupt():
        if interruption == "捕获丢失":
            QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.UngrabMouse))
        elif interruption == "只读":
            canvas.set_editable(False)
        elif interruption == "离开图页":
            canvas.return_to_parent_page()
        else:
            canvas.apply_graph_text(THREE_NODES.replace("依赖", "模块"))

    _drag_edge(canvas, _outlet_point(canvas, "a"), _node_point(canvas, "b"), cancel=interrupt)
    assert not canvas.engine.node_graph(owner).edges
    assert canvas.engine.revision == revision + (interruption == "图已更新")
    if interruption == "只读":
        point = _node_point(canvas, "a") + QPoint(35, 0)
        _pointer_event(canvas, QEvent.Type.MouseMove, _node_point(canvas, "a"))
        assert view.graph_edge_edit.handle_at(point) is None


def test_manual_edges_sync_split_and_save_while_filter_remains_local(window, qtbot):
    first = window.documents.active
    canvas = _create_graph(window, THREE_NODES + "\na -> b")
    edge = GraphEdge("a", "b")
    canvas.select_nodes(["a"])
    start = _node_point(canvas, "a")
    end = start + QPoint(-40, 16)
    button = Qt.MouseButton.LeftButton
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, button, button)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=button)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, button)
    manual_positions = canvas.graph.positions
    assert set(manual_positions) == {"a"}
    canvas.set_graph_hidden_nodes({"a"})
    canvas.center_root()
    assert first.duplicate()
    second = window.documents.active
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    other = second.canvas
    # 分屏初建时控件仍是占位尺寸，先等布局完成再使用场景坐标模拟拖动。
    qtbot.waitUntil(lambda: other.view.viewport().height() > 200)
    other.fit_to_view()
    other.set_graph_show_all_edges(True)
    assert edge.key in other.branch_items and edge.key not in canvas.branch_items
    view = other.view
    viewport = view.viewport()
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=_edge_point(other, edge))
    assert view.graph_edge_edit.selected_edge == edge

    # 活动画布离开前取消手势，返回标签后旧释放也不能写回同一张图。
    revision = first.engine.revision
    _drag_edge(other, _endpoint_point(other, edge), _node_point(other, "c"),
               cancel=lambda: window.documents.activate_view(first.id))
    assert other.graph == canvas.graph and first.engine.revision == revision
    window.documents.activate_view(second.id)
    qtbot.waitUntil(lambda: window.documents.active is second and view.hasFocus())
    _drag_edge(other, _endpoint_point(other, edge), _node_point(other, "c"))
    updated = GraphEdge("a", "c")
    assert canvas.graph.edges == other.graph.edges == (updated,)
    assert not canvas.branch_items and updated.key in other.branch_items
    assert canvas.graph.positions == other.graph.positions == manual_positions
    assert first.engine.revision == revision + 1

    handle = _endpoint_point(other, updated)
    other.set_editable(False)
    assert not other.action("delete").isEnabled()
    assert view.graph_edge_edit.handle_at(handle) is None
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert other.graph.edges == (updated,) and first.engine.revision == revision + 1
    other.set_editable(True)
    other.set_graph_show_all_edges(False)
    assert view.graph_edge_edit.selected_edge is None and not other.branch_items

    assert first.document.save()
    fresh = load_document(first.document.path).page_view(canvas.current_page_id).graph
    assert fresh.edges == (updated,) and fresh.hidden_node_ids == {"a"}
    assert fresh.positions == manual_positions


def test_drag_moves_nodes_and_reroutes_until_release_and_supports_cancel(window, qtbot):
    canvas = _create_graph(window)
    canvas.fit_to_view()
    canvas.select_nodes(["a"])
    before = canvas.layout_result
    revision = canvas.engine.revision
    view = canvas.view
    start = view.mapFromScene(canvas.node_items["a"].sceneBoundingRect().center())
    end = start + QPoint(75, 30)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view.viewport(), end)
    assert canvas.layout_result.nodes["a"] != before.nodes["a"]
    assert canvas.layout_result.branches["a->b"] != before.branches["a->b"]
    assert not canvas.graph.positions
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert canvas.layout_result == before and canvas.engine.revision == revision
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view.viewport(), end)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert "a" in canvas.graph.positions and "b" not in canvas.graph.positions
    assert canvas.engine.revision == revision + 1
    moved = canvas.graph.positions["a"]
    canvas.apply_graph_text(SAMPLE.replace("入口", "新标题"))
    assert canvas.graph.positions["a"] == moved
    canvas.action("undo").trigger()
    canvas.action("undo").trigger()
    assert not canvas.graph.positions and canvas.layout_result == before


def test_filter_and_status_follow_split_tabs_and_respect_read_only(window, qtbot):
    first = window.documents.active
    canvas = _create_graph(window)
    panel = window.application._graph_filter
    controls = window.application._status_controls
    assert not window.manager.states["graph_filter"].visible
    assert window.manager.activate("graph_filter", focus=False)
    assert panel.isVisible() and not canvas.isAncestorOf(panel)
    assert controls._canvas is canvas and all(button.isVisible() for button in controls.buttons.values())
    panel.model.item(0).setCheckState(Qt.CheckState.Checked)
    assert not canvas.branch_items
    assert first.duplicate()
    second = window.documents.active
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    assert second.document is first.document and second.canvas.graph == canvas.graph
    assert panel._canvas is controls._canvas is second.canvas
    revision = first.engine.revision
    panel.preview.click()
    assert second.canvas.branch_items and not canvas.branch_items
    assert first.engine.revision == revision
    second.canvas.set_editable(False)
    assert not panel.bulk.isEnabled() and panel.preview.isEnabled()
    assert not second.canvas.action("graph_add_edge").isEnabled()
    assert second.canvas.action("graph_text").isEnabled()
    with pytest.raises(MindMapError, match="只读"):
        second.canvas.apply_graph_text("")
    window.documents.activate_view(first.id)
    assert panel._canvas is controls._canvas is canvas
    assert not panel.preview.isChecked()
    panel.bulk.click()
    assert second.canvas.graph.hidden_node_ids == {"a", "b"}
    panel.bulk.click()
    assert canvas.branch_items and second.canvas.branch_items
    canvas.return_to_parent_page()
    assert not panel.isVisible() and controls._canvas is canvas
    window.documents.activate_view(second.id)
    qtbot.waitUntil(panel.isVisible)
    assert panel._canvas is second.canvas


def test_graph_save_restart_remove_undo_and_owner_deletion(window, qtbot, tmp_path, monkeypatch):
    view = window.documents.active
    owner = view.engine.add_child(view.engine.root_id, "附图入口")
    view.canvas.select_nodes([owner])
    canvas = _create_graph(window)
    canvas.set_graph_hidden_nodes({"a"})
    canvas.view.zoom(1.4)
    assert view.document.save()
    assert "graph" in load_document(view.document.path).nodes[owner].extension_pages
    state = canvas.capture_page_view_state()
    assert window.close()
    reopened = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(reopened, before_close_func=lambda item: item.dispose())
    reopened.show()
    canvas = reopened.application.canvas
    assert canvas.current_page_id == graph_page_id(owner)
    assert canvas.graph.hidden_node_ids == {"a"}
    assert canvas.capture_page_view_state()["zoom"] == state["zoom"]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    canvas.action("graph_remove").trigger()
    assert canvas.graph is not None
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    canvas.action("graph_remove").trigger()
    assert canvas.current_page_kind == "main" and canvas.selected_node_ids() == [owner]
    canvas.action("undo").trigger()
    assert canvas.current_page_id == graph_page_id(owner)
    assert canvas.graph.hidden_node_ids == {"a"}
    canvas.engine.remove_nodes([owner])
    assert canvas.current_page_kind == "main"
    assert canvas.engine.page_summary(graph_page_id(owner)) is None
    canvas.action("undo").trigger()
    assert canvas.engine.node_graph(owner).edges == (GraphEdge("a", "b"),)


def test_graph_export_uses_committed_filter(window, tmp_path):
    canvas = _create_graph(window)
    canvas.set_graph_hidden_nodes({"a"})
    canvas.set_graph_show_all_edges(True)
    target = tmp_path / "图.svg"
    canvas.export(target)
    hidden = target.read_text("utf-8")
    canvas.set_graph_hidden_nodes(set())
    canvas.export(target)
    assert target.read_text("utf-8") != hidden
