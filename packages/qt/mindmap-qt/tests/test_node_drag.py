"""分支拖动验收：固定目标、放下重排、结构结果、文档隔离和取消边界。"""

import pytest
from mindmap_engine import (
    BranchPath,
    BranchStyle,
    Curve,
    LayoutConfig,
    LayoutInfo,
    MindMapEngine,
    MindMapError,
    Theme,
    TreeLayout,
)
from mindmap_qt import MindMapWidget
from mindmap_qt.dragging import _DropResolver
from mindmap_qt.rendering import branch_path
from PySide6.QtCore import QAbstractAnimation, QBuffer, QEvent, QIODevice, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QFocusEvent, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture
def canvas(qtbot):
    """源分支包含两层后代，另有空父节点和带孩子的同级，覆盖结构变化后的测量。"""
    engine = MindMapEngine()
    a, b, c = [engine.add_child(engine.root_id, text) for text in ("源分支", "目标主题", "另一分支")]
    x = engine.add_child(a, "子主题")
    y = engine.add_child(x, "深层主题")
    z = engine.add_child(c, "另一子主题")
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine, toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(1200, 800)
    widget.show()
    widget.activateWindow()
    widget.view.setFocus()
    QApplication.processEvents()
    widget.view.resetTransform()
    widget.view.centerOn(180, 0)
    yield widget, (a, b, c, x, y, z)
    widget.dispose()


def mouse(widget, kind, point, buttons=Qt.MouseButton.LeftButton):
    """显式给出按钮状态，经视口发送事件，避免测试依赖系统鼠标位置。"""
    button = (
        Qt.MouseButton.LeftButton
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)
        else Qt.MouseButton.NoButton
    )
    if kind == QEvent.Type.MouseButtonRelease:
        buttons = Qt.MouseButton.NoButton
    view = widget.view
    event = QMouseEvent(
        kind,
        QPointF(point),
        QPointF(view.viewport().mapToGlobal(point)),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)


def body(widget, key, fraction=0.5):
    """选择正文中的任意位置；旧的四分之一边缘区域也统一接为子主题。"""
    item = widget.node_items[key]
    x, y = item.visual.width / 2, item.visual.height / 2
    if widget.engine.document.layout.direction == "down":
        x = item.visual.width * fraction
    else:
        y = item.visual.height * fraction
    return widget.view.mapFromScene(item.mapToScene(QPointF(x, y)))


def sorting(widget, key, position, distance=12):
    """从正式节点外缘选取同级落点，窄间隙取中点，不借用原来的正文分区。"""
    document = widget._document
    nodes = widget.layout_result.nodes
    rect = nodes[key]
    down = document.layout.direction == "down"
    before = position == "before"
    sides = document.branch_sides()
    siblings = [
        child
        for child in document.nodes[document.parents()[key]].children
        if child in nodes and sides[child] == sides[key]
    ]
    index = siblings.index(key)
    neighbor_index = index - 1 if before else index + 1
    offset = distance / widget.view.transform().m11()
    edge = (rect.x if before else rect.right) if down else (rect.y if before else rect.bottom)
    if 0 <= neighbor_index < len(siblings):
        neighbor = nodes[siblings[neighbor_index]]
        other = (
            (neighbor.right if before else neighbor.x)
            if down
            else (neighbor.bottom if before else neighbor.y)
        )
        gap = edge - other if before else other - edge
        offset = min(offset, gap / 2)
    axis = edge + (-offset if before else offset)
    point = QPointF(axis, rect.center[1]) if down else QPointF(rect.center[0], axis)
    return widget.view.mapFromScene(point)


def begin(widget, key, *, select=True):
    if select:
        widget.select_nodes([key])
    start = body(widget, key)
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    mouse(widget, QEvent.Type.MouseMove, start + QPoint(25, 0))
    return widget.view.node_drag


def release(widget, point):
    mouse(widget, QEvent.Type.MouseMove, point)
    mouse(widget, QEvent.Type.MouseButtonRelease, point)


