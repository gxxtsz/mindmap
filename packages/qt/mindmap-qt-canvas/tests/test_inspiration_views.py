"""单想法导航、当前范围导出、跨控件拖放和撤销重做的界面验收。"""

import json

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_ui.dialogs import EdrawPasteOptions
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QInputMethodEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture
def canvas(qtbot):
    widget = MindMapWidget(toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(1100, 700)
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.finish_edit(commit=False)
    widget.dispose()


def ideas(canvas):
    e = canvas.engine
    first = e.add_inspiration("甲想法", position=(-700, 20))
    child = e.add_child(first, "甲孩子")
    second = e.add_inspiration("乙想法", position=(900, -100))
    return first, child, second


def test_individual_views_and_owned_pages_restore(canvas):
    a, child, b = ideas(canvas)
    original = canvas.engine.document
    canvas.show_inspiration(a)
    QApplication.processEvents()
    assert set(canvas.node_items) == {a, child}
    assert canvas.layout_result.nodes[a].x == 0
    canvas.view.zoom(1.5)
    canvas.view.update_navigation_rect(center=QPointF(80, 60))
    canvas.select_nodes([child])
    expected = canvas.capture_page_view_state()
    canvas.show_inspiration(b)
    QApplication.processEvents()
    assert set(canvas.node_items) == {b}
    canvas.view.zoom(0.7)
    canvas.navigate_to(None)
    canvas.show_inspiration(a)
    assert canvas.view.transform().m11() == pytest.approx(expected["zoom"])
    assert canvas.selected_node_ids() == [child]
    assert canvas.engine.document == original
    canvas.select_nodes([child])
    detail = canvas.create_detail_page()
    assert canvas.current_page_id == detail
    assert [entry.text for entry in canvas.path_navigation.entries] == ["甲想法", "甲孩子 · 详情"]
    canvas.return_to_parent_page()
    assert canvas.current_inspiration_root_id == a
    assert set(canvas.node_items) == {a, child}


def test_restart_preserves_all_idea_views_and_current_main(canvas, qtbot):
    a, child, b = ideas(canvas)
    canvas.show_inspiration(a)
    QApplication.processEvents()
    canvas.view.zoom(1.4)
    canvas.view.update_navigation_rect(center=QPointF(70, 50))
    canvas.select_nodes([child])
    canvas.show_inspiration(b)
    QApplication.processEvents()
    canvas.view.zoom(0.6)
    canvas.navigate_to(None)
    state = json.loads(json.dumps(canvas.capture_page_view_state()))
    restored = MindMapWidget(MindMapEngine(canvas.engine.document), toolbar=False)
    qtbot.addWidget(restored)
    try:
        restored.restore_page_view_state(state)
        assert restored.current_page_id is None
        restored.show_inspiration()
        assert restored.current_inspiration_root_id == b
        assert restored.view.transform().m11() == pytest.approx(0.6)
        restored.show_inspiration(a)
        assert restored.view.transform().m11() == pytest.approx(1.4)
        assert restored.selected_node_ids() == [child]
    finally:
        restored.dispose()


def test_sidebar_drag_combines_then_undo_restores_exact_view(canvas):
    a, child, b = ideas(canvas)
    canvas.show_inspiration(a)
    canvas.select_nodes([child])
    original, revision = canvas.engine.document, canvas.engine.revision
    mime = canvas.create_branch_drag([b], origin="sidebar")
    assert canvas.current_inspiration_root_id == a
    point = canvas.view.mapFromScene(QPointF(*canvas.layout_result.nodes[child].center))
    enter = QDragEnterEvent(point, Qt.DropAction.MoveAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas.view.viewport(), enter)
    assert enter.isAccepted()
    move = QDragMoveEvent(point, Qt.DropAction.MoveAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas.view.viewport(), move)
    assert move.isAccepted() and canvas.view._branch_drop_preview is not None
    assert canvas.engine.document == original and canvas.engine.revision == revision
    drop = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas.view.viewport(), drop)
    assert drop.isAccepted()
    assert canvas.engine.revision == revision + 1
    assert canvas.engine.parent_id(b) == child
    assert canvas.current_inspiration_root_id == a and canvas.selected_node_ids() == [b]
    canvas.engine.undo()
    assert canvas.engine.document == original
    assert canvas.current_inspiration_root_id == a and canvas.selected_node_ids() == [child]
    canvas.engine.redo()
    assert canvas.current_inspiration_root_id == a and canvas.selected_node_ids() == [b]


