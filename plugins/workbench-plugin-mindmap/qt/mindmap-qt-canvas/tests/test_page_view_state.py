"""可持久化页面快照、隐藏视口初始化及损坏记录的恢复验收。"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, MindMapError, Node
from mindmap_formats import save_document
from mindmap_qt_canvas import MindMapFileController, MindMapWidget
from PySide6.QtCore import QPointF
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture
def pages(qtbot):
    """创建同名、嵌套及详情页面；全部内容和视图均独立于工作区文件。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "同名")
    engine.add_child(engine.root_id, "主图兄弟")
    first = engine.create_submap(owner)
    first_root = engine.document.submaps[first].root_id
    nested_owner = engine.add_child(first_root, "同名")
    engine.add_child(first_root, "子图兄弟")
    nested = engine.create_submap(nested_owner)
    nested_root = engine.document.submaps[nested].root_id
    detail_owner = engine.add_child(nested_root, "详情入口")
    engine.add_child(nested_root, "嵌套兄弟")
    detail = engine.create_detail_page(detail_owner)
    detail_root = engine.document.detail_pages[detail].root_id
    engine.add_child(detail_root, "详情一")
    engine.add_child(detail_root, "详情二")
    engine.replace_document(engine.document)
    widgets = []

    def create(*, visible=True):
        widget = MindMapWidget(MindMapEngine(engine.document), toolbar=False)
        qtbot.addWidget(widget)
        widgets.append(widget)
        widget.resize(1000, 650)
        if visible:
            widget.show()
            QApplication.processEvents()
        return widget

    yield create, [None, first, nested, detail]
    for widget in widgets:
        widget.finish_edit(commit=False)
        widget.dispose()


def arrange(canvas, page_id):
    """设置真实导航、倍率和位置，返回公开接口捕获的退出快照。"""
    canvas.navigate_to(page_id)
    QApplication.processEvents()
    canvas.view.zoom(1.6 / canvas.view.transform().m11())
    canvas.view.update_navigation_rect(center=QPointF(130, 80))
    canvas.select_nodes(canvas.document_view.root.children)
    return canvas.capture_page_view_state()


def assert_view(canvas, expected):
    actual = canvas.capture_page_view_state()
    assert actual["page_id"] == expected["page_id"]
    assert actual["zoom"] == pytest.approx(expected["zoom"])
    assert actual["selection"] == expected["selection"]
    assert actual["focus_id"] == expected["focus_id"]
    delta = QPointF(*actual["center"]) - QPointF(*expected["center"])
    assert delta.manhattanLength() < 3 / expected["zoom"]


@pytest.mark.parametrize("page_index", range(4))
@pytest.mark.parametrize("editable", [True, False])
def test_roundtrip_restores_each_page_after_first_show_and_resize(pages, page_index, editable, qtbot):
    create, page_ids = pages
    source = create()
    expected = arrange(source, page_ids[page_index])
    serialized = json.loads(json.dumps(expected))
    restored = create(visible=False)
    restored.set_editable(editable)
    document, revision = restored.engine.document, restored.engine.revision
    restored.restore_page_view_state(serialized)
    assert_view(restored, expected)
    serialized["center"][0] += 10000
    serialized["selection"].clear()
    restored.resize(1160, 780)
    restored.show()
    QApplication.processEvents()
    qtbot.waitUntil(lambda: not restored._view_reset_pending)
    assert_view(restored, expected)
    assert not restored._view_reset_pending
    restored.hide()
    restored.resize(860, 540)
    restored.show()
    QApplication.processEvents()
    assert_view(restored, expected)
    assert restored.path_navigation.entries[-1].page_id == expected["page_id"]
    assert restored.engine.document == document and restored.engine.revision == revision
    assert not restored.engine.is_dirty and not restored.engine.can_undo and not restored.engine.can_redo


def test_snapshot_lists_are_owned_by_caller_and_only_include_current_page(pages):
    create, page_ids = pages
    canvas = create()
    arrange(canvas, page_ids[1])
    expected = arrange(canvas, page_ids[2])
    snapshot = canvas.capture_page_view_state()
    snapshot["center"].clear()
    snapshot["selection"].append("不存在")
    assert set(expected) == {"page_id", "zoom", "center", "selection", "focus_id"}
    assert_view(canvas, expected)
    restored = create(visible=False)
    restored.restore_page_view_state(expected)
    assert set(restored._page_states) == {page_ids[2]}