def assert_clean(widget):
    """完成已提交动画，再检查计时器、透明度、可见集合及正式几何的残留状态。"""
    control = widget.view.node_drag
    assert not control.active and control.target is None
    assert not control._scroll_timer.isActive()
    if control._animation.state() == QAbstractAnimation.State.Running:
        control._animation.setCurrentTime(140)
    assert control._animation.state() == QAbstractAnimation.State.Stopped
    assert control._reflow is None
    assert set(widget.node_items) == set(widget.layout_result.nodes)
    for key, rect in widget.layout_result.nodes.items():
        assert widget.node_items[key].pos() == QPointF(rect.x, rect.y)
    assert all(item.opacity() == 1 for item in (*widget.node_items.values(), *widget.branch_items.values()))
    assert widget.scene.mouseGrabberItem() is None


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("position", ["inside", "before", "after"])
def test_drop_structure_four_directions_and_one_undo(canvas, direction, position):
    widget, (a, b, c, x, y, z) = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction))
    widget.engine.replace_document(widget.engine.document)
    # 用末尾分支测试 before，其他情况移动首分支，均产生真实变化。
    source = c if position == "before" else a
    end = body(widget, b) if position == "inside" else sorting(widget, b, position)
    before, revision = widget.engine.document, widget.engine.revision
    control = begin(widget, source)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert control.target is not None
    assert widget.engine.document == before and widget.engine.revision == revision
    assert control._animation.state() == QAbstractAnimation.State.Stopped
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    assert control._animation.state() == QAbstractAnimation.State.Running
    document = widget.engine.document
    if position == "inside":
        assert document.nodes[b].children == [a]
        assert document.root.children == [b, c]
        assert document.nodes[a].children == [x] and document.nodes[x].children == [y]
    else:
        assert document.root.children == ([a, c, b] if position == "before" else [b, a, c])
        if direction == "both":
            assert document.branch_sides()[source] == before.branch_sides()[b]
            assert all(
                document.branch_sides()[key] == before.branch_sides()[key]
                for key in (a, b, c)
                if key != source
            )
    assert widget.engine.revision == revision + 1
    assert widget.selected_node_ids() == [source]
    assert_clean(widget)
    assert widget.engine.undo() and widget.engine.document == before
    assert not widget.engine.can_undo
    assert widget.engine.redo() and widget.engine.document == document


def test_hover_keeps_all_geometry_and_content_fixed_without_layout_or_events(canvas, monkeypatch, qtbot):
    widget, (a, b, c, *_) = canvas
    before, revision, layout = widget.engine.document, widget.engine.revision, widget.layout_result
    events = []
    unsubscribe = widget.engine.subscribe(events.append)
    calls = []
    original = widget.layout_registry.layout

    def track(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(widget.layout_registry, "layout", track)
    items = {key: (item, item.pos(), item.node, item.visual) for key, item in widget.node_items.items()}
    branches = {key: item.path() for key, item in widget.branch_items.items()}
    scene_rect = widget.scene.sceneRect()
    end = body(widget, b)
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert control.target is not None
    assert widget.node_items[a].opacity() < 0.5
    qtbot.wait(180)
    for delta in (QPoint(1, 0), QPoint(2, 0), QPoint(0, 0)):
        mouse(widget, QEvent.Type.MouseMove, end + delta)
    mouse(widget, QEvent.Type.MouseMove, body(widget, c))
    mouse(widget, QEvent.Type.MouseMove, end)
    assert not calls and control._animation.state() == QAbstractAnimation.State.Stopped
    assert items == {
        key: (item, item.pos(), item.node, item.visual) for key, item in widget.node_items.items()
    }
    assert branches == {key: item.path() for key, item in widget.branch_items.items()}
    assert widget.scene.sceneRect() == scene_rect
    assert widget.layout_result is layout and widget.engine.document == before
    assert widget.engine.revision == revision and not widget.engine.is_dirty and not events
    assert widget.selected_node_ids() == [a]
    widget.view.cancel_interaction()
    unsubscribe()
    assert_clean(widget)


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
def test_reflow_starts_from_fixed_nodes_and_released_ghost_then_eases_to_formal_layout(canvas, direction):
    widget, (a, b, *_rest) = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction))
    widget.engine.replace_document(widget.engine.document)
    before = widget.layout_result
    end = body(widget, b)
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    moving = set(control._resolver.moving)
    offset = QPointF(control._offset)
    assert not control._connection_hint().isEmpty()
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    assert control._animation.state() == QAbstractAnimation.State.Running
    assert not control.active and control.target is None
    assert not control._scroll_timer.isActive() and widget.scene.mouseGrabberItem() is None
    for key, rect in before.nodes.items():
        expected = QPointF(rect.x, rect.y) + (offset if key in moving else QPointF())
        assert widget.node_items[key].pos() == expected
    final = widget.layout_result
    assert final != before
    after, revision = widget.engine.document, widget.engine.revision
    # 140 毫秒 OutCubic 在一半时间已完成 7/8 位移；动画仅改变展示图元。
    control._animation.setCurrentTime(70)
    for key, rect in final.nodes.items():
        origin = control._reflow.start_nodes[key]
        assert widget.node_items[key].x() == pytest.approx(origin.x + (rect.x - origin.x) * 0.875)
        assert widget.node_items[key].y() == pytest.approx(origin.y + (rect.y - origin.y) * 0.875)
    assert widget.engine.document == after and widget.engine.revision == revision
    assert widget.layout_result is final
    control._animation.setCurrentTime(140)
    assert widget.scene.sceneRect().x() == final.bounds.x
    assert widget.scene.sceneRect().width() == final.bounds.width
    assert_clean(widget)