def test_detaching_multiple_branches_stays_and_is_atomic(canvas):
    e = canvas.engine
    a = e.add_child(e.root_id, "甲")
    child = e.add_child(a, "随行")
    b = e.add_child(e.root_id, "乙")
    detail = e.create_detail_page(child)
    canvas.select_nodes([a, child, b])
    original, revision = e.document, e.revision
    mime = canvas.create_branch_drag()
    assert canvas.can_detach_branch_drag(mime)
    assert canvas.detach_branch_drag(mime)
    assert e.revision == revision + 1
    assert canvas.current_page_id is None
    assert set(e.document.inspiration.root_ids) == {a, b}
    assert not {a, b, child} & set(canvas.node_items)
    assert e.get_node(child).detail_page_id == detail
    assert e.document.inspiration.positions[a] == (0, 0)
    assert e.document.inspiration.positions[b][1] > 0
    e.undo()
    assert e.document == original and canvas.current_page_id is None
    assert canvas.selected_node_ids() == [a, child, b]
    e.redo()
    assert canvas.current_page_id is None and canvas.selected_node_ids() == [e.root_id]


def test_blank_cancel_readonly_composing_stale_and_cross_file_are_noops(canvas, qtbot):
    a, child, b = ideas(canvas)
    canvas.show_inspiration(a)
    e = canvas.engine
    original, revision = e.document, e.revision
    mime = canvas.create_branch_drag([b], origin="sidebar")
    point = canvas.view.viewport().rect().bottomRight() - canvas.view.viewport().rect().topLeft() / 10
    drop = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas.view.dropEvent(drop)
    assert not drop.isAccepted()
    canvas.view.dragLeaveEvent(QDragLeaveEvent())
    canvas.select_nodes([child])
    control = canvas.view.node_drag
    assert control.begin(QPointF(*canvas.layout_result.nodes[child].center))
    assert control.finish(canvas.view.mapFromScene(QPointF(-200, 220))) is None
    assert e.document == original and e.revision == revision
    canvas.set_editable(False)
    assert not canvas.can_receive_branch_drag(mime, a)
    canvas.set_editable(True)
    canvas.begin_edit(child)
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    assert not canvas.can_receive_branch_drag(mime, a)
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent())
    canvas.finish_edit(commit=False)
    other = MindMapWidget(MindMapEngine(e.document), toolbar=False)
    qtbot.addWidget(other)
    try:
        other.show_inspiration(a)
        assert not other.can_receive_branch_drag(mime, a)
    finally:
        other.dispose()
    e.update_node(child, text="版本已变")
    assert not canvas.can_receive_branch_drag(mime, a)


def test_current_projection_export_and_invalid_navigation(canvas, tmp_path):
    a, child, b = ideas(canvas)
    canvas.show_inspiration(a)
    path = tmp_path / "单个.svg"
    canvas.export(path)
    text = path.read_text(encoding="utf-8")
    assert "甲想法" in text and "甲孩子" in text and "乙想法" not in text
    assert "<title>甲想法</title>" in text
    canvas.show_inspiration(b)
    canvas.export(path)
    text = path.read_text(encoding="utf-8")
    assert "乙想法" in text and "甲想法" not in text and "<title>乙想法</title>" in text
    with pytest.raises(MindMapError):
        canvas.show_inspiration(child)
    assert canvas.current_inspiration_root_id == b and set(canvas.node_items) == {b}


