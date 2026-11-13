"""从产品入口验证图与树共用画布、编辑命令和停靠侧栏。"""

import pytest
from mindmap_app.app import build_window
from mindmap_core import GraphEdge, MindMapError
from mindmap_core.formats import load_document
from mindmap_core.formats.graph import parse_graph_text
from mindmap_core.node_pages import graph_page_id
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QKeyEvent
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