@pytest.mark.parametrize(
    "interruption", ["external", "undo", "hide", "readonly", "focus", "dispose", "selection"]
)
def test_post_drop_animation_interruptions_settle_without_reverting_successful_move(canvas, interruption):
    widget, (a, b, *_rest) = canvas
    before = widget.engine.document
    end = body(widget, b)
    control = begin(widget, a)
    release(widget, end)
    assert control._animation.state() == QAbstractAnimation.State.Running
    control._animation.setCurrentTime(40)
    after = widget.engine.document
    if interruption == "external":
        widget.engine.update_node(b, text="动画期间的外部修改")
        after = widget.engine.document
    elif interruption == "undo":
        assert widget.engine.undo()
        after = before
    elif interruption == "hide":
        widget.hide()
    elif interruption == "readonly":
        widget.set_editable(False)
    elif interruption == "focus":
        QApplication.sendEvent(widget.view, QFocusEvent(QEvent.Type.FocusOut))
    elif interruption == "dispose":
        widget.dispose()
    else:
        widget.select_nodes([b])
    assert control._reflow is None and control._animation.state() == QAbstractAnimation.State.Stopped
    assert widget.engine.document == after
    assert_clean(widget)


def test_saved_notification_keeps_post_drop_animation_and_new_press_settles_it(canvas):
    widget, (a, b, *_rest) = canvas
    end = body(widget, b)
    control = begin(widget, a)
    release(widget, end)
    widget.engine.mark_saved()
    assert control._reflow is not None and control._animation.state() == QAbstractAnimation.State.Running
    control._animation.setCurrentTime(40)
    # 新按下先完成上次布局，再按最终位置选择并启动下一次拖动。
    rect = widget.layout_result.nodes[a]
    start = widget.view.mapFromScene(QPointF(*rect.center))
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    assert control._reflow is None and widget.view.press_node == a
    mouse(widget, QEvent.Type.MouseMove, start + QPoint(25, 0))
    assert control.active and control._animation.state() == QAbstractAnimation.State.Stopped
    widget.view.cancel_interaction()
    assert not widget.engine.is_dirty
    assert_clean(widget)


def test_multi_selection_deduplicates_descendants_and_preserves_preorder(canvas):
    widget, (a, b, c, x, y, z) = canvas
    end = body(widget, b)
    widget.select_nodes([c, x, a])
    control = begin(widget, a, select=False)
    assert control._resolver.roots == (a, c)
    assert set(control._ghost_nodes) == {a, c, x, y, z}
    assert control.ghost.acceptedMouseButtons() == Qt.MouseButton.NoButton
    release(widget, end)
    assert widget.engine.get_node(b).children == [a, c]
    assert widget.engine.get_node(a).children == [x]
    assert widget.selected_node_ids() == [a, x, c]
    assert_clean(widget)


def test_revisiting_candidates_only_changes_hint_and_keeps_original_hit_regions(canvas):
    widget, (a, b, c, *_rest) = canvas
    before = widget.engine.document
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, body(widget, b))
    first = control.target
    assert first.proposal.parent_id == b
    regions = dict(control._resolver.node_regions)
    mouse(widget, QEvent.Type.MouseMove, body(widget, c))
    assert control.target.proposal.parent_id == c
    mouse(widget, QEvent.Type.MouseMove, body(widget, b))
    assert control.target == first
    assert control._resolver.node_regions == regions
    assert control._animation.state() == QAbstractAnimation.State.Stopped
    assert widget.engine.document == before
    widget.view.cancel_interaction()
    assert_clean(widget)


def test_failed_injected_layout_is_checked_only_on_release_and_leaves_document_unchanged(canvas, monkeypatch):
    widget, (a, b, *_rest) = canvas
    before = widget.engine.document
    errors = []
    widget.error_occurred.connect(errors.append)
    end = body(widget, b)
    begin(widget, a)

    def fail(*args, **kwargs):
        raise MindMapError("注入布局拒绝候选")

    monkeypatch.setattr(widget.layout_registry, "layout", fail)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert not errors and widget.view.node_drag.target is not None
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    assert errors == ["注入布局拒绝候选"]
    assert widget.engine.document == before and not widget.engine.can_undo
    assert_clean(widget)


@pytest.mark.parametrize("side", ["left", "right"])
def test_both_layout_empty_side_and_undo(canvas, side):
    widget, (a, b, c, *_) = canvas
    widget.engine.set_layout(LayoutConfig(direction="both"))
    other = "right" if side == "left" else "left"
    for key in (a, b, c):
        widget.engine.update_node(key, direction=other)
    widget.engine.replace_document(widget.engine.document)
    before = widget.engine.document
    root = widget.layout_result.nodes[widget.engine.root_id]
    point = QPointF(root.x - 48 if side == "left" else root.right + 48, root.center[1])
    end = widget.view.mapFromScene(point)
    control = begin(widget, a)
    release(widget, end)
    document = widget.engine.document
    assert document.layout == before.layout
    assert document.branch_sides()[a] == side
    assert document.branch_sides()[b] == document.branch_sides()[c] == other
    assert widget.engine.undo() and widget.engine.document == before
    assert not control.active
    assert_clean(widget)