def test_temporary_navigation_scope_and_empty_selection_survive_restart(pages):
    create, page_ids = pages
    canvas = create()
    page = page_ids[2]
    root = canvas.engine.document.submaps[page].root_id
    child = canvas.engine.get_node(root).children[0]
    canvas.engine.set_expanded(root, False)
    canvas.navigate_to(page, child)
    canvas.view.zoom(1.3)
    expected = canvas.capture_page_view_state()
    assert expected["focus_id"] == child
    # 使用具有相同已提交折叠状态的独立画布。
    restored = create(visible=False)
    restored.engine.replace_document(canvas.engine.document)
    restored.restore_page_view_state(expected)
    restored.show()
    QApplication.processEvents()
    assert_view(restored, expected)
    assert not restored.engine.get_node(root).expanded
    restored.select_nodes([])
    empty = restored.capture_page_view_state()
    canvas.restore_page_view_state(empty)
    QApplication.processEvents()
    assert_view(canvas, empty)


@pytest.mark.parametrize("center", [[0, 0], [165, -95]])
def test_unconsumed_restore_preserves_target_when_hidden_and_captured(pages, center):
    create, page_ids = pages
    source = create()
    expected = arrange(source, page_ids[1])
    expected["center"] = center
    canvas = create(visible=False)
    canvas.restore_page_view_state(expected)
    canvas.resize(240, 180)
    QApplication.processEvents()
    assert_view(canvas, expected)
    canvas.resize(1120, 760)
    canvas.show()
    QApplication.processEvents()
    assert_view(canvas, expected)


def test_hidden_initialization_captures_root_target_instead_of_placeholder(pages):
    create, _ = pages
    canvas = create(visible=False)
    canvas.view.scale(0.3, 0.3)
    canvas.view._hidden_center = QPointF(-9999, -9999)
    rect = canvas.layout_result.nodes[canvas.engine.root_id]
    snapshot = canvas.capture_page_view_state()
    assert snapshot["zoom"] == 1
    assert snapshot["center"] == [rect.x + rect.width / 2, rect.y + rect.height / 2]
    canvas.show()
    QApplication.processEvents()
    assert_view(canvas, snapshot)


@pytest.mark.parametrize("invalid", ["已经删除", 12, [], {}])
def test_missing_page_falls_back_to_main_default_view(pages, invalid):
    create, page_ids = pages
    canvas = create()
    snapshot = arrange(canvas, page_ids[1])
    snapshot["page_id"] = invalid
    canvas.restore_page_view_state(snapshot)
    QApplication.processEvents()
    assert canvas.current_page_id is None
    assert canvas.view.transform().isIdentity()
    assert canvas.selected_node_ids() == [canvas.engine.root_id]


@pytest.mark.parametrize("field,value", [
    ("zoom", float("nan")), ("zoom", float("inf")), ("zoom", True),
    ("zoom", "1.5"), ("zoom", 0), ("zoom", -1), ("zoom", 10 ** 500),
    ("center", [float("inf"), 0]), ("center", [False, 2]), ("center", [0]),
    ("center", "错误"), ("center", [0, "错误"]),
])
def test_invalid_geometry_keeps_valid_page_and_uses_default_view(pages, field, value):
    create, page_ids = pages
    canvas = create()
    snapshot = arrange(canvas, page_ids[2])
    snapshot[field] = value
    canvas.restore_page_view_state(snapshot)
    QApplication.processEvents()
    assert canvas.current_page_id == page_ids[2]
    assert canvas.view.transform().isIdentity()
    assert canvas.selected_node_ids() == [canvas.document_view.root_id]
    assert canvas.focus_id is None


@pytest.mark.parametrize("zoom,expected", [(0.001, 0.1), (100, 4)])
def test_finite_zoom_is_limited_to_canvas_range(pages, zoom, expected):
    create, page_ids = pages
    canvas = create()
    snapshot = arrange(canvas, page_ids[1])
    snapshot["zoom"] = zoom
    canvas.restore_page_view_state(snapshot)
    QApplication.processEvents()
    assert canvas.view.transform().m11() == expected


@pytest.mark.parametrize("selection_kind", ["empty", "mixed", "missing", "wrong_type", "folded"])
def test_selection_filters_wrong_page_and_missing_nodes(pages, selection_kind):
    create, page_ids = pages
    canvas = create()
    snapshot = arrange(canvas, page_ids[1])
    valid = snapshot["selection"][0]
    snapshot["focus_id"] = canvas.engine.root_id  # 属于主图的临时范围必须忽略。
    if selection_kind == "empty":
        snapshot["selection"] = []
        expected = []
    elif selection_kind == "mixed":
        snapshot["selection"] = [valid, "删除", canvas.engine.root_id, valid, {}]
        expected = [valid]
    else:
        snapshot["selection"] = {} if selection_kind == "wrong_type" else ["删除", canvas.engine.root_id]
        expected = [canvas.document_view.root_id]
        if selection_kind == "folded":
            snapshot["selection"] = [valid]
            canvas.engine.set_expanded(canvas.document_view.root_id, False)
    canvas.restore_page_view_state(snapshot)
    QApplication.processEvents()
    assert canvas.selected_node_ids() == expected
    assert canvas.focus_id is None