def test_owned_submap_cycle_and_failed_transfer_leave_no_history(canvas, monkeypatch):
    a, child, b = ideas(canvas)
    e = canvas.engine
    page = e.create_submap(b)
    target = e.document.submaps[page].root_id
    canvas.navigate_to(page)
    e.mark_saved()
    original, revision = e.document, e.revision
    mime = canvas.create_branch_drag([b], origin="sidebar")
    assert not canvas.can_receive_branch_drag(mime, target)
    assert not canvas.receive_branch_drag(mime, target)
    assert e.document == original and e.revision == revision and not e.is_dirty
    canvas.navigate_to(None)
    mime = canvas.create_branch_drag([b], origin="sidebar")

    def reject_layout(document):
        raise MindMapError("布局拒绝候选")

    with monkeypatch.context() as patch:
        patch.setattr(canvas, "validate_document", reject_layout)
        with pytest.raises(MindMapError, match="布局拒绝"):
            canvas.receive_branch_drag(mime, e.root_id)
    assert e.document == original and e.revision == revision and not e.is_dirty
    e.before_operation = lambda operation, arguments: operation != "attach_inspirations"
    with pytest.raises(MindMapError):
        canvas.receive_branch_drag(mime, e.root_id)
    assert e.document == original and e.revision == revision and not e.is_dirty


def test_deleted_and_combined_current_root_follows_valid_idea(canvas):
    a, child, b = ideas(canvas)
    e = canvas.engine
    c = e.add_inspiration("丙想法", position=(500, 500))
    canvas.show_inspiration(b)
    original = e.document
    e.remove_nodes([b])
    assert canvas.current_inspiration_root_id == c
    e.undo()
    assert e.document == original and canvas.current_inspiration_root_id == b
    e.move_nodes([b], a, "inside")
    assert canvas.current_inspiration_root_id == a and b in canvas.node_items
    e.undo()
    assert e.document == original and canvas.current_inspiration_root_id == b


def test_legacy_overview_snapshot_and_invalid_cached_roots(canvas):
    a, child, b = ideas(canvas)
    e = canvas.engine
    original, revision = e.document, e.revision
    canvas.restore_page_view_state({"page_id": e.inspiration_id, "zoom": 1.2,
                                    "center": [0, 0], "selection": [b], "focus_id": None})
    assert canvas.current_inspiration_root_id == a and set(canvas.node_items) == {a, child}
    assert canvas.view.transform().m11() == 1
    state = canvas.capture_page_view_state()
    state["inspiration"]["views"].extend([None, {"mode": "single", "root_id": []},
                                         {"mode": "single", "root_id": "已删除"}])
    canvas.restore_page_view_state(state)
    assert canvas.current_inspiration_root_id == a and set(canvas.node_items) == {a, child}
    assert e.document == original and e.revision == revision


def test_legacy_overview_uses_last_idea_cache_and_discards_forest_geometry(canvas):
    a, child, b = ideas(canvas)
    e = canvas.engine
    original, revision = e.document, e.revision
    canvas.restore_page_view_state({
        "page_id": e.inspiration_id, "zoom": 2.7, "center": [2000, -4000], "selection": [a, b],
        "inspiration": {"mode": "overview", "root_id": None, "last_root_id": b, "views": [
            {"mode": "overview", "root_id": None, "zoom": 3, "center": [3000, -5000]},
            {"mode": "single", "root_id": b, "zoom": 0.65, "center": [70, 30], "selection": [b]},
        ]},
    })
    assert canvas.current_inspiration_root_id == b and set(canvas.node_items) == {b}
    assert canvas.view.transform().m11() == pytest.approx(0.65)
    state = canvas.capture_page_view_state()
    assert "mode" not in state["inspiration"]
    assert all("mode" not in entry for entry in state["inspiration"]["views"])
    assert all(len(key) == 2 for key in canvas._page_states if isinstance(key, tuple))
    assert e.document == original and e.revision == revision


def test_legacy_empty_snapshot_and_removed_overview_interfaces(canvas, tmp_path):
    e = canvas.engine
    e.mark_saved()
    canvas.restore_page_view_state({"page_id": e.inspiration_id, "zoom": 2, "center": [700, 600],
                                    "inspiration": {"mode": "overview", "last_root_id": "已删除"}})
    assert canvas.current_inspiration_root_id is None and not canvas.node_items
    assert not hasattr(canvas, "show_inspiration_overview")
    assert not hasattr(canvas, "inspiration_view_mode")
    assert not hasattr(canvas.path_navigation, "inspiration_button")
    assert canvas.path_navigation.entries == []
    assert not e.is_dirty
    with pytest.raises(MindMapError):
        canvas.export(tmp_path / "空想法.svg")