@pytest.mark.parametrize("scale", [0.1, 1])
def test_both_side_lock_can_switch_across_center_in_one_gesture(canvas, scale):
    widget, (a, b, c, *_rest) = canvas
    widget.engine.set_layout(LayoutConfig(direction="both"))
    for key in (a, b, c):
        widget.engine.update_node(key, direction="right")
    widget.view.resetTransform()
    widget.view.scale(scale, scale)
    widget.view.update_navigation_rect(center=QPointF())
    root = widget.layout_result.nodes[widget.engine.root_id]
    control = begin(widget, a)
    right = widget.view.mapFromScene(QPointF(root.right + 32, root.center[1]))
    left = widget.view.mapFromScene(QPointF(root.x - 32, root.center[1]))
    mouse(widget, QEvent.Type.MouseMove, right)
    assert control.target.proposal.direction == "right"
    mouse(widget, QEvent.Type.MouseMove, left)
    assert control.target.proposal.direction == "left"
    mouse(widget, QEvent.Type.MouseButtonRelease, left)
    assert widget.engine.document.branch_sides()[a] == "left"
    assert_clean(widget)


@pytest.mark.parametrize("scale", [0.1, 0.5, 1, 4])
def test_full_body_and_external_sorting_can_switch_immediately_on_small_nodes(canvas, scale):
    widget, (a, b, c, *_rest) = canvas
    widget.view.resetTransform()
    widget.view.scale(scale, scale)
    widget.view.update_navigation_rect(center=QPointF(*widget.layout_result.nodes[b].center))
    control = begin(widget, c)
    mouse(widget, QEvent.Type.MouseMove, body(widget, b))
    assert control.target.proposal.parent_id == b
    for fraction in (0.1, 0.9):
        mouse(widget, QEvent.Type.MouseMove, body(widget, b, fraction))
        assert control.target.position == "inside" and control.target.proposal.parent_id == b
    end = sorting(widget, b, "before", distance=6)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert control.target is not None and control.target.position == "before"
    assert control._hint_text() == "↑ 插到前面"
    mouse(widget, QEvent.Type.MouseMove, body(widget, b, 0.1))
    assert control.target.position == "inside" and control._hint_text() == "＋ 作为子主题"
    mouse(widget, QEvent.Type.MouseMove, end)
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    assert widget.engine.document.root.children == [a, c, b]
    assert_clean(widget)


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("scale", [0.1, 1, 4])
def test_every_part_of_body_receives_children_despite_expanded_sorting_regions(canvas, direction, scale):
    widget, (a, b, c, *_rest) = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction))
    resolver = _DropResolver(widget._document, widget.layout_result, [c], None)
    resolver.rebuild(widget.layout_result.nodes)
    rect = widget.layout_result.nodes[b]
    # 九个正文探针同时覆盖四角、边缘和中心，低倍率下相邻排序区可能相交，
    # 仍必须把每个实际落在正文内的点判为接收子主题，而不是同级移动。
    for x in (0.02, 0.5, 0.98):
        for y in (0.02, 0.5, 0.98):
            target, direct = resolver.resolve(
                QPointF(rect.x + rect.width * x, rect.y + rect.height * y), scale
            )
            assert direct and target.position == "inside" and target.proposal.parent_id == b


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_external_sorting_regions_have_cross_axis_tolerance_and_body_has_priority(
    canvas, direction, position
):
    widget, (a, b, c, *_rest) = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction))
    resolver = _DropResolver(widget._document, widget.layout_result, [c], None)
    resolver.rebuild(widget.layout_result.nodes)
    rect = widget.layout_result.nodes[b]
    before = position == "before"
    # 同级落区比短条更宽，越过正文宽度 8 像素仍可直接命中；沿排序轴只越出
    # 4 像素，保证落点紧邻 b，而不是相同间隙另一端的兄弟主题。
    point = (
        QPointF(rect.x - 4 if before else rect.right + 4, rect.bottom + 8)
        if direction == "down"
        else QPointF(rect.right + 8, rect.y - 4 if before else rect.bottom + 4)
    )
    target, direct = resolver.resolve(point, 1)
    assert direct and target.reference == b and target.position == position
    assert target.proposal.parent_id == widget.engine.root_id
    body_point = QPointF(rect.x + rect.width * 0.02, rect.y + rect.height * 0.02)
    target, direct = resolver.resolve(body_point, 1)
    assert direct and target.position == "inside" and target.proposal.parent_id == b


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
def test_gap_and_body_labels_explain_current_operation_without_moving_nodes(canvas, direction):
    widget, (a, b, c, *_rest) = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction))
    before = {key: item.pos() for key, item in widget.node_items.items()}
    control = begin(widget, c)
    for reference, position in ((b, "before"), (a, "after")):
        point = sorting(widget, reference, position, distance=6)
        mouse(widget, QEvent.Type.MouseMove, point)
        assert control.target.position == position
        expected = (
            ("← 插到左侧" if position == "before" else "→ 插到右侧")
            if direction == "down"
            else ("↑ 插到前面" if position == "before" else "↓ 插到后面")
        )
        assert control._hint_text() == expected
        assert control._connection_hint().isEmpty() and not control._insertion_hint().isEmpty()
        assert not control.ghost._label_bounds.isEmpty()
        # 文字所在区域也不接收鼠标，既不成为节点，也不进入落点计算。
        assert control.ghost.acceptedMouseButtons() == Qt.MouseButton.NoButton
    mouse(widget, QEvent.Type.MouseMove, body(widget, b, 0.95))
    assert control._hint_text() == "＋ 作为子主题"
    assert not control._connection_hint().isEmpty() and control._insertion_hint().isEmpty()
    assert before == {key: item.pos() for key, item in widget.node_items.items()}
    widget.view.cancel_interaction()
    assert_clean(widget)