def test_capture_preserves_text_draft_and_restore_rejects_editor_and_input_guard(pages):
    create, _ = pages
    canvas = create()
    snapshot = canvas.capture_page_view_state()
    document, revision = canvas.engine.document, canvas.engine.revision
    canvas.begin_edit(canvas.engine.root_id)
    editor = canvas.editor
    editor.setPlainText("尚未提交的草稿")
    editor.inputMethodEvent(QInputMethodEvent("候选", []))
    assert canvas.capture_page_view_state()["page_id"] is None
    with pytest.raises(MindMapError, match="结束编辑"):
        canvas.restore_page_view_state(snapshot)
    assert canvas.editor is editor and editor.composing
    assert editor.toPlainText() == "尚未提交的草稿"
    editor.inputMethodEvent(QInputMethodEvent("", []))
    canvas.finish_edit(commit=False)
    remove = canvas.register_navigation_guard(lambda: False)
    with pytest.raises(MindMapError, match="输入法候选"):
        canvas.restore_page_view_state(snapshot)
    remove()
    assert canvas.engine.document == document and canvas.engine.revision == revision


def test_replacement_and_disposal_cancel_queued_restore(pages, tmp_path):
    create, page_ids = pages
    source = create()
    snapshot = arrange(source, page_ids[1])
    canvas = create(visible=False)
    canvas.restore_page_view_state(snapshot)
    replacement = tmp_path / "替换.mindmap"
    save_document(MindMapDocument.new("替换根"), replacement)
    files = MindMapFileController(canvas, SimpleNamespace(flush_pending_edits=lambda: None))
    assert files.open_path(replacement)
    canvas.show()
    QApplication.processEvents()
    assert canvas.current_page_id is None
    assert canvas.view.transform().isIdentity()
    assert canvas._view_restore_center is None
    canvas.restore_page_view_state(snapshot)  # 页面已不在新文档中，仍安全降级。
    canvas.dispose()
    QApplication.processEvents()
    with pytest.raises(MindMapError, match="已释放"):
        canvas.capture_page_view_state()
    with pytest.raises(MindMapError, match="已释放"):
        canvas.restore_page_view_state(snapshot)
    pending = create(visible=False)
    pending.restore_page_view_state(snapshot)
    pending.show()
    pending.dispose()
    QApplication.processEvents()
    assert not pending._view_reset_pending and pending._view_restore_center is None


def test_restore_preserves_existing_dirty_undo_and_redo_history(pages):
    create, page_ids = pages
    canvas = create()
    expected = arrange(canvas, page_ids[1])
    root = canvas.document_view.root_id
    canvas.engine.update_node(root, text="已有修改")
    canvas.engine.add_child(root, "可重做的主题")
    assert canvas.engine.undo()
    document, revision = canvas.engine.document, canvas.engine.revision
    assert canvas.engine.is_dirty and canvas.engine.can_undo and canvas.engine.can_redo
    canvas.restore_page_view_state(expected)
    QApplication.processEvents()
    assert canvas.engine.document == document and canvas.engine.revision == revision
    assert canvas.engine.is_dirty and canvas.engine.can_undo and canvas.engine.can_redo


def test_large_page_restores_screen_and_offscreen_multiselection(pages):
    create, _ = pages
    canvas = create()
    document = MindMapDocument.new("大图")
    for index in range(1050):
        node = Node(f"主题 {index}")
        document.nodes[node.id] = node
        document.root.children.append(node.id)
    canvas.engine.replace_document(document)
    canvas.reset_view()
    QApplication.processEvents()
    first, last = document.root.children[0], document.root.children[-1]
    canvas.select_nodes([last, first])
    rect = canvas.layout_result.nodes[last]
    canvas.view.update_navigation_rect(center=QPointF(rect.x + rect.width / 2, rect.y + rect.height / 2))
    expected = canvas.capture_page_view_state()
    assert canvas._virtualized
    restored = create(visible=False)
    restored.engine.replace_document(document)
    restored.restore_page_view_state(expected)
    restored.show()
    QApplication.processEvents()
    assert restored._virtualized
    assert_view(restored, expected)
    assert restored.selected_node_ids() == [first, last]


@pytest.mark.parametrize("invalid", [None, [], "错误"])
def test_non_mapping_snapshot_fails_without_changing_view(pages, invalid):
    create, page_ids = pages
    canvas = create()
    expected = arrange(canvas, page_ids[2])
    with pytest.raises(MindMapError, match="必须是对象"):
        canvas.restore_page_view_state(invalid)
    assert_view(canvas, deepcopy(expected))