@pytest.mark.parametrize("mode", [None, "single", "overview"])
def test_stale_active_root_uses_valid_idea_view_without_copying_stale_geometry(canvas, mode):
    a, child, b = ideas(canvas)
    extra = {"root_id": "已删除", "last_root_id": b, "views": [
        {"root_id": b, "zoom": 0.8, "center": [70, 20], "selection": [b]},
        {"mode": "overview", "root_id": None, "zoom": 3, "center": [4000, 2000]},
    ]}
    if mode is not None:
        extra["mode"] = mode
    canvas.restore_page_view_state({"page_id": canvas.engine.inspiration_id, "zoom": 2.5,
                                    "center": [5000, 3000], "selection": ["已删除"], "inspiration": extra})
    assert canvas.current_inspiration_root_id == b and set(canvas.node_items) == {b}
    assert canvas.view.transform().m11() == pytest.approx(0.8)
    assert canvas.selected_node_ids() == [b]


def test_nested_owned_page_paths_and_return_keep_only_the_owning_idea(canvas, qtbot):
    a, child, b = ideas(canvas)
    e = canvas.engine
    submap = e.create_submap(child)
    subroot = e.document.submaps[submap].root_id
    leaf = e.add_child(subroot, "细分主题")
    detail = e.create_detail_page(leaf)
    canvas.show_inspiration(a)
    canvas.select_nodes([child])
    canvas.navigate_to(detail)
    assert [entry.text for entry in canvas.path_navigation.entries] == ["甲想法", "甲孩子", "细分主题 · 详情"]
    assert canvas.path_navigation.back_button.toolTip() == "返回所属子导图"
    assert canvas.path_navigation.back_button.accessibleName() == "返回所属子导图"
    qtbot.mouseClick(canvas.path_navigation.back_button, Qt.MouseButton.LeftButton)
    assert canvas.current_page_id == submap
    assert canvas.path_navigation.back_button.toolTip() == "返回所属想法"
    qtbot.mouseClick(canvas.path_navigation.back_button, Qt.MouseButton.LeftButton)
    assert canvas.current_inspiration_root_id == a and canvas.selected_node_ids() == [child]
    assert set(canvas.node_items) == {a, child} and b not in canvas.node_items
    assert canvas.path_navigation.back_button.toolTip() == "返回主导图"
    qtbot.mouseClick(canvas.path_navigation.back_button, Qt.MouseButton.LeftButton)
    assert canvas.current_page_id is None and not canvas.path_navigation.back_button.isEnabled()


def test_double_click_creates_and_opens_another_idea_with_undo_redo(canvas, qtbot):
    a, child, b = ideas(canvas)
    e = canvas.engine
    canvas.show_inspiration(a)
    QApplication.processEvents()
    original, revision = e.document, e.revision
    point = canvas.view.mapFromScene(QPointF(-400, 250))
    assert canvas.view.itemAt(point) is None
    qtbot.mouseDClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    fresh = canvas.current_inspiration_root_id
    assert fresh not in (a, b) and canvas.editor is not None
    assert e.revision == revision + 1 and set(canvas.node_items) == {fresh}
    assert e.document.inspiration.positions[a] == original.inspiration.positions[a]
    canvas.finish_edit(commit=False)
    after = e.document
    e.undo()
    assert e.document == original and canvas.current_inspiration_root_id == a
    e.redo()
    assert e.document == after and canvas.current_inspiration_root_id == fresh


def test_replace_paste_redo_keeps_selected_idea_root(canvas, monkeypatch):
    a, child, b = ideas(canvas)
    e = canvas.engine
    canvas.show_inspiration(a)
    original = e.document
    fragment = MindMapEngine()
    root = fragment.add_child(fragment.root_id, "替换标题")
    fragment.add_child(root, "新子主题")
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: EdrawPasteOptions("replace", False, True))
    assert canvas._paste_edraw(fragment.document) == [a]
    assert canvas.selected_node_ids() == [a] and child not in canvas.node_items
    after = e.document
    e.undo()
    assert e.document == original and canvas.current_inspiration_root_id == a
    e.redo()
    assert e.document == after and canvas.current_inspiration_root_id == a
    assert canvas.selected_node_ids() == [a]