def test_label_stays_inside_viewport_and_keeps_readable_size_across_zoom(canvas):
    widget, (a, b, c, *_rest) = canvas
    sizes = []
    for scale in (0.1, 0.5, 1, 2, 4):
        widget.view.resetTransform()
        widget.view.scale(scale, scale)
        widget.view.update_navigation_rect(center=QPointF(*widget.layout_result.nodes[b].center))
        control = begin(widget, a)
        mouse(widget, QEvent.Type.MouseMove, body(widget, b))
        assert control._hint_text() == "＋ 作为子主题"
        box = control.ghost._label_bounds
        sizes.append((box.width() * scale, box.height() * scale))
        screen = widget.view.mapFromScene(box).boundingRect()
        assert widget.view.viewport().rect().contains(screen)
        assert sizes[-1][0] >= 80 and sizes[-1][1] >= 24
        widget.view.cancel_interaction()
    assert all(
        width == pytest.approx(sizes[0][0]) and height == pytest.approx(sizes[0][1])
        for width, height in sizes
    )
    assert_clean(widget)


@pytest.mark.parametrize("target", ["source", "descendant", "same_position", "blank", "outside"])
def test_invalid_and_unchanged_drops_have_no_history(canvas, target):
    widget, (a, b, c, x, *_) = canvas
    before, revision = widget.engine.document, widget.engine.revision
    end = {
        "source": body(widget, a),
        "descendant": body(widget, x),
        "same_position": sorting(widget, b, "before"),
        "blank": QPoint(40, 40),
        "outside": QPoint(-50, -50),
    }[target]
    begin(widget, a)
    release(widget, end)
    assert widget.engine.document == before and widget.engine.revision == revision
    assert not widget.engine.can_undo
    assert_clean(widget)


def test_noop_in_both_does_not_freeze_automatic_directions(canvas):
    widget, (a, b, c, *_) = canvas
    widget.engine.set_layout(LayoutConfig(direction="both"))
    widget.engine.replace_document(widget.engine.document)
    before = widget.engine.document
    root = widget.layout_result.nodes[widget.engine.root_id]
    begin(widget, c)
    release(widget, widget.view.mapFromScene(QPointF(root.x - 48, root.center[1])))
    assert widget.engine.document == before
    assert all(node.direction is None for node in widget.engine.document.nodes.values())
    assert not widget.engine.can_undo
    assert_clean(widget)


@pytest.mark.parametrize("direction", ["right", "left", "down"])
def test_single_direction_does_not_switch_layout_or_create_opposite_side(canvas, direction):
    widget, (a, *_rest) = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction))
    widget.engine.replace_document(widget.engine.document)
    before = widget.engine.document
    root = widget.layout_result.nodes[widget.engine.root_id]
    point = (
        QPointF(root.right + 48, root.center[1])
        if direction == "left"
        else QPointF(root.center[0], root.y - 48)
        if direction == "down"
        else QPointF(root.x - 48, root.center[1])
    )
    begin(widget, a)
    release(widget, widget.view.mapFromScene(point))
    assert widget.engine.document == before and not widget.engine.can_undo
    assert_clean(widget)


@pytest.mark.parametrize("scale", [0.1, 0.5, 1, 2, 4])
def test_drag_and_drop_with_actual_view_transform(canvas, scale):
    widget, (a, b, *_rest) = canvas
    widget.view.resetTransform()
    widget.view.scale(scale, scale)
    rect = widget.layout_result.nodes[a].union(widget.layout_result.nodes[b])
    widget.view.update_navigation_rect(center=QPointF(*rect.center))
    begin(widget, a)
    release(widget, body(widget, b))
    assert widget.engine.get_node(b).children == [a]
    assert_clean(widget)


def test_folded_source_moves_complete_subtree_but_ghost_only_shows_visible_nodes(canvas):
    widget, (a, b, c, x, y, *_rest) = canvas
    widget.engine.set_expanded(a, False)
    before = widget.engine.document
    end = body(widget, b)
    control = begin(widget, a)
    assert set(control._ghost_nodes) == {a}
    release(widget, end)
    assert widget.engine.get_node(b).children == [a]
    assert widget.engine.get_node(a).children == [x] and widget.engine.get_node(x).children == [y]
    assert not widget.engine.get_node(a).expanded and x not in widget.node_items
    assert widget.engine.undo() and widget.engine.document == before
    assert_clean(widget)


