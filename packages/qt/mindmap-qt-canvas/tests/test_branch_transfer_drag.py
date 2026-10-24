"""原生拖入与内部拖动的落点、影像、跨区事务及取消边界回归。"""

import pytest
from mindmap_core import LayoutConfig, MindMapEngine, MindMapError
from mindmap_qt_canvas import MindMapWidget
from PySide6.QtCore import QAbstractAnimation, QBuffer, QIODevice, QPointF, Qt
from PySide6.QtGui import QColor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication


@pytest.fixture
def canvas(qtbot):
    engine = MindMapEngine()
    source = engine.add_inspiration("独立想法", position=(900, -400))
    child = engine.add_child(source, "随行内容")
    engine.create_detail_page(child)
    engine.update_node(source, style={"text_color": "#bd5279", "font_size": 14})
    widget = MindMapWidget(engine, toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(1200, 800)
    widget.show()
    QApplication.processEvents()
    yield widget, source, child
    widget.dispose()


def prepare(widget, context="main", direction="right"):
    """构造三种接收画布及内部分支，后者用于与外部根比较同一落点。"""
    engine = widget.engine
    if context == "submap":
        owner = engine.add_child(engine.root_id, "子导图入口")
        widget.navigate_to(engine.create_submap(owner))
    elif context == "inspiration":
        root = engine.add_inspiration("当前想法", position=(-200, 50))
        widget.show_inspiration(root)
    root = widget.document_view.root_id
    target = engine.add_child(root, "接收主题")
    tail = engine.add_child(root, "同级主题")
    origin = engine.add_child(root, "内部来源")
    internal = engine.add_child(origin, "内部移动主题")
    engine.set_layout(LayoutConfig(direction=direction), page_id=widget.current_page_id)
    if direction == "both":
        for key in (target, tail, origin):
            engine.update_node(key, direction="right")
    widget.select_nodes([tail])
    QApplication.processEvents()
    return target, tail, internal


def body(widget, key):
    """正文中心使用正式布局几何，不依赖正在绘制的预览图层。"""
    return widget.view.mapFromScene(QPointF(*widget.layout_result.nodes[key].center))


def destination(widget, target, position):
    """在正文或正文外缘选取落点，排序区沿目标布局的排序轴分布。"""
    rect = widget.layout_result.nodes[target]
    if position == "inside":
        return body(widget, target)
    offset = (-1 if position == "before" else 1) * 8 / widget.view.transform().m11()
    if widget.document_view.layout.direction == "down":
        return widget.view.mapFromScene(QPointF((rect.x if position == "before" else rect.right) + offset,
                                               rect.center[1]))
    return widget.view.mapFromScene(QPointF(rect.center[0],
                                           (rect.y if position == "before" else rect.bottom) + offset))


def drag_event(widget, mime, point, kind):
    """经真实视口分派 Qt 原生拖放事件，覆盖控件事件路由及接受状态。"""
    if kind == "drop":
        event = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    else:
        event_type = QDragEnterEvent if kind == "enter" else QDragMoveEvent
        event = event_type(point, Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget.view.viewport(), event)
    return event


@pytest.mark.parametrize("context", ["main", "submap", "inspiration"])
@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("position", ["inside", "before", "after"])
def test_native_and_internal_drag_share_targets_visuals_and_atomic_results(canvas, context, direction, position):
    widget, source, child = canvas
    target, tail, internal = prepare(widget, context, direction)
    engine, control = widget.engine, widget.view.node_drag
    point = destination(widget, target, position)
    widget.select_nodes([internal])
    assert control.begin(QPointF(*widget.layout_result.nodes[internal].center))
    control.update(point)
    assert control.target is not None and control.target.position == position
    proposal, hint, layer_type = control.target.proposal, control._hint_text(), type(control.ghost)
    control.cancel()
    widget.select_nodes([tail])
    page_context = widget.current_page_id, widget.current_inspiration_root_id
    original, revision = engine.document, engine.revision
    positions = {key: item.pos() for key, item in widget.node_items.items()}
    mime = widget.create_branch_drag([source], origin="sidebar")
    assert drag_event(widget, mime, point, "enter").isAccepted()
    assert drag_event(widget, mime, point, "move").isAccepted()
    actual = control.target.proposal
    assert (actual.parent_id, actual.index, actual.direction) == (
        proposal.parent_id, proposal.index, proposal.direction)
    assert type(control.ghost) is layer_type and control._hint_text() == hint
    assert control._connection_hint().isEmpty() == (position != "inside")
    assert control._insertion_hint().isEmpty() == (position == "inside")
    widget.view.viewport().grab()
    assert engine.document == original and engine.revision == revision
    assert positions == {key: item.pos() for key, item in widget.node_items.items()}
    assert drag_event(widget, mime, point, "drop").isAccepted()
    assert engine.revision == revision + 1 and engine.parent_id(source) == proposal.parent_id
    assert engine.get_node(proposal.parent_id).children[proposal.index] == source
    assert engine.get_node(source).style == original.nodes[source].style
    assert engine.get_node(child).detail_page_id == original.nodes[child].detail_page_id
    assert widget.selected_node_ids() == [source]
    assert (widget.current_page_id, widget.current_inspiration_root_id) == page_context
    assert control._animation.state() == QAbstractAnimation.State.Running
    widget.cancel_branch_transfer_preview()
    assert control._animation.state() == QAbstractAnimation.State.Running
    control._animation.setCurrentTime(140)
    assert control._reflow is None and not control.active
    engine.undo()
    assert engine.document == original and widget.selected_node_ids() == [tail]
    assert (widget.current_page_id, widget.current_inspiration_root_id) == page_context
    engine.redo()
    assert engine.get_node(proposal.parent_id).children[proposal.index] == source


@pytest.mark.parametrize("side", ["left", "right"])
def test_native_drag_to_empty_both_side_keeps_other_branches_fixed(canvas, side):
    widget, source, _ = canvas
    prepare(widget, direction="both")
    engine = widget.engine
    root = engine.root_id
    opposite = "right" if side == "left" else "left"
    siblings = list(engine.get_node(root).children)
    for key in siblings:
        engine.update_node(key, direction=opposite)
    rect = widget.layout_result.nodes[root]
    point = widget.view.mapFromScene(QPointF(rect.x + rect.width * (0.25 if side == "left" else 0.75),
                                            rect.center[1]))
    original = engine.document
    mime = widget.create_branch_drag([source], origin="sidebar")
    assert drag_event(widget, mime, point, "enter").isAccepted()
    assert widget.view.node_drag.target.proposal.direction == side
    assert drag_event(widget, mime, point, "drop").isAccepted()
    assert engine.get_node(source).direction == side
    assert all(engine.get_node(key).direction == opposite for key in siblings)
    engine.undo()
    assert engine.document == original


def test_native_hover_does_not_expand_and_submap_body_cannot_receive(canvas):
    widget, source, _ = canvas
    target, _, _ = prepare(widget)
    engine = widget.engine
    hidden = engine.add_child(target, "折叠后代")
    engine.set_expanded(target, False)
    original, revision = engine.document, engine.revision
    mime, point = widget.create_branch_drag([source], origin="sidebar"), body(widget, target)
    assert drag_event(widget, mime, point, "enter").isAccepted()
    assert hidden not in widget.node_items and engine.document == original
    assert engine.revision == revision
    assert drag_event(widget, mime, point, "drop").isAccepted()
    assert engine.get_node(target).expanded and hidden in widget.node_items
    engine.undo()
    assert engine.document == original
    entry = engine.add_child(engine.root_id, "已有子导图")
    engine.create_submap(entry)
    mime, point = widget.create_branch_drag([source], origin="sidebar"), body(widget, entry)
    assert drag_event(widget, mime, point, "enter").isAccepted()
    assert not drag_event(widget, mime, point, "move").isAccepted()
    assert widget.view.node_drag.target is None
    point = destination(widget, entry, "before")
    assert drag_event(widget, mime, point, "move").isAccepted()
    assert drag_event(widget, mime, point, "drop").isAccepted()
    assert engine.get_node(engine.root_id).children.index(source) < engine.get_node(engine.root_id).children.index(entry)


@pytest.mark.parametrize("cancel", ["blank", "leave", "escape", "stale", "readonly", "replace", "page"])
def test_native_cancel_and_invalidations_leave_no_transfer_or_animation(canvas, qtbot, cancel):
    widget, source, _ = canvas
    target, _, _ = prepare(widget)
    engine, control = widget.engine, widget.view.node_drag
    mime, point = widget.create_branch_drag([source], origin="sidebar"), body(widget, target)
    assert drag_event(widget, mime, point, "enter").isAccepted()
    original, revision = engine.document, engine.revision
    if cancel == "blank":
        point = widget.view.viewport().rect().bottomRight()
        assert not drag_event(widget, mime, point, "move").isAccepted()
        assert not drag_event(widget, mime, point, "drop").isAccepted()
    elif cancel == "leave":
        QApplication.sendEvent(widget.view.viewport(), QDragLeaveEvent())
    elif cancel == "escape":
        qtbot.keyClick(widget.view, Qt.Key.Key_Escape)
    elif cancel == "stale":
        engine.update_node(target, text="外部已更新")
        assert not drag_event(widget, mime, point, "drop").isAccepted()
    elif cancel == "readonly":
        widget.set_editable(False)
    elif cancel == "replace":
        engine.replace_document(engine.document)
    else:
        widget.show_inspiration(source)
    widget.cancel_branch_transfer_preview()
    assert not control.active and not control._scroll_timer.isActive()
    assert control._animation.state() == QAbstractAnimation.State.Stopped
    assert source in engine.document.inspiration.root_ids
    if cancel != "stale":
        assert engine.document == original
    if cancel not in ("stale", "replace"):
        assert engine.revision == revision


@pytest.mark.parametrize("index,direction", [(-1, None), (True, None), (999, None), (None, "up"), (0, "left")])
def test_invalid_public_drop_parameters_do_not_touch_content(canvas, index, direction):
    widget, source, _ = canvas
    target, _, _ = prepare(widget)
    engine = widget.engine
    original, revision = engine.document, engine.revision
    mime = widget.create_branch_drag([source], origin="sidebar")
    with pytest.raises(MindMapError):
        widget.receive_branch_drag(mime, target, index=index, direction=direction)
    assert engine.document == original and engine.revision == revision


def test_external_hover_caches_source_layout_and_never_relayouts_large_target(canvas, monkeypatch):
    widget, source, _ = canvas
    prepare(widget)
    with widget.engine.transaction("准备大图"):
        for index in range(1050):
            widget.engine.add_child(widget.engine.root_id, f"其他主题 {index}")
    widget.center_root()
    point = body(widget, widget.engine.root_id)
    mime = widget.create_branch_drag([source], origin="sidebar")
    assert drag_event(widget, mime, point, "enter").isAccepted()
    control = widget.view.node_drag
    original, revision = widget.engine.document, widget.engine.revision
    source_layout, target_layout = control._source.layout, widget.layout_result

    def unexpected(*args, **kwargs):
        pytest.fail("连续原生拖动不应重新测量或布局")

    monkeypatch.setattr(widget.layout_registry, "layout", unexpected)
    monkeypatch.setattr(widget.factory, "measure", unexpected)
    for _ in range(5):
        assert drag_event(widget, mime, point, "move").isAccepted()
        widget.view.viewport().grab()
    assert control._source.layout is source_layout and widget.layout_result is target_layout
    assert widget.engine.document == original and widget.engine.revision == revision
    assert control._resolver._virtual
    widget.cancel_branch_transfer_preview()


def test_native_drop_preserves_images_owned_pages_and_favorites(canvas):
    widget, source, child = canvas
    target, _, _ = prepare(widget)
    engine = widget.engine
    image = QImage(90, 40, QImage.Format.Format_ARGB32)
    image.fill(QColor("#42a5a0"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    engine.attach_image(child, bytes(buffer.data()), width=90)
    page = engine.create_submap(child)
    engine.set_submap_favorite(page, True)
    engine.update_node(child, note="保留备注", hyperlink="https://example.com")
    original = engine.document
    mime, point = widget.create_branch_drag([source], origin="sidebar"), body(widget, target)
    assert drag_event(widget, mime, point, "enter").isAccepted()
    widget.view.viewport().grab()
    assert drag_event(widget, mime, point, "drop").isAccepted()
    assert engine.get_node(child) == original.nodes[child]
    assert engine.document.assets == original.assets
    assert engine.document.submaps == original.submaps
    assert engine.document.detail_pages == original.detail_pages
    assert engine.is_submap_favorite(page)
    engine.undo()
    assert engine.document == original


@pytest.mark.parametrize("rejected", ["move_nodes", "update_node", "layout"])
def test_native_transfer_rejection_rolls_back_attach_order_direction_and_fold(canvas, monkeypatch, rejected):
    widget, source, _ = canvas
    target, _, _ = prepare(widget, direction="both")
    engine = widget.engine
    engine.mark_saved()
    original, revision = engine.document, engine.revision
    mime = widget.create_branch_drag([source], origin="sidebar")
    point = destination(widget, target, "before")
    assert drag_event(widget, mime, point, "enter").isAccepted()
    if rejected == "layout":
        def reject_layout(document):
            raise MindMapError("布局拒绝跨区域候选")
        monkeypatch.setattr(widget, "validate_document", reject_layout)
    else:
        engine.before_operation = lambda operation, arguments: operation != rejected
    assert not drag_event(widget, mime, point, "drop").isAccepted()
    assert engine.document == original and engine.revision == revision and not engine.is_dirty
    assert not widget.view.node_drag.active and widget.view.node_drag._reflow is None


def test_native_edge_scroll_keeps_document_fixed_and_stops_on_leave(canvas, qtbot):
    widget, source, _ = canvas
    target, _, _ = prepare(widget)
    engine, view = widget.engine, widget.view
    mime = widget.create_branch_drag([source], origin="sidebar")
    assert drag_event(widget, mime, body(widget, target), "enter").isAccepted()
    point = view.viewport().rect().topRight()
    point.setY(view.viewport().height() // 2)
    original = engine.document
    positions = {key: item.pos() for key, item in widget.node_items.items()}
    before = view.horizontalScrollBar().value()
    drag_event(widget, mime, point, "move")
    assert view.node_drag._scroll_timer.isActive()
    qtbot.waitUntil(lambda: view.horizontalScrollBar().value() != before)
    assert engine.document == original
    assert positions == {key: item.pos() for key, item in widget.node_items.items()}
    QApplication.sendEvent(view.viewport(), QDragLeaveEvent())
    assert not view.node_drag.active and not view.node_drag._scroll_timer.isActive()


def test_canvas_native_handoff_cleanup_preserves_committed_animation(canvas, monkeypatch):
    widget, _, _ = canvas
    target, _, internal = prepare(widget)
    widget.select_nodes([internal])
    original = widget.engine.document
    point = body(widget, target)

    class NativeDrag:
        """仅替代平台拖动循环，目标端仍接收实际 Qt 拖放事件。"""

        def __init__(self, source):
            self.mime = None

        def setMimeData(self, mime):
            self.mime = mime

        def exec(self, action):
            assert drag_event(widget, self.mime, point, "enter").isAccepted()
            assert drag_event(widget, self.mime, point, "drop").isAccepted()
            return action

        def deleteLater(self):
            pass

    monkeypatch.setattr("mindmap_qt_canvas.transfers.QDrag", NativeDrag)
    assert widget.start_external_branch_drag([internal]) == Qt.DropAction.MoveAction
    assert widget.engine.parent_id(internal) == target
    assert widget.view.node_drag._animation.state() == QAbstractAnimation.State.Running
    widget.engine.undo()
    assert widget.engine.document == original


def test_native_owned_page_cycle_detail_and_cross_file_are_rejected(canvas, qtbot):
    widget, source, child = canvas
    prepare(widget)
    engine = widget.engine
    page = engine.create_submap(child)
    widget.navigate_to(page)
    original, revision = engine.document, engine.revision
    mime = widget.create_branch_drag([source], origin="sidebar")
    point = body(widget, widget.document_view.root_id)
    assert drag_event(widget, mime, point, "enter").isAccepted()
    assert not drag_event(widget, mime, point, "move").isAccepted()
    assert not drag_event(widget, mime, point, "drop").isAccepted()
    assert not widget.view.node_drag.active
    widget.navigate_to(engine.get_node(child).detail_page_id)
    assert not drag_event(widget, mime, body(widget, widget.document_view.root_id), "enter").isAccepted()
    other = MindMapWidget(MindMapEngine(engine.document), toolbar=False)
    qtbot.addWidget(other)
    other.show()
    QApplication.processEvents()
    try:
        assert not drag_event(other, mime, body(other, other.engine.root_id), "enter").isAccepted()
        assert not other.view.node_drag.active
    finally:
        other.dispose()
    assert engine.document == original and engine.revision == revision


def test_native_canvas_root_cannot_drop_into_its_own_descendant(canvas):
    widget, _, _ = canvas
    _, _, internal = prepare(widget)
    descendant = widget.engine.add_child(internal, "自身后代")
    original, revision = widget.engine.document, widget.engine.revision
    mime = widget.create_branch_drag([internal], origin="canvas")
    point = body(widget, descendant)
    assert drag_event(widget, mime, point, "enter").isAccepted()
    assert not drag_event(widget, mime, point, "move").isAccepted()
    assert not drag_event(widget, mime, point, "drop").isAccepted()
    assert widget.engine.document == original and widget.engine.revision == revision
    assert not widget.view.node_drag.active


@pytest.mark.parametrize("change", ["content", "page"])
def test_source_preview_layout_callback_cannot_install_stale_external_gesture(canvas, monkeypatch, change):
    widget, source, _ = canvas
    target, _, _ = prepare(widget)
    mime, point = widget.create_branch_drag([source], origin="sidebar"), body(widget, target)
    original_layout = widget.layout_registry.layout
    changed = False

    def source_layout(document, sizes, **kwargs):
        nonlocal changed
        layout = original_layout(document, sizes, **kwargs)
        if not changed and source in document.nodes:
            changed = True
            if change == "content":
                widget.engine.update_node(target, text="宿主回调更新")
            else:
                widget.show_inspiration(source)
        return layout

    monkeypatch.setattr(widget.layout_registry, "layout", source_layout)
    assert not drag_event(widget, mime, point, "enter").isAccepted()
    assert changed and not widget.view.node_drag.active
    assert source in widget.engine.document.inspiration.root_ids


def test_large_source_ghost_only_paints_visible_nodes(canvas, monkeypatch):
    widget, source, _ = canvas
    target, _, _ = prepare(widget)
    with widget.engine.transaction("准备大源树"):
        for index in range(1000):
            widget.engine.add_child(source, f"源主题 {index}")
    original = widget.engine.document
    mime = widget.create_branch_drag([source], origin="sidebar")
    assert drag_event(widget, mime, body(widget, target), "enter").isAccepted()
    control = widget.view.node_drag
    visual_type = type(control._source.visuals[source])
    original_paint, painted = visual_type.paint, []

    def paint_visible(visual, painter, node, *args, **kwargs):
        painted.append(node.id)
        return original_paint(visual, painter, node, *args, **kwargs)

    monkeypatch.setattr(visual_type, "paint", paint_visible)
    widget.view.viewport().grab()
    assert 0 < len(set(painted) & control._ghost_nodes.keys()) < 100
    assert widget.engine.document == original
    widget.cancel_branch_transfer_preview()


def test_native_source_ghost_keeps_horizontal_branch_visible(canvas, monkeypatch):
    from mindmap_qt_canvas import dragging

    widget, source, _ = canvas
    target, _, _ = prepare(widget)
    mime = widget.create_branch_drag([source], origin="sidebar")
    assert drag_event(widget, mime, body(widget, target), "enter").isAccepted()
    original_paint, branches = dragging.paint_branch, []

    def paint_branch(painter, branch):
        branches.append(branch)
        original_paint(painter, branch)

    monkeypatch.setattr(dragging, "paint_branch", paint_branch)
    widget.view.viewport().grab()
    assert branches
    widget.cancel_branch_transfer_preview()