@pytest.mark.parametrize(
    "interruption", ["escape", "focus", "capture", "hide", "readonly", "dispose", "buttons"]
)
def test_interruptions_keep_target_folded_and_stop_timers(canvas, qtbot, interruption):
    widget, (a, b, *_rest) = canvas
    hidden = widget.engine.add_child(b, "折叠目标中的原有孩子")
    widget.engine.set_expanded(b, False)
    widget.engine.replace_document(widget.engine.document)
    end = body(widget, b)
    before = widget.engine.document
    actions = []
    widget.action("unfocus").triggered.connect(lambda: actions.append(1))
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert hidden not in widget.node_items and not widget.node_items[b].node.expanded
    assert control._animation.state() == QAbstractAnimation.State.Stopped
    if interruption == "escape":
        qtbot.keyClick(widget.view.viewport(), Qt.Key.Key_Escape)
    elif interruption == "focus":
        QApplication.sendEvent(widget.view, QFocusEvent(QEvent.Type.FocusOut))
    elif interruption == "capture":
        QApplication.sendEvent(widget.view.viewport(), QEvent(QEvent.Type.UngrabMouse))
    elif interruption == "hide":
        widget.hide()
    elif interruption == "readonly":
        widget.set_editable(False)
    elif interruption == "dispose":
        widget.dispose()
    else:
        mouse(widget, QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton)
    assert widget.engine.document == before and not widget.engine.can_undo and not actions
    assert not control.active and hidden not in widget.node_items
    assert_clean(widget)


def test_saved_notification_keeps_preview_external_change_cancels(canvas):
    widget, (a, b, *_rest) = canvas
    end = body(widget, b)
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    widget.engine.mark_saved()
    assert control.active and control.target is not None
    widget.engine.update_node(b, text="外部修改")
    assert widget.engine.get_node(a).children and a in widget.engine.document.root.children
    assert not control.active
    assert_clean(widget)


@pytest.mark.parametrize("outside", [False, True])
def test_release_rechecks_position_without_a_matching_mouse_move(canvas, outside):
    widget, (a, b, c, *_rest) = canvas
    before = widget.engine.document
    end = body(widget, b)
    begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    # 最后的释放可以直接出现在另一个主题或画布外，不能提交上一条移动事件的落点。
    release_at = QPoint(-20, -20) if outside else body(widget, c)
    mouse(widget, QEvent.Type.MouseButtonRelease, release_at)
    if outside:
        assert widget.engine.document == before
    else:
        assert widget.engine.get_node(c).children[-1] == a
    assert_clean(widget)


def test_content_change_during_capture_release_invalidates_prepared_drop(canvas, monkeypatch):
    widget, (a, b, *_rest) = canvas
    end = body(widget, b)
    control = begin(widget, a)
    original = control.commit

    def interrupted(drop):
        widget.engine.update_node(b, text="捕获释放时的宿主修改")
        return original(drop)

    monkeypatch.setattr(control, "commit", interrupted)
    release(widget, end)
    assert a in widget.engine.document.root.children
    assert widget.engine.get_node(b).text == "捕获释放时的宿主修改"
    assert widget.engine.undo_label == "update_node"
    assert_clean(widget)


def test_folded_target_expands_only_after_drop_and_undo_redo_restores_state(canvas, qtbot):
    widget, (a, b, *_rest) = canvas
    child = widget.engine.add_child(b, "放下后才显示的原有孩子")
    widget.engine.set_expanded(b, False)
    widget.engine.replace_document(widget.engine.document)
    before = widget.engine.document
    end = body(widget, b)
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    qtbot.wait(180)
    assert child not in widget.node_items and not widget.node_items[b].node.expanded
    assert child not in control._resolver.node_regions and widget.engine.document == before
    assert control._animation.state() == QAbstractAnimation.State.Stopped
    release(widget, end)
    after = widget.engine.document
    assert after.nodes[b].children == [child, a]
    assert widget.node_items[b].node.expanded and child in widget.node_items
    assert control._animation.state() == QAbstractAnimation.State.Running
    assert widget.engine.undo() and widget.engine.document == before
    assert child not in widget.node_items and not widget.engine.can_undo
    assert widget.engine.redo() and widget.engine.document == after
    assert_clean(widget)


def test_focused_drop_keeps_ancestors_outside_focus_folded(canvas):
    widget, (a, b, c, x, y, *_rest) = canvas
    target = widget.engine.add_child(x, "聚焦范围内的目标")
    widget.engine.set_expanded(a, False)
    widget.focus_node(x)
    before = widget.engine.document
    end = body(widget, target)
    begin(widget, y)
    release(widget, end)
    assert widget.engine.get_node(target).children == [y]
    assert not widget.engine.get_node(a).expanded and widget.focus_id == x
    assert widget.engine.undo() and widget.engine.document == before
    assert_clean(widget)


def test_rejected_direction_update_rolls_back_entire_drop(canvas):
    widget, (a, b, c, *_rest) = canvas
    widget.engine.set_layout(LayoutConfig(direction="both"))
    widget.engine.replace_document(widget.engine.document)
    before, revision = widget.engine.document, widget.engine.revision
    calls, errors = [], []

    def reject(name, parameters):
        calls.append(name)
        return not (name == "update_node" and parameters["node_id"] == a)

    widget.engine.before_operation = reject
    widget.error_occurred.connect(errors.append)
    end = sorting(widget, b, "after")
    begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert not calls
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    assert calls and "move_nodes" in calls and errors
    assert widget.engine.document == before and widget.engine.revision == revision
    assert widget.selected_node_ids() == [a] and not widget.engine.can_undo
    assert_clean(widget)


def test_rejected_move_does_not_expand_folded_target_or_start_animation(canvas):
    widget, (a, b, *_rest) = canvas
    hidden = widget.engine.add_child(b, "保持折叠的孩子")
    widget.engine.set_expanded(b, False)
    widget.engine.replace_document(widget.engine.document)
    before = widget.engine.document
    widget.engine.before_operation = lambda name, parameters: name != "move_nodes"
    errors = []
    widget.error_occurred.connect(errors.append)
    end = body(widget, b)
    control = begin(widget, a)
    release(widget, end)
    assert errors and widget.engine.document == before and not widget.engine.can_undo
    assert hidden not in widget.node_items and not widget.node_items[b].node.expanded
    assert control._reflow is None and control._animation.state() == QAbstractAnimation.State.Stopped
    assert_clean(widget)


def test_injected_layout_is_used_only_after_release_and_its_paths_survive_animation(canvas):
    widget, (a, b, *_rest) = canvas
    calls = []

    class CustomLayout(TreeLayout):
        """故意改变控制点，以便检测动画是否错误地重新套用内置树路由。"""

        info = LayoutInfo("custom", "自定义连线布局", LayoutConfig(algorithm_id="custom"))

        def validate_config(self, config):
            config.validate()

        def layout(self, document, measurements=None, **kwargs):
            calls.append(1)
            document.layout.algorithm_id = "tree"
            result = super().layout(document, measurements, **kwargs)
            for key, curve in result.branches.items():
                start, end = (
                    (curve.start, curve.end)
                    if isinstance(curve, BranchPath)
                    else (curve.points[0], curve.points[-1])
                )
                result.branches[key] = Curve(
                    (start, (start[0] + 10, start[1] + 20), (end[0] - 10, end[1]), end), curve.color
                )
            return result

    widget.layout_registry.register(CustomLayout())
    widget.set_layout(CustomLayout.info.default_config)
    widget.engine.replace_document(widget.engine.document)
    calls.clear()
    end = body(widget, b)
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert not calls
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    count = len(calls)
    assert count == 2 and control._animation.state() == QAbstractAnimation.State.Running
    final = widget.layout_result
    for time in (20, 70, 140):
        control._animation.setCurrentTime(time)
        assert len(calls) == count
    assert all(widget.branch_items[key].path() == branch_path(curve) for key, curve in final.branches.items())
    assert_clean(widget)


def test_post_drop_export_uses_committed_geometry_during_animation(canvas, tmp_path):
    widget, (a, b, *_rest) = canvas
    end = body(widget, b)
    control = begin(widget, a)
    release(widget, end)
    control._animation.setCurrentTime(35)
    during, finished = tmp_path / "重排期间.svg", tmp_path / "重排完成.svg"
    widget.export(during)
    assert control._reflow is not None
    control._animation.setCurrentTime(140)
    widget.export(finished)
    assert during.read_bytes() == finished.read_bytes()
    assert_clean(widget)


@pytest.mark.parametrize("scale", [0.1, 0.5, 1, 2, 4])
def test_snap_distances_are_viewport_logical_pixels(canvas, scale):
    widget, (a, b, *_rest) = canvas
    document = widget.engine.document
    resolver = _DropResolver(document, widget.layout_result, [a], None)
    resolver.rebuild(widget.layout_result.nodes)
    # 在低倍率下其他分支也可能更近；隔离本主题，验证其 24 像素边界而非竞争结果。
    resolver.targets = [target for target in resolver.targets if target.proposal.parent_id == b]
    rect = widget.layout_result.nodes[b]
    point = QPointF(rect.right + 23 / scale, rect.center[1])
    target, _ = resolver.resolve(point, scale)
    assert target is not None and target.proposal.parent_id == b
    target, _ = resolver.resolve(QPointF(rect.right + 25 / scale, rect.center[1]), scale)
    assert target is None


def test_target_hysteresis_and_far_leave_restore_layout(canvas):
    widget, (a, b, *_rest) = canvas
    rect = widget.layout_result.nodes[b]
    control = begin(widget, a)
    enter = widget.view.mapFromScene(QPointF(rect.right + 23, rect.center[1]))
    mouse(widget, QEvent.Type.MouseMove, enter)
    assert control.target is not None
    proposal = control.target.proposal
    mouse(widget, QEvent.Type.MouseMove, enter + QPoint(10, 0))
    assert control.target is not None and control.target.proposal == proposal
    mouse(widget, QEvent.Type.MouseMove, QPoint(40, 40))
    assert control.target is None
    for key, rect in widget.layout_result.nodes.items():
        assert widget.node_items[key].pos() == QPointF(rect.x, rect.y)
    widget.view.cancel_interaction()
    assert_clean(widget)


def test_edge_scroll_moves_view_without_moving_document_and_stops_outside(canvas, qtbot):
    widget, (a, *_rest) = canvas
    before, revision = widget.engine.document, widget.engine.revision
    control = begin(widget, a)
    view = widget.view
    point = QPoint(view.viewport().width() - 2, view.viewport().height() // 2)
    old = view.horizontalScrollBar().value()
    mouse(widget, QEvent.Type.MouseMove, point)
    qtbot.waitUntil(lambda: view.horizontalScrollBar().value() > old)
    assert widget.engine.document == before and widget.engine.revision == revision
    mouse(widget, QEvent.Type.MouseMove, QPoint(-2, point.y()))
    assert not control._scroll_timer.isActive()
    widget.view.cancel_interaction()
    assert_clean(widget)


def test_root_and_focus_center_never_start_drag(canvas):
    widget, (a, b, c, x, *_rest) = canvas
    begin(widget, widget.engine.root_id)
    assert not widget.view.node_drag.active
    widget.view.cancel_interaction()
    widget.focus_node(a)
    begin(widget, a)
    assert not widget.view.node_drag.active
    assert widget.view.press_node is None
    widget.view.cancel_interaction()
    widget.select_nodes([a, x])
    begin(widget, x, select=False)
    assert not widget.view.node_drag.active
    widget.view.cancel_interaction()
    assert_clean(widget)


@pytest.mark.parametrize("kind", list(BranchStyle))
def test_all_branch_styles_animate_only_after_drop_and_preserve_final_geometry(canvas, kind):
    widget, (a, b, *_rest) = canvas
    widget.engine.set_branch_style([widget.engine.root_id], kind)
    end = body(widget, b)
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert control._animation.state() == QAbstractAnimation.State.Stopped
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    after, revision = widget.engine.document, widget.engine.revision
    assert control._animation.state() == QAbstractAnimation.State.Running
    for time in (0, 20, 70, 139, 140):
        control._animation.setCurrentTime(time)
        assert all(item.path().elementCount() > 0 for item in widget.branch_items.values())
        assert widget.engine.document == after and widget.engine.revision == revision
        if control._reflow is not None:
            # 所有连线锚点跟随父子节点，允许中间帧穿过正文，不重新校验树方向。
            for key, branch in widget.layout_result.branches.items():
                endpoint = branch.end if isinstance(branch, BranchPath) else branch.points[-1]
                final = widget.layout_result.nodes[key]
                expected = QPointF(*endpoint) + widget.node_items[key].pos() - QPointF(final.x, final.y)
                path = widget.branch_items[key].path()
                assert path.currentPosition() == expected
    assert all(
        widget.branch_items[key].path() == branch_path(branch)
        for key, branch in widget.layout_result.branches.items()
    )
    assert_clean(widget)


@pytest.mark.parametrize("dark", [False, True])
def test_ghost_contains_images_long_text_and_does_not_leak_to_export(canvas, tmp_path, dark):
    widget, (a, b, c, x, *_rest) = canvas
    image = QImage(60, 40, QImage.Format.Format_ARGB32)
    image.fill(QColor("#f02020"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    widget.engine.attach_image(x, bytes(buffer.data()), width=80)
    widget.engine.update_node(a, text="包含长文本的中文主题\n第二行说明")
    if dark:
        widget.engine.set_theme(Theme.dark())
    widget.engine.replace_document(widget.engine.document)
    before = widget.engine.document
    expected = tmp_path / "正式.svg"
    widget.export(expected)
    control = begin(widget, a)
    mouse(widget, QEvent.Type.MouseMove, body(widget, b))
    actual = tmp_path / "拖动期间.svg"
    widget.export(actual)
    assert actual.read_bytes() == expected.read_bytes()
    # 实际渲染预览，覆盖 QPainter 文字、图片、分支及落点图层的组合。
    rendered = widget.view.viewport().grab().toImage()
    assert not rendered.isNull()
    assert x in control._ghost_nodes and control._base.visuals[x].image is not None
    child_rect = control._ghost_nodes[x]
    center = control._base.visuals[x].image_rect.center()
    point = widget.view.mapFromScene(QPointF(child_rect.x, child_rect.y) + center + control._offset)
    ratio = rendered.devicePixelRatio()
    assert widget.view.viewport().rect().contains(point)
    pixel = rendered.pixelColor(round(point.x() * ratio), round(point.y() * ratio))
    assert pixel.red() > pixel.green() + 75 and pixel.red() > pixel.blue() + 75
    assert widget.engine.document == before
    widget.view.cancel_interaction()
    assert_clean(widget)
