"""从产品入口验证图与树共用画布、编辑命令和停靠侧栏。"""

import json
from dataclasses import replace

import pytest
from mindmap_app.app import build_window
from mindmap_core import GraphEdge, GraphGroup, MindMapEngine, MindMapError, NodeGraph
from mindmap_core.formats import load_directory, load_document, save_directory, save_document
from mindmap_core.formats.graph import format_graph_text, parse_graph_text
from mindmap_core.node_pages import graph_page_id
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFocusEvent, QImage, QInputMethodEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QGraphicsSimpleTextItem,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QToolButton,
)

SAMPLE = '节点 a "入口"\n节点 b "服务"\na -> b'
THREE_NODES = '节点 a "入口"\n节点 b "服务"\n节点 c "依赖"'
FOUR_NODES = THREE_NODES + '\n节点 d "结果"'
CHAIN = FOUR_NODES + '\na -> b\nb -> c\nc -> d'


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.resize(1200, 800)
    result.show()
    assert result.new_document(tmp_path / "主体")
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


def _viewport_region(view, point, radius):
    image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
    view.viewport().render(image)
    return image.copy(point.x() - radius, point.y() - radius, radius * 2 + 1, radius * 2 + 1)


def _pointer_event(canvas, kind, position, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton,
                   *, modifiers=Qt.KeyboardModifier.NoModifier):
    """通过视口发送鼠标事件，显式指定按钮状态，避免依赖系统鼠标所在窗口。"""
    viewport = canvas.view.viewport()
    event = QMouseEvent(kind, QPointF(position), QPointF(viewport.mapToGlobal(position)),
                        button, buttons, modifiers)
    QApplication.sendEvent(viewport, event)


def _outlet_point(canvas, key):
    """按节点实际边界寻找出线把手，途经节点与把手之间的空隙。"""
    view = canvas.view
    rect = canvas.node_items[key].sceneBoundingRect()
    if key in canvas.graph_group_items:
        rect = canvas.graph_group_items[key].sceneBoundingRect()
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


def _choose_graph_menu_action(canvas, position, label, qtbot, *, first=False):
    """以真实右键和菜单事件选择动作，返回动作是否可用。"""
    enabled = []
    labels = []

    def choose():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            labels.extend(entry.text() for entry in menu.actions())
            action = next((entry for entry in menu.actions() if entry.text() == label), None)
            if action is not None:
                enabled.append(action.isEnabled())
                if action.isEnabled():
                    menu.setActiveAction(action)
                    qtbot.keyClick(menu, Qt.Key.Key_Return)
                    return
            menu.close()

    QTimer.singleShot(0, choose)
    right = Qt.MouseButton.RightButton
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, position, right, right)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, position, right)
    assert enabled, (label, labels, canvas.selected_node_ids(), type(canvas.view.itemAt(position)).__name__)
    if first:
        assert labels[0] == label
    result, = enabled
    return result


def _start_graph_drag(canvas, start, end, *, modifiers=Qt.KeyboardModifier.NoModifier):
    left = Qt.MouseButton.LeftButton
    assert canvas.view.viewport().rect().contains(start) and canvas.view.viewport().rect().contains(end)
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left, modifiers=modifiers)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left, modifiers=modifiers)
    assert canvas.view.node_drag.active


def _release_graph_drag(canvas, end, *, modifiers=Qt.KeyboardModifier.NoModifier):
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, modifiers=modifiers)
    assert not canvas.view.node_drag.active


def test_graph_group_title_is_a_node_and_ungroup_keeps_details_and_links(window, qtbot):
    canvas = _create_graph(window, THREE_NODES + "\na -> c\nb -> c")
    canvas.fit_to_view()
    positions = canvas.layout_result.nodes.copy()
    canvas.select_nodes(["a", "b"])
    revision = canvas.engine.revision
    assert _choose_graph_menu_action(canvas, _node_point(canvas, "a"), "编组", qtbot)
    group, = canvas.graph.groups
    assert group == GraphGroup("g1", ("a", "b"))
    assert canvas.graph.edges == (GraphEdge(group.id, "c"),)
    assert canvas.engine.revision == revision + 1
    assert {key: canvas.layout_result.nodes[key] for key in positions} == positions
    assert not canvas.action("graph_create_group").isEnabled()
    assert canvas.editor is not None
    qtbot.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert canvas.editor is None and canvas.document_view.nodes[group.id].text == "分组 1"

    canvas.fit_to_view()
    canvas.select_nodes(["c"])
    point = _node_point(canvas, group.id)
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.selected_node_ids() == [group.id]
    qtbot.mouseDClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.mouseRelease(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.editor is not None
    canvas.editor.setPlainText("重命名后的界面模块")
    canvas.finish_edit(commit=True)
    assert canvas.document_view.nodes[group.id].text == "重命名后的界面模块"
    assert canvas.graph.groups == (group,)
    canvas.action("create_detail_page").trigger()
    detail = canvas.current_page_id
    assert canvas.current_page_kind == "detail"
    canvas.return_to_parent_page()
    canvas.action("create_submap").trigger()
    submap = canvas.current_page_id
    assert canvas.current_page_kind == "submap"
    canvas.return_to_parent_page()
    assert canvas.selected_node_ids() == [group.id]
    canvas.fit_to_view()
    canvas.set_editable(False)
    point = _node_point(canvas, group.id)
    assert not _choose_graph_menu_action(canvas, point, "解除分组", qtbot)
    start_graph = canvas.graph
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _pointer_event(canvas, QEvent.Type.MouseMove, point + QPoint(44, 20), buttons=Qt.MouseButton.LeftButton)
    _release_graph_drag(canvas, point + QPoint(44, 20))
    assert canvas.graph == start_graph and group.id in canvas.graph_group_items

    canvas.set_editable(True)
    assert _choose_graph_menu_action(canvas, _node_point(canvas, group.id), "解除分组", qtbot)
    assert canvas.graph.groups == () and not canvas.graph_group_items
    assert canvas.graph.nodes == start_graph.nodes
    assert canvas.graph.edges == (GraphEdge("a", "c"), GraphEdge("b", "c"))
    assert canvas.document_view.nodes[group.id].detail_page_id == detail
    assert canvas.document_view.nodes[group.id].submap_id == submap
    assert canvas.engine.undo() and canvas.graph == start_graph
    assert canvas.engine.redo() and canvas.graph.groups == ()


def test_graph_group_drag_moves_title_and_members_together_and_updates_split_view(window, qtbot):
    canvas = _create_graph(window, FOUR_NODES + '\n节点 e "输出"\n节点 g1 "左侧"\n节点 g2 "右侧"\n'
                           '分组 g1 a b\n分组 g2 c d\na -> b\nc -> d\ng1 -> e')
    first = window.documents.active
    positions = {"a": (0, 0), "b": (0, 100), "c": (350, 0), "d": (350, 100), "e": (500, 210)}
    graph = replace(canvas.graph, positions=positions)
    first.engine.set_node_graph(first.engine.root_id, graph)
    assert first.duplicate()
    second = window.documents.active
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    other = second.canvas
    qtbot.waitUntil(lambda: other.view.viewport().height() > 200)
    other.fit_to_view()
    original = other.layout_result
    other.select_nodes(["g1"])
    start = _node_point(other, "g1")
    end = other.view.mapFromScene(QPointF(*original.group_frames["g2"].body.center))
    _start_graph_drag(other, start, end)
    moved = other.layout_result
    dx, dy = moved.nodes["a"].x - original.nodes["a"].x, moved.nodes["a"].y - original.nodes["a"].y
    assert moved.group_frames["g1"].bounds == original.group_frames["g1"].bounds.translated(dx, dy)
    assert moved.nodes["b"] == original.nodes["b"].translated(dx, dy)
    assert moved.nodes["c"] == original.nodes["c"] and moved.branches["g1->e"] != original.branches["g1->e"]
    assert moved.branches["a->b"] == original.branches["a->b"].translated(dx, dy)
    assert other.graph == canvas.graph == graph
    qtbot.keyClick(other.view, Qt.Key.Key_Escape)
    _release_graph_drag(other, end)
    assert other.layout_result == canvas.layout_result == original

    _start_graph_drag(other, start, end)
    assert canvas.apply_graph_text(format_graph_text(graph).replace('"左侧"', '"左侧模块"'))
    _release_graph_drag(other, end)
    graph = canvas.graph
    assert canvas.document_view.nodes["g1"].text == "左侧模块" and graph.positions == positions
    assert other.graph == graph and other.layout_result == canvas.layout_result

    revision = first.engine.revision
    start = _node_point(other, "g1")
    end = other.view.mapFromScene(QPointF(*other.layout_result.group_frames["g2"].body.center))
    _start_graph_drag(other, start, end)
    _release_graph_drag(other, end)
    assert first.engine.revision == revision + 1 and other.graph == canvas.graph
    assert other.graph.parent_groups()["g1"] == "g2" and other.layout_result == canvas.layout_result
    assert GraphEdge("g2", "e") in other.graph.edges and GraphEdge("g1", "e") not in other.graph.edges
    committed = other.graph
    assert other.engine.undo() and other.graph == canvas.graph == graph
    assert other.engine.redo() and other.graph == committed

    other.action("graph_auto_layout").trigger()
    assert other.graph.groups == committed.groups and other.graph.positions == {}
    left, right = (other.layout_result.group_frames[group.id].bounds for group in graph.groups)
    assert right.union(left) == right and other.layout_result == canvas.layout_result


def test_local_group_layout_moves_only_direct_members_in_nested_split_view(window, qtbot):
    source = ('节点 o "外层"\n节点 m "中层"\n节点 i "内层"\n节点 x "甲"\n节点 y "乙"\n'
              '节点 a "同层项"\n节点 b "直接成员"\n节点 z "外部"\n'
              '分组 o m b\n分组 m i a\n分组 i x y\nx -> y\ni -> a\nm -> b\no -> z')
    canvas = _create_graph(window, source)
    engine = canvas.engine
    groups = tuple(replace(g, min_size=(265, 180)) if g.id == "i" else g for g in canvas.graph.groups)
    before = replace(canvas.graph, groups=groups, positions={
        "x": (210, 125), "y": (65, 74), "a": (450, 90), "b": (-280, 410), "z": (382, 330)})
    engine.set_node_graph(engine.root_id, before)
    assert window.documents.active.duplicate()
    other = window.documents.active.canvas
    window.host.editor.split_token(window.documents.active.id, Qt.Orientation.Horizontal)
    qtbot.waitUntil(lambda: other.view.viewport().height() > 200)
    other.fit_to_view()
    base = other.layout_result
    frame = base.group_frames["o"]
    header = other.view.mapFromScene(QPointF(frame.bounds.right - 14, frame.header.center[1]))
    other.select_nodes(["z"])
    qtbot.mouseClick(other.view.viewport(), Qt.MouseButton.LeftButton, pos=header)
    assert other.selected_node_ids() == ["o"] and other.graph_group_items["o"]._selected
    assert other.node_items["o"].group_title and other.view.text_resize._selected_item() is None
    view_state = other.capture_page_view_state()
    revision = engine.revision
    assert _choose_graph_menu_action(other, header, "组内自动布局", qtbot, first=True)
    result = other.layout_result
    assert engine.revision == revision + 1 and other.graph == canvas.graph
    assert result.nodes == canvas.layout_result.nodes and result.nodes["z"] == base.nodes["z"]
    assert result.group_frames["o"].bounds.x == pytest.approx(frame.bounds.x)
    assert result.group_frames["o"].bounds.y == pytest.approx(frame.bounds.y)
    box, old = result.group_frames["m"].bounds, base.group_frames["m"].bounds
    dx, dy = box.x - old.x, box.y - old.y
    assert dx or dy
    for key in ("m", "i", "a", "x", "y"):
        assert result.nodes[key] == base.nodes[key].translated(dx, dy)
    assert result.branches["x->y"] == base.branches["x->y"].translated(dx, dy)
    assert other.capture_page_view_state()["center"] == pytest.approx(view_state["center"], abs=1)
    assert other.zoom_factor == view_state["zoom"]
    after = other.graph
    other.auto_layout_graph_group("o")
    assert engine.revision == revision + 1 and other.graph == after
    assert engine.undo() and canvas.graph == other.graph == before
    assert engine.redo() and canvas.graph == other.graph == after
    other.auto_layout_graph()
    assert not other.graph.positions
    for key, parent in other.graph.parent_groups().items():
        content = other.layout_result.group_frames[parent].body
        child = other.layout_result.group_frames.get(key)
        box = child.bounds if child else other.layout_result.nodes[key]
        assert content.union(box) == content
    assert not other.layout_result.group_frames["o"].bounds.intersects(other.layout_result.nodes["z"])


def test_top_level_menu_arranges_roots_as_rigid_blocks_in_both_views(window, qtbot):
    source = ('节点 o "外层"\n节点 m "中层"\n节点 i "登录方式"\n节点 a "密码"\n节点 b "短信"\n'
              '节点 c "扫码"\n节点 x "帮助"\n节点 y "账号"\n节点 z "入口"\n节点 w "完成"\n'
              '分组 o m y\n分组 m i x\n分组 i a b c\na -> b\nb -> c\ni -> x\nm -> y\nz -> o\no -> w')
    canvas = _create_graph(window, source)
    engine = canvas.engine
    before = replace(canvas.graph, positions={
        "a": (170, 250), "b": (50, 160), "c": (100, 175), "x": (270, 165), "y": (45, 105),
        "z": (350, -110), "w": (-75, 65)})
    engine.set_node_graph(engine.root_id, before)
    assert window.documents.active.duplicate()
    other = window.documents.active.canvas
    window.host.editor.split_token(window.documents.active.id, Qt.Orientation.Horizontal)
    qtbot.waitUntil(other.isVisible)
    other.fit_to_view()
    base = other.layout_result
    viewport = other.capture_page_view_state()
    revision = engine.revision
    assert _choose_graph_menu_action(other, _node_point(other, "z"), "顶层自动布局", qtbot)
    result = other.layout_result
    assert result == canvas.layout_result and engine.revision == revision + 1
    old = base.group_frames["o"].bounds
    box = result.group_frames["o"].bounds
    dx, dy = box.x - old.x, box.y - old.y
    assert dx or dy
    for key in ("o", "m", "i"):
        assert result.group_frames[key].bounds == base.group_frames[key].bounds.translated(dx, dy)
    for key in before.subtree_ids(["o"]):
        assert result.nodes[key] == base.nodes[key].translated(dx, dy)
    for key in ("a->b", "b->c", "i->x", "m->y"):
        actual, expected = result.branches[key], base.branches[key].translated(dx, dy)
        assert replace(actual, arrow=None) == replace(expected, arrow=None)
        for point, translated in zip(actual.arrow, expected.arrow, strict=True):
            assert point == pytest.approx(translated, abs=1e-9)
    z, w = result.nodes["z"], result.nodes["w"]
    assert z.bottom < box.y and box.bottom < w.y
    assert (min(z.x, box.x, w.x), min(z.y, box.y, w.y)) == (
        min(base.nodes["z"].x, old.x, base.nodes["w"].x), min(base.nodes["z"].y, old.y, base.nodes["w"].y))
    assert other.capture_page_view_state()["center"] == pytest.approx(viewport["center"], abs=1)
    assert other.zoom_factor == viewport["zoom"]
    assert _choose_graph_menu_action(other, QPoint(3, 3), "顶层自动布局", qtbot)
    assert engine.revision == revision + 1 and other.layout_result == result
    assert engine.undo() and other.layout_result == canvas.layout_result == base
    assert engine.redo() and other.layout_result == canvas.layout_result == result


@pytest.mark.parametrize("source", ["", '节点 a "独立"', '节点 g "唯一组"\n节点 a "唯一成员"\n分组 g a'])
def test_top_level_layout_requires_two_roots(window, qtbot, source):
    canvas = _create_graph(window, source)
    revision = canvas.engine.revision
    assert not _choose_graph_menu_action(canvas, QPoint(4, 4), "顶层自动布局", qtbot)
    canvas.auto_layout_graph_top_level()
    assert canvas.engine.revision == revision


def test_shift_drag_stacks_node_and_group_both_ways_and_updates_at_rest(window, qtbot):
    source = ('节点 g "登录方式"\n节点 a "密码"\n节点 b "短信"\n节点 n "这是一张较宽的外部卡片"\n'
              '节点 z "结束"\n分组 g a b\na -> b\nn -> z')
    canvas = _create_graph(window, source)
    view = canvas.view
    engine = canvas.engine
    before = replace(canvas.graph, groups=(GraphGroup("g", ("a", "b"), (335, 175)),),
                     positions={"a": (25, 75), "b": (165, 100), "n": (465, 115), "z": (500, 240)})
    engine.set_node_graph(engine.root_id, before)
    canvas.fit_to_view()
    left = Qt.MouseButton.LeftButton
    shift = Qt.KeyboardModifier.ShiftModifier
    group = canvas.layout_result.group_frames["g"].bounds
    node = canvas.layout_result.nodes["n"]
    start = _node_point(canvas, "g")
    delta = QPointF(node.center[0] - group.right, node.center[1] - group.bottom + 25)
    end = view.mapFromScene(view.mapToScene(start) + delta)
    overlap = view.mapFromScene(QPointF(node.center[0] - 16, node.center[1]))
    assert view.itemAt(overlap) == canvas.node_items["n"]
    revision = engine.revision
    qtbot.mouseClick(view.viewport(), left, pos=start)
    assert canvas.graph == before and engine.revision == revision
    _start_graph_drag(canvas, start, end)
    assert view.itemAt(overlap) == canvas.graph_group_items["g"] and canvas.graph == before
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    _release_graph_drag(canvas, end)
    assert view.itemAt(overlap) == canvas.node_items["n"] and engine.revision == revision
    _start_graph_drag(canvas, start, end)
    _release_graph_drag(canvas, end)
    grouped = canvas.graph
    assert engine.revision == revision + 1 and grouped.groups == before.groups
    assert view.itemAt(overlap) == canvas.graph_group_items["g"]
    assert view.graph_group_at(overlap, include_body=True) == "g"
    qtbot.mouseClick(view.viewport(), left, pos=overlap)
    assert not canvas.selected_node_ids() and canvas.graph == grouped

    opaque = _viewport_region(view, overlap, 4)
    canvas.node_items["n"].hide()
    assert _viewport_region(view, overlap, 4) == opaque
    canvas.node_items["n"].show()
    # 从仍然露出的半张卡片开始拖动，松手位置落在成员正文上。
    exposed = view.mapFromScene(QPointF(node.right - 10, node.center[1]))
    target = _node_point(canvas, "a")
    assert view.itemAt(exposed) == canvas.node_items["n"]
    end = target + exposed - _node_point(canvas, "n")
    _start_graph_drag(canvas, exposed, end, modifiers=shift)
    assert view.node_drag._group_destination.hint == "保持原层叠放"
    qtbot.keyRelease(view, Qt.Key.Key_Shift)
    assert view.node_drag._group_destination.key == "g" and canvas.graph_group_items["g"]._highlighted
    qtbot.keyPress(view, Qt.Key.Key_Shift)
    assert view.node_drag._group_destination.hint == "保持原层叠放" and not canvas.graph_group_items["g"]._highlighted
    _release_graph_drag(canvas, end, modifiers=shift)
    qtbot.keyRelease(view, Qt.Key.Key_Shift)
    assert engine.revision == revision + 2 and canvas.graph.groups == before.groups
    assert view.itemAt(target) == canvas.node_items["n"] and view.graph_group_at(target, include_body=True) is None
    qtbot.mouseClick(view.viewport(), left, pos=target)
    assert canvas.selected_node_ids() == ["n"]
    opaque = _viewport_region(view, target, 6)
    canvas.node_items["a"].hide()
    assert _viewport_region(view, target, 6) == opaque
    canvas.node_items["a"].show()
    raised = canvas.graph
    assert engine.undo() and canvas.graph == grouped and view.itemAt(overlap) == canvas.graph_group_items["g"]
    assert engine.redo() and canvas.graph == raised and view.itemAt(target) == canvas.node_items["n"]


def test_multiple_shift_drag_keeps_each_parent_and_sibling_order_in_split_view(window, qtbot):
    source = ('节点 a "甲"\n节点 b "乙"\n节点 c "丙"\n节点 n "外部"\n节点 z "另一项"\n'
              '节点 g "容器"\n分组 g a b c\n叠放 b a c n g z')
    canvas = _create_graph(window, source)
    engine = canvas.engine
    before = replace(canvas.graph, positions={"a": (130, 55), "b": (55, 55), "c": (220, 60),
                                              "n": (220, 260), "z": (50, 260)})
    engine.set_node_graph(engine.root_id, before)
    assert window.documents.active.duplicate()
    other = window.documents.active.canvas
    window.host.editor.split_token(window.documents.active.id, Qt.Orientation.Horizontal)
    qtbot.waitUntil(other.isVisible)
    other.fit_to_view()
    other.select_nodes(["a", "b", "n"])
    start = _node_point(other, "n")
    end = _node_point(other, "b")
    shift = Qt.KeyboardModifier.ShiftModifier
    revision = engine.revision
    base = other.layout_result
    _start_graph_drag(other, start, end, modifiers=shift)
    assert canvas.node_items["n"].zValue() < canvas.node_items["g"].zValue()
    assert other.node_items["n"].zValue() > other.node_items["g"].zValue()
    _release_graph_drag(other, end, modifiers=shift)
    committed = other.graph
    assert engine.revision == revision + 1 and committed == canvas.graph
    assert committed.groups == before.groups and committed.nodes == before.nodes
    assert committed.stacking_children() == {None: ["g", "z", "n"], "g": ["c", "b", "a"]}
    dx = other.layout_result.nodes["n"].x - base.nodes["n"].x
    dy = other.layout_result.nodes["n"].y - base.nodes["n"].y
    for key in ("n", "a", "b"):
        assert other.layout_result.nodes[key] == base.nodes[key].translated(dx, dy)
    assert other.layout_result == canvas.layout_result
    assert canvas.node_items["n"].zValue() > canvas.node_items["g"].zValue()
    assert engine.undo() and canvas.graph == other.graph == before
    assert engine.redo() and canvas.graph == other.graph == committed


def test_opaque_group_body_hides_edges_handles_and_nodes_from_picking(window, qtbot):
    source = ('节点 a "成员"\n节点 n "遮住的节点"\n节点 z "终点"\n节点 p "拖入项"\n节点 g "组"\n'
              '分组 g a\nn -> z')
    canvas = _create_graph(window, source)
    view = canvas.view
    edge = GraphEdge("n", "z")
    graph = replace(canvas.graph, groups=(GraphGroup("g", ("a",), (310, 220)),),
                    positions={"a": (5, 5), "n": (112, 24), "z": (440, 24), "p": (465, 160)})
    canvas.engine.set_node_graph(canvas.engine.root_id, graph)
    canvas.fit_to_view()
    hidden = _node_point(canvas, "n")
    line = view.mapFromScene(canvas.branch_items[edge.key].path().pointAtPercent(0.2))
    assert view.itemAt(hidden) == view.itemAt(line) == canvas.graph_group_items["g"]
    assert canvas.graph_edge_at(line) is None
    assert canvas._graph_objects_in_rect(QRect(line - QPoint(2, 2), line + QPoint(2, 2))) == (set(), set())
    controls = view.graph_edge_edit
    canvas.select_nodes([])
    background = _viewport_region(view, line, 4)
    controls.select_edge(edge)
    assert not any(handle.moving_source for handle in controls._handles())
    assert _viewport_region(view, line, 4) == background
    left = Qt.MouseButton.LeftButton
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, hidden - QPoint(20, 10), left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, hidden + QPoint(20, 10), buttons=left)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, hidden + QPoint(20, 10), left)
    assert not canvas.selected_node_ids() and not controls.selected_edges
    qtbot.mouseDClick(view.viewport(), left, pos=hidden)
    qtbot.mouseRelease(view.viewport(), left, pos=hidden)
    added = canvas.editor.node_id
    assert added != "n" and canvas.graph.parent_groups()[added] == "g"
    canvas.finish_edit(commit=False)
    assert canvas.engine.undo() and canvas.graph == graph
    canvas.apply_graph_text(format_graph_text(graph.raised_nodes(["n"])))
    assert view.itemAt(hidden) == canvas.node_items["n"]
    _start_graph_drag(canvas, _node_point(canvas, "p"), hidden)
    assert view.node_drag._group_destination.key is None
    _release_graph_drag(canvas, hidden)
    assert "p" not in canvas.graph.parent_groups()


def test_stack_order_roundtrips_file_text_and_both_clipboards(window, tmp_path):
    source = '节点 a "甲"\n节点 b "乙"\n节点 g "组"\n节点 n "外"\n分组 g a b'
    canvas = _create_graph(window, source)
    legacy = canvas.graph.to_record()
    assert legacy["version"] == 7 and "stack_order" not in legacy["data"]
    assert NodeGraph.from_data(legacy["data"], version=7).stacking_children() == {None: ["g", "n"], "g": ["a", "b"]}
    canvas.apply_graph_text(source + "\n叠放 b a n g")
    graph = canvas.graph
    assert graph.stacking_children() == {None: ["n", "g"], "g": ["b", "a"]}
    assert [node.id for node in graph.nodes] == ["a", "b", "g", "n"]
    assert parse_graph_text(format_graph_text(graph)).stack_order == graph.stack_order
    archive = tmp_path / "叠放.mindmap"
    save_document(canvas.engine.document, archive)
    assert MindMapEngine(load_document(archive)).node_graph(canvas.engine.root_id) == graph
    folder = tmp_path / "目录图"
    save_directory(canvas.engine.document, folder)
    assert MindMapEngine(load_directory(folder)).node_graph(canvas.engine.root_id) == graph
    canvas.select_nodes(["n", "g"])
    canvas.copy()
    clipboard = QApplication.clipboard()
    text = clipboard.text()
    for plain in (False, True):
        if plain:
            mime = QMimeData()
            mime.setText(text)
            clipboard.setMimeData(mime)
        a, b, g, n = canvas.paste()
        scopes = canvas.graph.stacking_children()
        assert scopes[None][-2:] == [n, g] and scopes[g] == [b, a] and scopes["g"] == ["b", "a"]
    # 单独取出前方组的一个成员，粘贴后仍位于原来在它后方的普通节点之上。
    canvas.select_nodes(["a", "n"])
    canvas.copy()
    partial = parse_graph_text(clipboard.text())
    assert not partial.groups and partial.stacking_children()[None] == ["n", "a"]


def test_transparent_graph_image_in_front_covers_group_title(window):
    canvas = _create_graph(window, '节点 a "成员"\n节点 g "登录方式"\n分组 g a')
    engine = canvas.engine
    image_id = engine.add_graph_image_node(engine.root_id, "g", b'<svg xmlns="http://www.w3.org/2000/svg" '
                                          b'width="160" height="120"/>', width=160)
    title = canvas.layout_result.nodes["g"]
    group = canvas.layout_result.group_frames["g"].bounds
    graph = replace(canvas.graph, positions={key: (box.x, box.y) for key, box in canvas.layout_result.nodes.items()})
    engine.set_node_graph(engine.root_id, replace(graph, positions={**graph.positions, image_id: (group.x, group.y)}))
    canvas.fit_to_view()
    canvas.select_nodes([])
    view = canvas.view
    point = view.mapFromScene(QPointF(*title.center))
    assert view.itemAt(point) == canvas.node_items[image_id]
    pixels = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
    view.viewport().render(pixels)
    assert {pixels.pixelColor(point + QPoint(dx, dy)).name() for dx in range(-6, 7) for dy in range(-4, 5)} == {
        canvas.scene.backgroundBrush().color().name()}


def test_group_header_body_shortcuts_and_member_creation(window, qtbot):
    canvas = _create_graph(window, THREE_NODES)
    canvas.engine.set_node_graph(canvas.engine.root_id, replace(
        canvas.graph, positions={"a": (-140, 0), "b": (0, 0), "c": (360, 0)}))
    canvas.fit_to_view()
    view = canvas.view
    boxes = canvas.layout_result.nodes
    region = boxes["a"].union(boxes["b"])
    start = view.mapFromScene(QPointF(region.x - 7, region.y - 7))
    end = view.mapFromScene(QPointF(region.right + 7, region.bottom + 7))
    left = Qt.MouseButton.LeftButton
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    assert canvas.selected_node_ids() == ["a", "b"]
    qtbot.keyClick(view, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
    group, = canvas.graph.groups
    assert canvas.editor.node_id == group.id
    canvas.editor.setPlainText("登录方式")
    qtbot.keyClick(view, Qt.Key.Key_Return)
    assert canvas.document_view.nodes[group.id].text == "登录方式" and canvas.editor is None
    canvas._set_graph_group_size(group.id, (380, 290))
    canvas.fit_to_view()
    name = _node_point(canvas, group.id)
    qtbot.mouseDClick(view.viewport(), left, pos=name)
    qtbot.mouseRelease(view.viewport(), left, pos=name)
    assert canvas.editor.node_id == group.id and canvas.editor.textCursor().selectedText() == "登录方式"
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    f = canvas.layout_result.group_frames[group.id]
    base = canvas.layout_result.nodes.copy()
    region = base["a"].union(base["b"])
    start = view.mapFromScene(QPointF(region.x - 5, region.y - 5))
    end = view.mapFromScene(QPointF(region.right + 5, region.bottom + 5))
    assert view.graph_group_at(start, include_body=True) == group.id
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    assert canvas.selected_node_ids() == ["a", "b"]
    title = view.mapFromScene(QPointF(f.bounds.right - 14, f.header.center[1]))
    end = title + QPoint(-28, 17)
    _start_graph_drag(canvas, title, end)
    _release_graph_drag(canvas, end)
    dx = canvas.layout_result.nodes["a"].x - base["a"].x
    dy = canvas.layout_result.nodes["a"].y - base["a"].y
    assert dx and dy and canvas.layout_result.nodes["b"] == base["b"].translated(dx, dy)
    assert canvas.layout_result.nodes["c"] == base["c"]
    f = canvas.layout_result.group_frames[group.id]
    point = view.mapFromScene(QPointF(f.bounds.right - 35, f.bounds.bottom - 35))
    assert view.graph_group_at(point) is None and view.graph_group_at(point, include_body=True) == group.id
    expected = view.mapToScene(point)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    member = canvas.editor.node_id
    canvas.finish_edit(commit=True)
    assert canvas.graph.parent_groups()[member] == group.id and not canvas.graph.edges
    assert canvas.graph.positions[member] == pytest.approx((expected.x(), expected.y()), abs=2)
    f = canvas.layout_result.group_frames[group.id]
    point = view.mapFromScene(QPointF(f.bounds.x + 8, f.body.center[1]))
    assert _choose_graph_menu_action(canvas, point, "新建组内节点", qtbot)
    assert canvas.graph.parent_groups()[canvas.editor.node_id] == group.id
    canvas.finish_edit(commit=True)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    canvas.fit_to_view()
    frame = canvas.layout_result.group_frames[group.id]
    rim = view.mapFromScene(QPointF(frame.bounds.x, frame.body.center[1]))
    qtbot.mouseClick(view.viewport(), left, pos=rim)
    assert canvas.selected_node_ids() == [group.id] and canvas.action("graph_ungroup").isEnabled()
    qtbot.keyClick(view, Qt.Key.Key_F2)
    assert canvas.editor is not None and canvas.editor.node_id == group.id
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    qtbot.keyClick(view, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert not canvas.graph.groups and group.id not in canvas.document_view.nodes
    assert member in canvas.document_view.nodes


def test_right_click_on_one_of_two_selected_groups_can_group_both(window, qtbot):
    canvas = _create_graph(window, '节点 x "一组"\n节点 y "二组"\n分组 x\n分组 y')
    canvas.select_nodes(["x", "y"])
    canvas.fit_to_view()
    assert _choose_graph_menu_action(canvas, _node_point(canvas, "x"), "编组", qtbot)
    group = canvas.editor.node_id
    assert canvas.graph.parent_groups() == {"x": group, "y": group}


def test_empty_and_single_group_layout_preserves_a_distant_viewport(window, qtbot):
    canvas = _create_graph(window, '节点 o "空组"\n节点 g "单成员组"\n节点 a "成员"\n分组 o\n分组 g a')
    initial = replace(canvas.graph, positions={"o": (-200, 50), "g": (0, 0), "a": (3500, 1600)})
    canvas.engine.set_node_graph(canvas.engine.root_id, initial)
    canvas.fit_to_view()
    revision = canvas.engine.revision
    assert not _choose_graph_menu_action(canvas, _node_point(canvas, "o"), "组内自动布局", qtbot, first=True)
    canvas.auto_layout_graph_group("o")
    assert canvas.engine.revision == revision
    canvas.reset_zoom()
    canvas.view.centerOn(QPointF(*canvas.layout_result.nodes["a"].center))
    state = canvas.capture_page_view_state()
    bounds = canvas.layout_result.group_frames["g"].bounds
    canvas.auto_layout_graph_group("g")
    rect = canvas.layout_result.group_frames["g"].bounds
    assert (rect.x, rect.y) == (bounds.x, bounds.y) and rect.width < bounds.width and rect.height < bounds.height
    assert canvas.graph.positions["a"] != initial.positions["a"]
    assert canvas.capture_page_view_state()["center"] == pytest.approx(state["center"], abs=1)
    assert canvas.zoom_factor == state["zoom"]
    canvas.auto_layout_graph_group("g")
    assert canvas.engine.revision == revision + 1
    assert canvas.engine.undo() and canvas.graph == initial


def test_group_layout_can_expand_across_an_external_node(window):
    source = (FOUR_NODES + '\n节点 g "内组"\n节点 o "外组"\n分组 g a b c\n分组 o g\na -> b\nb -> c')
    canvas = _create_graph(window, source)
    positions = {"g": (30, 75), "o": (0, 0), **dict.fromkeys("abc", (60, 140))}
    canvas.engine.set_node_graph(canvas.engine.root_id, replace(canvas.graph, positions=positions))
    before = canvas.layout_result.group_frames
    inner = before["g"].bounds
    positions["d"] = (inner.x + 12, inner.bottom + 1)
    canvas.engine.set_node_graph(canvas.engine.root_id, replace(canvas.graph, positions=positions))
    outside = canvas.layout_result.nodes["d"]
    assert not inner.intersects(outside)
    canvas.auto_layout_graph_group("g")
    after = canvas.layout_result.group_frames
    assert after["g"].bounds.intersects(outside) and canvas.layout_result.nodes["d"] == outside
    assert (after["g"].bounds.x, after["g"].bounds.y) == (inner.x, inner.y)
    assert after["o"].bounds.height > before["o"].bounds.height


def test_nested_group_drag_previews_innermost_parent_top_level_and_cycle(window, qtbot):
    canvas = _create_graph(window, '节点 o "外层"\n节点 g "登录方式"\n节点 a "密码"\n节点 z "短信"\n分组 o g\n分组 g a')
    groups = (GraphGroup("o", ("g",), (480, 350)), GraphGroup("g", ("a",), (200, 145)))
    initial = replace(canvas.graph, groups=groups, positions={"o": (0, 0), "g": (65, 110),
                                                               "a": (95, 192), "z": (625, 215)})
    canvas.engine.set_node_graph(canvas.engine.root_id, initial)
    canvas.fit_to_view()
    view = canvas.view
    inner = canvas.layout_result.group_frames["g"].bounds
    target = view.mapFromScene(QPointF(inner.right - 12, inner.bottom - 12))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_node_point(canvas, "o"))
    _start_graph_drag(canvas, _node_point(canvas, "o"), target)
    assert view.node_drag.graph_drop_blocked and "后代" in view.node_drag._group_destination.hint
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    _release_graph_drag(canvas, target)
    assert canvas.graph == initial
    _start_graph_drag(canvas, _node_point(canvas, "z"), target)
    assert view.node_drag._group_destination.hint == "松开加入『登录方式』"
    assert [key for key, item in canvas.graph_group_items.items() if item._highlighted] == ["g"]
    _release_graph_drag(canvas, target)
    assert canvas.graph.parent_groups()["z"] == "g"
    outer = canvas.layout_result.group_frames["o"]
    target = view.mapFromScene(QPointF(outer.bounds.right - 22, outer.body.y + 18))
    _start_graph_drag(canvas, _node_point(canvas, "z"), target)
    assert view.node_drag._group_destination.hint == "松开移到父组"
    _release_graph_drag(canvas, target)
    assert canvas.graph.parent_groups()["z"] == "o"
    canvas.fit_to_view()
    target = view.viewport().rect().bottomRight() - QPoint(6, 6)
    _start_graph_drag(canvas, _node_point(canvas, "z"), target)
    assert view.node_drag._group_destination.hint == "松开移到顶层"
    _release_graph_drag(canvas, target)
    assert "z" not in canvas.graph.parent_groups()


def test_graph_node_drag_uses_frozen_topmost_boxes_and_keeps_empty_group(window):
    source = FOUR_NODES + '\n节点 e "甲"\n节点 f "乙"\n分组 g1 "左组" a b\n分组 g2 "右组" c d'
    canvas = _create_graph(window, source)
    positions = {"a": (0, 0), "b": (25, 130), "c": (90, 40), "d": (255, 160), "e": (0, 250), "f": (170, 250)}
    graph = replace(canvas.graph, positions=positions)
    canvas.engine.set_node_graph(canvas.engine.root_id, graph)
    canvas.reset_zoom()
    canvas.center_root()
    frames = canvas.layout_result.group_frames
    assert frames["g1"].bounds.intersects(frames["g2"].bounds)
    assert canvas.graph_group_items["g1"].zValue() < canvas.graph_group_items["g2"].zValue()
    revision = canvas.engine.revision
    canvas.select_nodes(["e", "f"])
    end = canvas.view.mapFromScene(QPointF(42, 100))
    _start_graph_drag(canvas, _node_point(canvas, "e"), end)
    assert canvas.layout_result.group_frames == frames and canvas.graph == graph
    assert [key for key, item in canvas.graph_group_items.items() if item._highlighted] == ["g1"]
    _release_graph_drag(canvas, end)
    assert canvas.graph.groups == (GraphGroup("g1", ("a", "b", "e", "f")), GraphGroup("g2", ("c", "d")))
    assert canvas.engine.revision == revision + 1
    assert not any(item._highlighted for item in canvas.graph_group_items.values())

    canvas.select_nodes(["e", "f"])
    outside = canvas.view.mapFromScene(QPointF(350, 245))
    _start_graph_drag(canvas, _node_point(canvas, "e"), outside)
    _release_graph_drag(canvas, outside)
    assert canvas.graph.groups[0] == graph.groups[0]
    canvas.select_nodes(["a"])
    canvas.delete_selected()
    assert canvas.graph.groups[0].member_ids == ("b",)
    canvas.select_nodes(["b"])
    _start_graph_drag(canvas, _node_point(canvas, "b"), outside)
    _release_graph_drag(canvas, outside)
    assert canvas.graph.groups[0] == GraphGroup("g1") and "g1" in canvas.graph_group_items
    assert canvas.engine.undo() and canvas.graph.groups[0].member_ids == ("b",)
    canvas.select_nodes(["b"])
    canvas.delete_selected()
    assert canvas.graph.groups[0] == GraphGroup("g1")


@pytest.mark.parametrize("interruption", ["失焦", "切页"])
def test_graph_group_drag_cancels_on_focus_loss_or_navigation(window, interruption):
    canvas = _create_graph(window, THREE_NODES + '\nc -> a\na -> b\n分组 g1 "模块" a b')
    canvas.fit_to_view()
    graph, layout = canvas.graph, canvas.layout_result
    canvas.select_nodes(["g1"])
    start = _node_point(canvas, "g1")
    assert canvas.graph_edge_at(start) is None
    end = start + QPoint(52, 26)
    _start_graph_drag(canvas, start, end)
    if interruption == "失焦":
        QApplication.sendEvent(canvas.view, QFocusEvent(QEvent.Type.FocusOut))
        assert canvas.layout_result == layout
    else:
        canvas.return_to_parent_page()
        assert canvas.graph is None and not canvas.graph_group_items
    _release_graph_drag(canvas, end)
    assert canvas.engine.node_graph(canvas.engine.root_id) == graph


def test_graph_group_clipboard_rebuilds_complete_groups_for_native_and_text_paste(window, tmp_path):
    canvas = _create_graph(window, SAMPLE + '\n分组 g1 "来源组" a b')
    clipboard = QApplication.clipboard()
    canvas.select_nodes(["a"])
    canvas.copy()
    assert parse_graph_text(clipboard.text()).groups == ()
    canvas.select_nodes(["g1"])
    canvas.copy()
    source = clipboard.text()
    assert parse_graph_text(source).groups == canvas.graph.groups

    assert window.new_document(tmp_path / "粘贴目标")
    target = _create_graph(window, '节点 a "已有"\n分组 g1 "目标组" a')
    ids = []
    for plain in (False, True):
        if plain:
            mime = QMimeData()
            mime.setText(source)
            clipboard.setMimeData(mime)
        added = target.paste()
        group_id = added[-1]
        ids.append(group_id)
        assert target.graph.groups[-1] == GraphGroup(group_id, tuple(added[:2]))
        assert target.document_view.nodes[group_id].text == "来源组"
        assert GraphEdge(*added[:2]) in target.graph.edges
    text = format_graph_text(target.graph).replace("分组 g1 a\n", "")
    assert target.apply_graph_text(text)
    assert [group.id for group in target.graph.groups] == ids and "g1" in target.document_view.nodes


def test_group_origins_survive_reopen_clipboards_and_explicit_edge_edits(window, tmp_path, qtbot):
    canvas = _create_graph(window, FOUR_NODES + "\na -> c\nb -> c")
    initial_edges = canvas.graph.edges
    canvas.select_nodes(["a", "b"])
    g = canvas.create_graph_group()
    canvas.finish_edit(commit=False)
    visible = GraphEdge(g, "c")
    grouped = canvas.graph
    assert grouped.edges == (visible,) and grouped.edge_origins == {visible: initial_edges}
    assert set(canvas.branch_items) == {visible.key}
    assert not any(isinstance(child, QGraphicsSimpleTextItem) and child.text() == "2 条"
                   for child in canvas.branch_items[visible.key].childItems())
    folder = tmp_path / "还原组内连线"
    save_directory(canvas.engine.document, folder)
    archive = tmp_path / "原端点.mindmap"
    save_document(canvas.engine.document, archive)
    assert MindMapEngine(load_document(archive)).node_graph(canvas.engine.root_id) == grouped
    assert window.open_path(folder)
    canvas = window.application.canvas
    canvas.open_node_graph(canvas.engine.root_id)
    assert canvas.graph == grouped
    assert canvas.graph.with_group_parents({"a": None}).edges == (initial_edges[0], visible)
    canvas.select_nodes([g])
    canvas.ungroup_graph_node()
    assert canvas.graph.edges == initial_edges
    assert canvas.engine.undo() and canvas.graph == grouped
    canvas.select_nodes(["a", "c"])
    canvas.copy()
    partial = parse_graph_text(QApplication.clipboard().text())
    assert not partial.groups and partial.edges == (GraphEdge("a", "c"),)
    canvas.select_nodes([g, "c"])
    canvas.copy()
    text = QApplication.clipboard().text()
    assert parse_graph_text(text).edge_origins == {visible: initial_edges}
    for plain in (False, True):
        if plain:
            mime = QMimeData()
            mime.setText(text)
            QApplication.clipboard().setMimeData(mime)
        a, b, c, group = canvas.paste()
        assert canvas.graph.edge_origins[GraphEdge(group, c)] == (GraphEdge(a, c), GraphEdge(b, c))
        canvas.select_nodes([group])
        canvas.ungroup_graph_node()
        assert GraphEdge(a, c) in canvas.graph.edges and GraphEdge(b, c) in canvas.graph.edges
        assert group not in canvas.document_view.nodes
        assert canvas.engine.undo() and canvas.engine.undo() and canvas.graph == grouped
    canvas.fit_to_view()
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_edge_point(canvas, visible))
    assert canvas.view.graph_edge_edit.selected_edge == visible
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
                     pos=_node_point(canvas, "d"))
    assert canvas.selected_node_ids() == ["d"] and canvas.view.graph_edge_edit.selected_edges == {visible}
    qtbot.keyClick(canvas.view, Qt.Key.Key_Delete)
    assert "d" not in canvas.document_view.nodes and not canvas.graph.edge_origins
    canvas.select_nodes([g])
    canvas.ungroup_graph_node()
    assert not canvas.graph.edges and not canvas.graph.edge_origins and g not in canvas.document_view.nodes
    assert canvas.engine.undo() and canvas.engine.undo() and canvas.graph == grouped
    replacement = GraphEdge(g, "d")
    canvas.fit_to_view()
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=_edge_point(canvas, visible))
    _drag_edge(canvas, _endpoint_point(canvas, visible), _node_point(canvas, "d"))
    canvas.select_nodes([g])
    canvas.ungroup_graph_node()
    assert canvas.graph.edges == (replacement,) and g in canvas.document_view.nodes and not canvas.graph.edge_origins
    legacy = replace(grouped, edge_origins={}).to_record()
    assert legacy["version"] == 7
    canvas.engine.set_node_graph(canvas.engine.root_id, NodeGraph.from_data(legacy["data"], version=7))
    canvas.select_nodes([g])
    canvas.ungroup_graph_node()
    assert canvas.graph.edges == (visible,) and g in canvas.document_view.nodes


def test_graph_group_edges_accept_peer_nodes_and_reject_cross_scope_drag_and_reconnect(window):
    canvas = _create_graph(window, FOUR_NODES + '\n节点 g1 "模块"\n分组 g1 a b')
    canvas.fit_to_view()
    _drag_edge(canvas, _outlet_point(canvas, "a"), _node_point(canvas, "c"))
    assert canvas.graph.edges == ()
    _drag_edge(canvas, _outlet_point(canvas, "a"), _node_point(canvas, "b"))
    canvas.fit_to_view()
    _drag_edge(canvas, _outlet_point(canvas, "c"), _node_point(canvas, "g1"))
    edge = GraphEdge("c", "g1")
    assert canvas.graph.edges == (GraphEdge("a", "b"), edge)
    branch = canvas.layout_result.branches[edge.key]
    group = canvas.layout_result.group_frames["g1"].bounds
    assert branch.end[1] == pytest.approx(group.y)
    _drag_edge(canvas, _endpoint_point(canvas, edge), _node_point(canvas, "b"))
    assert canvas.graph.edges == (GraphEdge("a", "b"), edge)

    # 已与组相连的外部节点放入该组后，原边将成为祖先到后代，整次操作应回滚。
    before, layout, revision = canvas.graph, canvas.layout_result, canvas.engine.revision
    errors = []
    canvas.error_occurred.connect(errors.append)
    canvas.select_nodes(["c"])
    end = canvas.view.mapFromScene(QPointF(*layout.group_frames["g1"].body.center))
    _start_graph_drag(canvas, _node_point(canvas, "c"), end)
    assert canvas.view.node_drag.graph_drop_blocked and "无法转接" in canvas.view.node_drag._group_destination.hint
    _release_graph_drag(canvas, end)
    assert errors and "无法转接" in errors[-1]
    assert canvas.graph == before and canvas.layout_result == layout and canvas.engine.revision == revision

    canvas.select_nodes(["a"])
    added = canvas.add_graph_node(connect_from_selection=True)
    canvas.finish_edit(commit=True)
    assert canvas.graph.parent_groups()[added] == "g1" and GraphEdge("a", added) in canvas.graph.edges
    canvas.fit_to_view()
    _drag_edge(canvas, _outlet_point(canvas, "g1"), _node_point(canvas, "d"))
    assert GraphEdge("g1", "d") in canvas.graph.edges


def test_graph_edge_dialog_lists_targets_in_the_selected_source_scope(window):
    canvas = _create_graph(window, THREE_NODES + '\n节点 g1 "模块"\n分组 g1 a b')
    canvas.select_nodes(["a"])
    choices = []

    def inspect_dialog():
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            return
        source, destination = dialog.findChildren(QComboBox)
        choices.append([destination.itemData(i) for i in range(destination.count())])
        source.setCurrentIndex(source.findData("g1"))
        choices.append([destination.itemData(i) for i in range(destination.count())])
        dialog.reject()

    QTimer.singleShot(0, inspect_dialog)
    canvas.choose_graph_edge()
    assert choices == [["a", "b"], ["c", "g1"]]


def test_graph_group_resize_preview_persists_minimum_and_adapts_after_drops(window, qtbot, tmp_path):
    canvas = _create_graph(window, '节点 a "成员"\n节点 b "较宽的外部节点"\n节点 g1 "模块"\n分组 g1 a')
    engine = canvas.engine
    path = tmp_path / "尺寸"
    graph = replace(canvas.graph, positions={"a": (0, 80), "b": (380, 135), "g1": (0, 0)})
    engine.set_node_graph(engine.root_id, graph)
    canvas.reset_zoom()
    canvas.center_root()
    canvas.select_nodes(["g1"])
    view = canvas.view
    left = Qt.MouseButton.LeftButton
    base = canvas.layout_result
    corner = view.group_resize._handle_rect().center().toPoint()
    end = corner + QPoint(82, 65)
    revision = engine.revision

    def resize_preview():
        assert view.group_resize.hit_test(corner)
        _pointer_event(canvas, QEvent.Type.MouseButtonPress, corner, left, left)
        _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left)
        assert view.group_resize.active and engine.revision == revision and canvas.graph == graph

    resize_preview()
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    assert canvas.layout_result == base and canvas.graph == graph
    resize_preview()
    expanded = canvas.layout_result.group_frames["g1"].bounds
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    minimum = expanded.width, expanded.height
    assert canvas.graph.groups[0].min_size == minimum and engine.revision == revision + 1
    save_directory(engine.document, path)
    assert load_directory(path).page_view(canvas.current_page_id).graph.groups[0].min_size == minimum

    canvas.select_nodes(["b"])
    inside = view.mapFromScene(QPointF(expanded.right - 5, expanded.bottom - 5))
    _start_graph_drag(canvas, _node_point(canvas, "b"), inside)
    _release_graph_drag(canvas, inside)
    added = canvas.layout_result.group_frames["g1"].bounds
    assert canvas.graph.parent_groups()["b"] == "g1"
    assert added.width > expanded.width and added.height > expanded.height
    outside = view.mapFromScene(QPointF(435, 135))
    _start_graph_drag(canvas, _node_point(canvas, "b"), outside)
    _release_graph_drag(canvas, outside)
    assert "b" not in canvas.graph.parent_groups()
    assert canvas.layout_result.group_frames["g1"].bounds == expanded
    canvas.select_nodes(["g1"])
    assert _choose_graph_menu_action(canvas, _node_point(canvas, "g1"), "恢复自动大小", qtbot)
    assert canvas.graph.groups[0].min_size is None and canvas.layout_result.group_frames["g1"].bounds.width < minimum[0]
    assert engine.undo() and canvas.graph.groups[0].min_size == minimum
    canvas.auto_layout_graph()
    assert canvas.graph.positions == {} and canvas.graph.groups[0].min_size == minimum


def test_deleting_group_confirms_nested_members_and_owned_pages(window, monkeypatch, qtbot):
    canvas = _create_graph(window, FOUR_NODES + '\n节点 g "外层"\n节点 h "内层"\n'
                           '分组 g a h\n分组 h b\nc -> g\nd -> c')
    engine = canvas.engine
    detail = engine.create_graph_node_page(engine.root_id, "h", kind="detail")
    canvas.fit_to_view()
    canvas.select_nodes(["g", "h"])
    edge = GraphEdge("d", "c")
    canvas.view.graph_edge_edit.select_edge(edge, toggle=True)
    messages = []
    answers = iter((QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: (messages.append(_args[2]), next(answers))[1])
    before = canvas.graph
    assert _choose_graph_menu_action(canvas, _node_point(canvas, "g"), "删除所选", qtbot)
    assert canvas.graph == before and "3 个组内成员" in messages[-1] and "1 张详情页" in messages[-1]
    assert "另外选中的 1 条连线" in messages[-1]
    assert set(canvas.selected_node_ids()) == {"g", "h"} and canvas.view.graph_edge_edit.selected_edges == {edge}
    canvas.delete_selected()
    assert tuple(canvas.document_view.nodes) == ("c", "d")
    assert canvas.graph.edges == () and canvas.graph.groups == () and engine.page_summary(detail) is None
    assert engine.undo() and canvas.graph == before and engine.page_summary(detail) is not None


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
    assert canvas.path_navigation.full_path.endswith(" › 依赖关系 · 图")
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


@pytest.mark.parametrize("location", ["主导图", "子导图", "详情页", "独立想法"])
def test_graph_breadcrumb_uses_tree_page_roots(window, qtbot, location):
    """空图也按页面层级导航。同名页根、详情和想法用各自的标识定位。"""
    canvas = window.application.canvas
    engine = canvas.engine
    root = engine.root_id
    engine.update_node(root, text="第二大脑")
    expected = [(None, root, "第二大脑", "main")]
    if location in ("子导图", "详情页"):
        branch = engine.add_child(root, "上层普通节点")
        entry = engine.add_child(branch, "开源软件研究")
        page = engine.create_submap(entry)
        root = engine.page_document(page).root_id
        expected.append((page, root, "开源软件研究", "submap"))
        if location == "子导图":
            page = engine.create_submap(root)
            root = engine.page_document(page).root_id
            expected.append((page, root, "开源软件研究", "submap"))
        else:
            page = engine.create_detail_page(engine.add_child(root, "笔记"))
            root = engine.page_document(page).root_id
            expected.append((page, root, "笔记 · 详情", "detail"))
    elif location == "独立想法":
        root = engine.add_inspiration("未整理的研究", position=(0, 0))
        expected = [(engine.inspiration_id, root, "未整理的研究", "inspiration")]
    branch = engine.add_child(root, "普通分支")
    owner = engine.add_child(branch, "Blender")
    if location == "详情页":
        # 旧版本允许详情内挂图。通过加载旧记录构造，当前引擎不再允许创建。
        legacy = engine.document
        legacy.nodes[owner].extension_pages["graph"] = NodeGraph().to_record()
        engine.replace_document(legacy)
    else:
        engine.set_node_graph(owner, NodeGraph())
    graph_page = graph_page_id(owner)
    expected.append((graph_page, owner, "Blender · 图", "graph"))
    engine.mark_saved()
    revision = engine.revision
    canvas.set_editable(False)
    bar = canvas.path_navigation
    for index, (page_id, node_id, title, kind) in enumerate(expected):
        canvas.open_node_graph(owner)
        QApplication.processEvents()
        assert [(item.page_id, item.node_id, item.text, bar.page_kinds[item.page_id])
                for item in bar.entries] == expected
        assert bar.full_path == bar.toolTip() == bar.accessibleDescription() == " › ".join(
            title for _, _, title, _ in expected)
        # 停靠栏宽度会影响路径省略。按显示顺序展开菜单，保留同名项各自的跳转位置。
        controls = []
        for i in range(1, bar.row.count()):
            button = bar.row.itemAt(i).widget()
            if isinstance(button, QToolButton):
                controls.extend(button.menu().actions() if button.menu() else [button])
        assert len(controls) == len(expected)
        control = controls[index]
        assert not control.icon().isNull()
        if isinstance(control, QToolButton):
            assert control.property("page_kind") == kind and control.toolTip() == title
            qtbot.mouseClick(control, Qt.MouseButton.LeftButton)
        else:
            assert control.text() == title
            control.trigger()
        assert canvas.current_page_id == page_id
        assert canvas.selected_node_ids() == ([] if kind == "graph" else [node_id])
    bar.back_button.click()
    assert canvas.current_page_id == expected[-2][0] and canvas.selected_node_ids() == [owner]
    assert engine.revision == revision and not engine.is_dirty


@pytest.mark.parametrize("selection", ["节点", "连线"])
def test_current_graph_breadcrumb_centers_without_changing_selection_or_zoom(window, qtbot, selection):
    """点击当前图页项回到内容中心，普通导航调用继续保留视口。"""
    canvas = _create_graph(window)
    if selection == "节点":
        canvas.select_nodes(["a", "b"])
    else:
        canvas.view.graph_edge_edit.select_edge(GraphEdge("a", "b"))
    canvas.view.zoom(1.65)
    center = canvas.layout_result.bounds.center
    canvas.view.update_navigation_rect(center=QPointF(*center) + QPointF(125, 75))
    canvas.engine.mark_saved()
    revision = canvas.engine.revision
    before = canvas.capture_page_view_state()
    edges = canvas.view.graph_edge_edit.selected_edges
    assert before["center"] != pytest.approx(center, abs=3)
    canvas.navigate_to(canvas.current_page_id)
    assert canvas.capture_page_view_state() == before

    button = next(button for button in canvas.path_navigation.findChildren(QToolButton)
                  if button.isVisible() and button.property("page_kind") == "graph")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    after = canvas.capture_page_view_state()
    assert after["center"] == pytest.approx(center, abs=2)
    assert after["zoom"] == before["zoom"] and after["selection"] == before["selection"]
    assert canvas.view.graph_edge_edit.selected_edges == edges
    assert canvas.engine.revision == revision and not canvas.engine.is_dirty


def test_graph_breadcrumb_follows_renamed_and_moved_owner(window):
    """所属节点迁入子导图后更新路径，首次返回仍能显示屏外入口。"""
    canvas = window.application.canvas
    engine = canvas.engine
    entry = engine.add_child(engine.root_id, "研究")
    branch = entry
    for _ in range(8):
        branch = engine.add_child(branch, "较长的普通分支名称")
    owner = engine.add_child(branch, "旧名")
    canvas.select_nodes([owner])
    _create_graph(window, text=None)
    bar = canvas.path_navigation
    engine.update_node(owner, text="新名")
    assert bar.full_path == f"{engine.get_node(engine.root_id).text} › 新名 · 图"
    page = engine.convert_descendants_to_submap(entry)
    assert canvas.current_page_id == graph_page_id(owner)
    assert [item.page_id for item in bar.entries] == [None, page, graph_page_id(owner)]
    assert bar.full_path.endswith(" › 研究 › 新名 · 图")

    bar.back_button.click()
    QApplication.processEvents()
    assert canvas.current_page_id == page and canvas.selected_node_ids() == [owner]
    owner_center = canvas.node_items[owner].sceneBoundingRect().center()
    assert canvas.view.viewport().rect().contains(canvas.view.mapFromScene(owner_center))
    canvas.open_node_graph(owner)
    engine.remove_nodes([owner])
    assert canvas.current_page_id == page


def test_graph_breadcrumb_checks_input_method_before_leaving(window, qtbot):
    """祖先项和返回按钮沿用统一检查，候选结束后切页会提交正文草稿。"""
    canvas = window.application.canvas
    owner = canvas.engine.add_child(canvas.engine.root_id, "入口节点")
    canvas.select_nodes([owner])
    _create_graph(window)
    canvas.begin_edit("a")
    editor = canvas.editor
    editor.setPlainText("已修改的标题")
    canvas.scene.sendEvent(editor, QInputMethodEvent("候选", []))
    bar = canvas.path_navigation
    ancestor = next(button for button in bar.findChildren(QToolButton)
                    if button.isVisible() and button.property("page_kind") == "main")
    errors = []
    canvas.error_occurred.connect(errors.append)
    revision = canvas.engine.revision

    qtbot.mouseClick(ancestor, Qt.MouseButton.LeftButton)
    bar.back_button.click()
    assert len(errors) == 2 and all("候选" in error for error in errors)
    assert canvas.current_page_id == graph_page_id(owner) and canvas.is_composing
    assert canvas.editor is editor and canvas.engine.revision == revision

    canvas.scene.sendEvent(editor, QInputMethodEvent())
    bar.back_button.click()
    assert canvas.current_page_id is None and canvas.selected_node_ids() == [owner]
    assert canvas.engine.node_graph(owner).nodes[0].title == "已修改的标题"
    assert canvas.engine.revision == revision + 1


def test_text_editor_reports_lines_and_keeps_draft_then_submits_once(window):
    canvas = _create_graph(window)
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


def test_manual_edges_sync_split_and_save(window, qtbot):
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
    canvas.center_root()
    assert first.duplicate()
    second = window.documents.active
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    other = second.canvas
    # 分屏初建时控件仍是占位尺寸，先等布局完成再使用场景坐标模拟拖动。
    qtbot.waitUntil(lambda: other.view.viewport().height() > 200)
    other.fit_to_view()
    assert edge.key in other.branch_items and edge.key in canvas.branch_items
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
    assert updated.key in canvas.branch_items and updated.key in other.branch_items
    assert canvas.graph.positions == other.graph.positions == manual_positions
    assert first.engine.revision == revision + 1

    handle = _endpoint_point(other, updated)
    other.set_editable(False)
    assert not other.action("delete").isEnabled()
    assert view.graph_edge_edit.handle_at(handle) is None
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert other.graph.edges == (updated,) and first.engine.revision == revision + 1
    other.set_editable(True)

    assert first.document.save()
    fresh = load_directory(first.document.path).page_view(canvas.current_page_id).graph
    assert fresh.edges == (updated,)
    assert fresh.positions == manual_positions


@pytest.mark.parametrize("theme, normal_color, selected_color", [
    ("light", "#65768d", "#ac5208"),
    ("dark", "#98a8bd", "#ffca70"),
])
def test_node_selection_highlights_incoming_outgoing_and_self_loop(window, theme, normal_color, selected_color):
    """验证两种主题下普通线、关联边和直接选线在真实视口中使用不同颜色。"""
    canvas = _create_graph(window, CHAIN + "\nb -> b")
    window.manager.set_theme(theme)
    canvas.fit_to_view()
    viewport = canvas.view.viewport()
    viewport_rect = viewport.rect()
    edges = canvas.graph.edges
    points = {edge: _edge_point(canvas, edge) for edge in edges}

    def colors_at_edges(radius=2):
        image = QImage(viewport_rect.size(), QImage.Format.Format_RGB32)
        viewport.render(image)
        return {edge: [image.pixelColor(point + QPoint(dx, dy))
                       for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)]
                for edge, point in points.items()}

    canvas.select_nodes([])
    revision = canvas.engine.revision
    normal = colors_at_edges()
    canvas.select_nodes(["b"])
    related = colors_at_edges()
    unrelated = GraphEdge("c", "d")
    accent = QColor(canvas.ui_colors.accent)
    for edge in edges:
        if edge == unrelated:
            assert related[edge] == normal[edge]
        else:
            assert accent in related[edge] and related[edge] != normal[edge]
    assert canvas.graph_edge_selection_count == 0 and canvas.engine.revision == revision
    assert "已选 1 个节点、0 条连线" in window.status_label.text()
    canvas.select_nodes(["b", "d"])
    assert all(accent in colors for colors in colors_at_edges().values())
    canvas.select_nodes([])
    assert colors_at_edges() == normal

    # 400% 下原描边已宽于固定的选中线宽，叠加效果仍应清晰可辨。
    incoming = GraphEdge("a", "b")
    line_center = canvas.branch_items[incoming.key].path().pointAtPercent(0.5)
    canvas.view.zoom(4 / canvas.zoom_factor)
    canvas.view.update_navigation_rect(center=line_center)
    points = {incoming: _edge_point(canvas, incoming)}
    normal = colors_at_edges(7)[incoming]
    canvas.select_nodes(["b"])
    related = colors_at_edges(7)[incoming]
    canvas.view.graph_edge_edit.select_edge(incoming)
    explicit = colors_at_edges(7)[incoming]
    normal_color, selected_color = QColor(normal_color), QColor(selected_color)
    assert normal_color in normal and accent not in normal and selected_color not in normal
    assert 0 < related.count(accent) < explicit.count(selected_color)
    assert selected_color not in related and accent not in explicit
    assert canvas.graph_edge_selection_count == 1 and canvas.engine.revision == revision


@pytest.mark.parametrize("theme, dpr, selected_color", [
    ("light", 1, "#ac5208"),
    ("dark", 2, "#ffca70"),
])
def test_edge_highlight_reaches_both_node_borders(window, theme, dpr, selected_color):
    """放大后检查连线起点和箭头尖端的颜色，并确认线身不会盖住卡片边框。"""
    canvas = _create_graph(window)
    window.manager.set_theme(theme)
    view = canvas.view
    view.zoom(4 / canvas.zoom_factor)
    canvas.center_root()
    edge = GraphEdge("a", "b")
    branch = canvas.layout_result.branches[edge.key]
    start, tip = (view.viewportTransform().map(QPointF(*point)) for point in (branch.start, branch.end))
    viewport = view.viewport()

    def snapshot():
        image = QImage(viewport.size() * dpr, QImage.Format.Format_RGB32)
        image.setDevicePixelRatio(dpr)
        viewport.render(image)
        return image

    def pixel(image, point, offset):
        return image.pixelColor(((point + QPointF(0, offset)) * dpr).toPoint())

    canvas.select_nodes([])
    normal = snapshot()
    canvas.select_nodes(["b"])
    related = snapshot()
    accent = QColor(canvas.ui_colors.accent)
    canvas.select_nodes(["a"])
    related_at_tip = snapshot()
    for offset in range(1, 9):
        assert pixel(related, start, offset) == accent
    for offset in range(-8, -3):
        assert pixel(related_at_tip, tip, offset) == accent
    assert pixel(related, start, -3) == pixel(normal, start, -3)
    assert pixel(related_at_tip, tip, 3) == pixel(normal, tip, 3)

    view.graph_edge_edit.select_edge(edge)
    selected = snapshot()
    assert pixel(selected, start, 7) == QColor(selected_color)
    assert pixel(selected, tip, -7) == QColor(selected_color)


def test_edge_selection_outline_and_arrow_survive_theme_switch(window):
    """相交时选线与普通线有间隔，批量选中和换色后仍可追踪线身与箭头。"""
    window.manager.set_theme("light")
    canvas = _create_graph(window, FOUR_NODES + "\na -> b\nc -> d")
    nodes = canvas.layout_result.nodes
    positions = {
        "a": (-140 - nodes["a"].width, -nodes["a"].height / 2),
        "b": (140, -nodes["b"].height / 2),
        "c": (-nodes["c"].width / 2, -75 - nodes["c"].height),
        "d": (-nodes["d"].width / 2, 75),
    }
    canvas.engine.set_node_graph(canvas.engine.root_id, NodeGraph(canvas.graph.nodes, canvas.graph.edges, positions))
    view = canvas.view
    view.zoom(2 / canvas.zoom_factor)
    view.update_navigation_rect(center=QPointF())
    viewport = view.viewport()
    horizontal, vertical = canvas.graph.edges
    controls = view.graph_edge_edit
    revision = canvas.engine.revision

    def snapshot():
        image = QImage(viewport.size(), QImage.Format.Format_RGB32)
        viewport.render(image)
        return image

    def near_crossing(image, y):
        crossing = view.mapFromScene(QPointF())
        return image.pixelColor(crossing + QPoint(0, y))

    controls.select_edge(horizontal)
    image = snapshot()
    normal = QColor("#65768d")
    assert near_crossing(image, 0) == QColor("#ac5208")
    assert near_crossing(image, 4).lightness() > normal.lightness()
    assert near_crossing(image, 10) == normal

    window.manager.set_theme("dark")
    image = snapshot()
    assert controls.selected_edges == {horizontal}
    assert near_crossing(image, 0) == QColor("#ffca70")
    normal = QColor("#98a8bd")
    assert near_crossing(image, 4).lightness() < normal.lightness()
    assert near_crossing(image, 10) == normal

    controls.select_edge(vertical, toggle=True)
    image = snapshot()
    assert near_crossing(image, 4) == QColor("#ffca70")
    assert not controls._handles()
    for edge in (horizontal, vertical):
        tip, wing, other_wing = (QPointF(*point) for point in canvas.layout_result.branches[edge.key].arrow)
        inside_wing = view.mapFromScene(tip * 0.18 + wing * 0.75 + other_wing * 0.07)
        assert image.rect().contains(inside_wing)
        assert image.pixelColor(inside_wing) == QColor("#ffca70")
    assert canvas.engine.revision == revision


def test_click_and_box_ignore_blank_inside_bent_edge(window, qtbot):
    """跨层连线绕过节点时，曲线和首尾之间的空白可以点击和框选。"""
    canvas = _create_graph(window, CHAIN + "\na -> d")
    canvas.center_root()
    view = canvas.view
    controls = view.graph_edge_edit
    outer = GraphEdge("a", "d")
    path = canvas.branch_items[outer.key].path()
    leg = path.pointAtPercent(0.5)
    start, end = path.pointAtPercent(0), path.currentPosition()
    chord = start + (end - start) * ((leg.y() - start.y()) / (end.y() - start.y()))
    blank = view.mapFromScene((leg + chord) / 2)
    left = Qt.MouseButton.LeftButton
    shift = Qt.KeyboardModifier.ShiftModifier

    qtbot.mouseClick(view.viewport(), left, pos=view.mapFromScene(leg))
    assert controls.selected_edges == {outer}
    qtbot.mouseClick(view.viewport(), left, pos=blank)
    assert not controls.selected_edges

    corner = QPoint(6, 6)
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, blank - corner, left, left, modifiers=shift)
    _pointer_event(canvas, QEvent.Type.MouseMove, blank + corner, buttons=left, modifiers=shift)
    assert controls.active
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, blank + corner, left, modifiers=shift)
    assert not controls.selected_edges


def test_click_arrow_prefers_its_edge_over_neighbor_tolerance(window, qtbot):
    """缩小后两个入边的点击容差重叠时，优先选择实际点中的箭头。"""
    canvas = _create_graph(window, THREE_NODES + "\na -> c\nb -> c")
    view = canvas.view
    view.zoom(0.45 / canvas.zoom_factor)
    canvas.center_root()
    edge = GraphEdge("a", "c")
    tip, wing_a, wing_b = (QPointF(*point) for point in canvas.layout_result.branches[edge.key].arrow)
    arrow = view.mapFromScene((tip + wing_a + wing_b) / 3)

    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=arrow)
    assert view.graph_edge_edit.selected_edges == {edge}


def test_ctrl_selects_edges_and_delete_commits_one_change(window, qtbot):
    """点选互斥、Ctrl 逐条增减、Delete 批量提交以及历史重放都经过画布入口。"""
    canvas = _create_graph(window, CHAIN)
    canvas.fit_to_view()
    view = canvas.view
    controls = view.graph_edge_edit
    first, middle, last = canvas.graph.edges
    left = Qt.MouseButton.LeftButton
    ctrl = Qt.KeyboardModifier.ControlModifier
    initial = canvas.graph
    revision = canvas.engine.revision
    canvas.select_nodes(["b"])
    qtbot.mouseClick(view.viewport(), left, pos=_edge_point(canvas, first))
    assert controls.selected_edge == first and not canvas.selected_node_ids()
    assert len(controls._handles()) == 2
    qtbot.mouseClick(view.viewport(), left, ctrl, pos=_edge_point(canvas, last))
    assert controls.selected_edges == {first, last} and not controls._handles()
    qtbot.mouseClick(view.viewport(), left, ctrl, pos=_edge_point(canvas, first))
    assert controls.selected_edge == last
    qtbot.mouseClick(view.viewport(), left, ctrl, pos=_edge_point(canvas, last))
    assert not controls.selected_edges and not canvas.action("delete").isEnabled()
    qtbot.mouseClick(view.viewport(), left, pos=_edge_point(canvas, first))
    qtbot.mouseClick(view.viewport(), left, ctrl, pos=_edge_point(canvas, last))
    assert "已选 0 个节点、2 条连线" in window.status_label.text()
    assert canvas.engine.revision == revision

    canvas.set_editable(False)
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert canvas.graph == initial and controls.selected_edges == {first, last}
    canvas.set_editable(True)
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert canvas.graph.edges == (middle,) and canvas.graph.nodes == initial.nodes
    assert not controls.selected_edges and canvas.engine.revision == revision + 1
    canvas.action("undo").trigger()
    assert canvas.graph == initial
    canvas.action("redo").trigger()
    assert canvas.graph.edges == (middle,)


def test_shift_edge_box_replaces_and_appends_alongside_mixed_node_box(window, qtbot):
    """从空白处框到线身可选线，取消和迟到的释放保留已完成的选择。"""
    canvas = _create_graph(window, CHAIN)
    canvas.fit_to_view()
    view = canvas.view
    controls = view.graph_edge_edit
    first, middle, last = canvas.graph.edges
    revision = canvas.engine.revision
    left = Qt.MouseButton.LeftButton
    shift = Qt.KeyboardModifier.ShiftModifier
    ctrl = Qt.KeyboardModifier.ControlModifier
    offsets = QPoint(20, 9)

    def box_edge(edge, modifiers, *, cancel=False):
        center = _edge_point(canvas, edge)
        start, end = center - offsets, center + offsets
        assert canvas.graph_edge_at(start) is None and view.itemAt(start) is None
        _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left, modifiers=modifiers)
        _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left, modifiers=modifiers)
        assert controls.active and controls._rubber_band.isVisible()
        if cancel:
            qtbot.keyClick(view, Qt.Key.Key_Escape)
            assert not controls.active and not controls._rubber_band.isVisible()
        _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left, modifiers=modifiers)
        assert not controls.active and not controls._rubber_band.isVisible()

    box_edge(first, Qt.KeyboardModifier.NoModifier)
    assert controls.selected_edges == {first} and not canvas.selected_node_ids()
    canvas.select_nodes(["a", "b"])
    box_edge(first, shift)
    assert controls.selected_edges == {first} and not canvas.selected_node_ids()
    # 只把末端箭头外翼框入，线身的中心不在框里。
    _, left_wing, right_wing = canvas.layout_result.branches[last.key].arrow
    wing = max((left_wing, right_wing), key=lambda point: point[0])
    wing = view.mapFromScene(QPointF(*wing))
    start, end = wing + QPoint(17, -5), wing + QPoint(-2, 3)
    assert canvas.graph_edge_at(start) is None and view.itemAt(start) is None
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left, modifiers=ctrl | shift)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left, modifiers=ctrl | shift)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left, modifiers=ctrl | shift)
    assert controls.selected_edges == {first, last}
    box_edge(middle, shift, cancel=True)
    assert controls.selected_edges == {first, last}
    box_edge(middle, shift)
    assert controls.selected_edges == {middle}

    rect = view.mapFromScene(canvas.node_items["c"].sceneBoundingRect()).boundingRect().adjusted(-8, -8, 8, 8)
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, rect.topLeft(), left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, rect.bottomRight(), buttons=left)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, rect.bottomRight(), left)
    assert canvas.selected_node_ids() == ["c"] and controls.selected_edges == {middle, last}
    assert canvas.engine.revision == revision


@pytest.mark.parametrize("finish", ["release", "escape", "focus"])
def test_mixed_marquee_highlights_while_growing_and_shrinking(window, qtbot, finish):
    canvas = _create_graph(window, CHAIN)
    window.manager.set_theme("light")
    canvas.fit_to_view()
    view = canvas.view
    first, middle, last = canvas.graph.edges
    left, ctrl = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier
    qtbot.mouseClick(view.viewport(), left, pos=_edge_point(canvas, last))
    qtbot.mouseClick(view.viewport(), left, ctrl, pos=_node_point(canvas, "d"))
    assert canvas.selected_node_ids() == ["d"] and view.graph_edge_edit.selected_edges == {last}
    revision = canvas.engine.revision
    box = view.mapFromScene(canvas.node_items["b"].sceneBoundingRect()).boundingRect()
    start = QPoint(box.left() - 12, box.center().y())
    node_only = box.center() + QPoint(0, 3)
    with_edge = _edge_point(canvas, middle) + QPoint(5, 2)
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left)

    observed = []

    def read_selection(*_):
        observed.append((canvas.selected_node_ids(), view.graph_edge_edit.selected_edges))

    canvas.selection_changed.connect(read_selection)
    canvas.graph_edge_selection_changed.connect(read_selection)
    _pointer_event(canvas, QEvent.Type.MouseMove, with_edge, buttons=left)
    assert observed and all(pair == (["b"], {middle}) for pair in observed)
    assert canvas.node_items["b"].isSelected() and not canvas.node_items["d"].isSelected()
    assert "已选 1 个节点、1 条连线" in window.status_label.text()
    assert view.graph_edge_edit._rubber_band.isVisible()
    # 在选框外取色，验证同一帧中明确选中的线和节点另一侧的关联线各自高亮。
    for edge, percent, color in ((middle, 0.8, "#ac5208"), (first, 0.5, canvas.ui_colors.accent)):
        point = view.mapFromScene(canvas.branch_items[edge.key].path().pointAtPercent(percent))
        image = _viewport_region(view, point, 5)
        assert QColor(color) in [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
    _pointer_event(canvas, QEvent.Type.MouseMove, node_only, buttons=left)
    assert canvas.selected_node_ids() == ["b"] and not view.graph_edge_edit.selected_edges
    assert "已选 1 个节点、0 条连线" in window.status_label.text()
    if finish == "escape":
        qtbot.keyClick(view, Qt.Key.Key_Escape)
    elif finish == "focus":
        QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut))
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, with_edge, left)
    assert not view.graph_edge_edit._rubber_band.isVisible()
    expected = (["b"], {middle}) if finish == "release" else (["d"], {last})
    assert (canvas.selected_node_ids(), view.graph_edge_edit.selected_edges) == expected
    assert canvas.engine.revision == revision


def test_ctrl_box_appends_and_ctrl_click_toggles_only_the_hit_object(window, qtbot):
    canvas = _create_graph(window, CHAIN)
    canvas.fit_to_view()
    canvas.set_editable(False)
    view = canvas.view
    left = Qt.MouseButton.LeftButton
    ctrl, shift = Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier
    first, middle, last = canvas.graph.edges
    canvas.select_nodes(["a"])
    view.graph_edge_edit.select_edge(first, toggle=True)
    before = canvas.engine.revision

    def box(rect, modifiers):
        _pointer_event(canvas, QEvent.Type.MouseButtonPress, rect.topLeft(), left, left, modifiers=modifiers)
        _pointer_event(canvas, QEvent.Type.MouseMove, rect.bottomRight(), buttons=left, modifiers=modifiers)
        _pointer_event(canvas, QEvent.Type.MouseButtonRelease, rect.bottomRight(), left, modifiers=modifiers)

    region = view.mapFromScene(canvas.node_items["c"].sceneBoundingRect()).boundingRect().adjusted(-7, -7, 7, 7)
    box(region, ctrl | shift)
    assert canvas.selected_node_ids() == ["a"] and view.graph_edge_edit.selected_edges == {first, middle, last}
    box(region, ctrl)
    assert canvas.selected_node_ids() == ["a", "c"] and view.graph_edge_edit.selected_edges == {first, middle, last}
    qtbot.mouseClick(view.viewport(), left, ctrl, pos=_edge_point(canvas, first))
    for key in ("c", "a"):
        qtbot.mouseClick(view.viewport(), left, ctrl, pos=_node_point(canvas, key))
    assert not canvas.selected_node_ids() and view.graph_edge_edit.selected_edges == {middle, last}
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert canvas.engine.revision == before and not canvas.action("delete").isEnabled()


def test_marquee_selects_only_fully_enclosed_visible_groups(window):
    canvas = _create_graph(window, '节点 o "外层"\n节点 i "内层"\n节点 a "甲"\n节点 b "乙"\n'
                           '节点 z "同层项"\n分组 o i z\n分组 i a b\na -> b')
    positions = {"a": (50, 75), "b": (180, 160), "z": (370, 240)}
    canvas.engine.set_node_graph(canvas.engine.root_id, replace(canvas.graph, positions=positions))
    canvas.fit_to_view()
    view = canvas.view
    frame = canvas.layout_result.group_frames["i"]
    start = view.mapFromScene(QPointF(frame.bounds.x - 7, frame.bounds.y - 7))
    header = view.mapFromScene(QPointF(*frame.header.center))
    end = view.mapFromScene(QPointF(frame.bounds.right + 3, frame.bounds.bottom + 3))
    left = Qt.MouseButton.LeftButton
    assert view.graph_group_at(start, include_body=True) == "o" and view.graph_group_at(start) is None
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, header, buttons=left)
    assert not canvas.selected_node_ids()
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left)
    assert set(canvas.selected_node_ids()) == {"i", "a", "b"}
    assert view.graph_edge_edit.selected_edges == {GraphEdge("a", "b")}
    _pointer_event(canvas, QEvent.Type.MouseMove, end - QPoint(12, 0), buttons=left)
    assert set(canvas.selected_node_ids()) == {"a", "b"}
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    assert set(canvas.selected_node_ids()) == {"i", "a", "b"}


@pytest.mark.parametrize("entry", ["keyboard", "node_menu", "edge_menu"])
def test_mixed_batch_delete_is_one_change_across_split_views(window, qtbot, entry):
    original = window.documents.active
    canvas = _create_graph(window, CHAIN)
    positions = {"a": (0, 0), "b": (180, -105), "c": (180, 245), "d": (425, 245)}
    canvas.engine.set_node_graph(canvas.engine.root_id, replace(canvas.graph, positions=positions))
    before = canvas.graph
    first, middle, last = before.edges
    assert original.duplicate()
    session = window.documents.active
    window.host.editor.split_token(session.id, Qt.Orientation.Horizontal)
    other = session.canvas
    qtbot.waitUntil(lambda: other.view.viewport().height() > 200)
    other.fit_to_view()
    canvas.select_nodes(["d"])
    for edge in (first, last):
        canvas.view.graph_edge_edit.select_edge(edge, toggle=True)
    view = other.view
    left, ctrl = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier
    a, line = _node_point(other, "a"), _edge_point(other, middle)
    if entry == "keyboard":
        card = view.mapFromScene(other.node_items["a"].sceneBoundingRect()).boundingRect()
        start = QPoint(card.left() - 8, a.y() + card.height() // 4)
        end = line + QPoint(6, 4)
        _pointer_event(other, QEvent.Type.MouseButtonPress, start, left, left)
        _pointer_event(other, QEvent.Type.MouseMove, end, buttons=left)
        _pointer_event(other, QEvent.Type.MouseButtonRelease, end, left)
    else:
        qtbot.mouseClick(view.viewport(), left, pos=a)
        qtbot.mouseClick(view.viewport(), left, ctrl, pos=line)
    assert other.selected_node_ids() == ["a"] and view.graph_edge_edit.selected_edges == {middle}
    revision = original.engine.revision
    if entry == "keyboard":
        qtbot.keyClick(view, Qt.Key.Key_Delete)
    else:
        assert _choose_graph_menu_action(other, a if entry == "node_menu" else line, "删除所选", qtbot)
    assert other.graph == canvas.graph and other.graph.edges == (last,)
    assert [node.id for node in other.graph.nodes] == ["b", "c", "d"]
    assert original.engine.revision == revision + 1
    assert not other.selected_node_ids() and not view.graph_edge_edit.selected_edges
    assert canvas.selected_node_ids() == ["d"] and canvas.view.graph_edge_edit.selected_edges == {last}
    other.action("undo").trigger()
    assert other.graph == canvas.graph == before
    other.action("redo").trigger()
    assert other.graph == canvas.graph and other.graph.edges == (last,)


def test_edge_menu_deletes_batch_and_other_views_keep_surviving_selection(window, qtbot):
    """右键已选边保留整组，删除后其他标签只剔除失效的选线。"""
    original = window.documents.active
    canvas = _create_graph(window, CHAIN)
    canvas.fit_to_view()
    first, middle, last = canvas.graph.edges
    left, right = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton
    ctrl = Qt.KeyboardModifier.ControlModifier
    qtbot.mouseClick(canvas.view.viewport(), left, pos=_edge_point(canvas, first))
    qtbot.mouseClick(canvas.view.viewport(), left, ctrl, pos=_edge_point(canvas, middle))
    assert original.duplicate()
    second = window.documents.active
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    other = second.canvas
    qtbot.waitUntil(lambda: other.view.viewport().height() > 200)
    other.fit_to_view()
    assert other.graph_edge_selection_count == 0
    qtbot.mouseClick(other.view.viewport(), left, pos=_edge_point(other, first))
    qtbot.mouseClick(other.view.viewport(), left, ctrl, pos=_edge_point(other, last))
    assert other.view.graph_edge_edit.selected_edges == {first, last}
    revision = original.engine.revision
    menus = []

    def choose_delete():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            menus.append((menu.actions()[0].text(), other.view.graph_edge_edit.selected_edges))
            qtbot.keyClick(menu, Qt.Key.Key_Down)
            qtbot.keyClick(menu, Qt.Key.Key_Return)

    QTimer.singleShot(0, choose_delete)
    qtbot.mouseClick(other.view.viewport(), right, pos=_edge_point(other, first))
    assert menus == [("删除所选 2 条连线", {first, last})]
    assert canvas.graph == other.graph and other.graph.edges == (middle,)
    assert canvas.view.graph_edge_edit.selected_edges == {middle}
    assert not other.view.graph_edge_edit.selected_edges and original.engine.revision == revision + 1
    other.action("undo").trigger()
    assert canvas.graph.edges == other.graph.edges == (first, middle, last)
    assert canvas.view.graph_edge_edit.selected_edges == {middle}

    other.fit_to_view()
    qtbot.mouseClick(other.view.viewport(), left, pos=_edge_point(other, first))
    qtbot.mouseClick(other.view.viewport(), left, ctrl, pos=_edge_point(other, last))
    menu = QMenu(other)
    window.application.populate_edit_menu(menu)
    delete = other.action("delete")
    assert delete in menu.actions() and delete.isEnabled()
    delete.trigger()
    assert other.graph.edges == (middle,) and original.engine.revision == revision + 3
    menu.deleteLater()


def test_edge_menu_rejects_entire_mixed_selection_after_another_view_removes_an_edge(window, qtbot):
    """菜单打开期间的跨标签修改使批量删除失效，其余待删边仍在原图。"""
    original = window.documents.active
    canvas = _create_graph(window, CHAIN)
    assert original.duplicate()
    other = window.documents.active.canvas
    other.fit_to_view()
    first, middle, last = other.graph.edges
    left = Qt.MouseButton.LeftButton
    qtbot.mouseClick(other.view.viewport(), left, pos=_edge_point(other, first))
    qtbot.mouseClick(other.view.viewport(), left, Qt.KeyboardModifier.ControlModifier, pos=_edge_point(other, last))
    qtbot.mouseClick(other.view.viewport(), left, Qt.KeyboardModifier.ControlModifier, pos=_node_point(other, "d"))
    errors = []
    other.error_occurred.connect(errors.append)
    revision = original.engine.revision

    def remove_from_original_then_choose():
        menu = QApplication.activePopupWidget()
        if not isinstance(menu, QMenu):
            return
        canvas.apply_graph_text(CHAIN.replace("\na -> b", ""))
        qtbot.keyClick(menu, Qt.Key.Key_Down)
        qtbot.keyClick(menu, Qt.Key.Key_Return)

    QTimer.singleShot(0, remove_from_original_then_choose)
    qtbot.mouseClick(other.view.viewport(), Qt.MouseButton.RightButton, pos=_edge_point(other, first))
    assert other.graph.edges == (middle, last)
    assert other.view.graph_edge_edit.selected_edges == {last}
    assert other.selected_node_ids() == ["d"] and "d" in other.document_view.nodes
    assert original.engine.revision == revision + 1 and errors == ["所选连线已变化，请重新选择"]


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


def test_dot_layout_action_is_only_in_graph_context_menus(window, qtbot):
    """通过视口右键操作核对两个入口，空图、只读和编辑时沿用动作禁用规则。"""
    canvas = window.application.canvas
    action = canvas.action("graph_auto_layout")
    assert not action.isVisible() and "graph_filter" not in window.manager.specs
    with pytest.raises(MindMapError, match="当前页面没有图"):
        canvas.auto_layout_graph()

    _create_graph(window, None)
    viewport = canvas.view.viewport()
    right = Qt.MouseButton.RightButton
    background = QPoint(12, 12)
    menu_entries = []

    def choose_layout():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            present = action in menu.actions()
            menu_entries.append((present, present and action.isEnabled()))
            if present and action.isEnabled():
                menu.setActiveAction(action)
                qtbot.keyClick(menu, Qt.Key.Key_Return)
            else:
                menu.close()

    assert action.isVisible() and not action.isEnabled()
    QTimer.singleShot(0, choose_layout)
    qtbot.mouseClick(viewport, right, pos=background)
    assert menu_entries == [(True, False)]
    with pytest.raises(MindMapError, match="请先添加图节点"):
        canvas.auto_layout_graph()

    canvas.apply_graph_text(SAMPLE)
    assert action.isEnabled()
    assert action not in canvas.toolbar.actions()
    assert not canvas.path_navigation.findChild(QToolButton, "mindmap_graph_auto_layout")
    main_menu = window.main_menu()
    edit_menu = next(entry.menu() for entry in main_menu.actions() if entry.text() == "编辑")
    assert action not in edit_menu.actions()
    view_menu = next(entry.menu() for entry in main_menu.actions() if entry.text() == "视图")
    panels_menu = next(entry.menu() for entry in view_menu.actions() if entry.text() == "面板")
    assert "图页面设置" not in {entry.text() for entry in panels_menu.actions()}
    main_menu.deleteLater()

    revision = canvas.engine.revision
    canvas.reset_zoom()
    canvas.select_nodes(["b"])
    QTimer.singleShot(0, choose_layout)
    qtbot.mouseClick(viewport, right, pos=_node_point(canvas, "a"))
    assert canvas.selected_node_ids() == ["a"] and canvas.zoom_factor > 1.0
    canvas.reset_zoom()
    QTimer.singleShot(0, choose_layout)
    qtbot.mouseClick(viewport, right, pos=background)
    assert menu_entries == [(True, False), (True, True), (True, True)]
    assert canvas.zoom_factor > 1.0 and canvas.engine.revision == revision

    canvas.set_editable(False)
    assert action.isVisible() and not action.isEnabled()
    with pytest.raises(MindMapError, match="只读"):
        canvas.auto_layout_graph()
    canvas.set_editable(True)
    canvas.begin_edit("a")
    assert not action.isEnabled()
    with pytest.raises(MindMapError, match="请先完成编辑"):
        canvas.auto_layout_graph()
    assert canvas.is_editing and canvas.engine.revision == revision
    canvas.finish_edit(commit=False)
    assert action.isEnabled()
    canvas.return_to_parent_page()
    assert not action.isVisible()


def test_dot_layout_reflows_whole_graph_with_one_undo_and_syncs_split_views(window, qtbot):
    """恢复全图布局会取消未提交的拖动，分屏同步内容，撤销恢复每个手动坐标。"""
    first = window.documents.active
    canvas = _create_graph(window, THREE_NODES + "\na -> b\nb -> c")
    automatic = canvas.layout_result
    canvas.fit_to_view()
    canvas.select_nodes(["a", "b"])
    start = _node_point(canvas, "a")
    end = start + QPoint(88, 44)
    left = Qt.MouseButton.LeftButton
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    manual = canvas.graph
    assert set(manual.positions) == {"a", "b"}
    assert canvas.layout_result.nodes != automatic.nodes

    assert first.duplicate()
    second = window.documents.active
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    other = second.canvas
    qtbot.waitUntil(lambda: other.view.viewport().height() > 200)
    other.select_nodes(["a", "b"])
    canvas.reset_zoom()
    other.reset_zoom()
    revision = first.engine.revision
    start = _node_point(other, "a")
    end = start + QPoint(30, -25)
    _pointer_event(other, QEvent.Type.MouseButtonPress, start, left, left)
    _pointer_event(other, QEvent.Type.MouseMove, end, buttons=left)
    assert other.view.node_drag.active and other.graph == manual
    other.action("graph_auto_layout").trigger()
    assert not other.view.node_drag.active
    _pointer_event(other, QEvent.Type.MouseButtonRelease, end, left)

    assert canvas.graph == other.graph and not other.graph.positions
    assert other.graph.nodes == manual.nodes and other.graph.edges == manual.edges
    assert canvas.layout_result == other.layout_result == automatic
    assert other.selected_node_ids() == ["a", "b"]
    assert first.engine.revision == revision + 1
    assert canvas.zoom_factor == pytest.approx(1.0)
    assert other.zoom_factor > 1.0
    assert other.capture_page_view_state()["center"] == pytest.approx(automatic.bounds.center, abs=2)

    other.reset_zoom()
    other.action("graph_auto_layout").trigger()
    assert other.zoom_factor > 1.0 and first.engine.revision == revision + 1
    other.action("undo").trigger()
    assert canvas.graph == other.graph == manual
    other.action("redo").trigger()
    assert canvas.graph == other.graph and not other.graph.positions


def test_dot_layout_persists_and_reflows_after_reopening(window, qtbot, tmp_path):
    """保存后重新加载空坐标状态，改连线继续分层，单个节点仍可重新手动定位。"""
    session = window.documents.active
    owner = session.engine.root_id
    canvas = _create_graph(window, THREE_NODES + "\na -> b")
    canvas.select_nodes(["b"])
    start = _node_point(canvas, "b")
    end = start + QPoint(95, 28)
    left = Qt.MouseButton.LeftButton
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    assert set(canvas.graph.positions) == {"b"}
    canvas.action("graph_auto_layout").trigger()
    assert session.document.save()
    assert window.close()

    reopened = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(reopened, before_close_func=lambda item: item.dispose())
    reopened.show()
    canvas = reopened.application.canvas
    assert canvas.current_page_id == graph_page_id(owner) and not canvas.graph.positions
    before = canvas.layout_result.nodes["c"]
    canvas.apply_graph_text(THREE_NODES + "\na -> b\nb -> c")
    after = canvas.layout_result.nodes
    assert not canvas.graph.positions and after["c"] != before and after["c"].y > after["b"].bottom

    canvas.fit_to_view()
    canvas.select_nodes(["b"])
    start = _node_point(canvas, "b")
    end = start + QPoint(50, 24)
    _pointer_event(canvas, QEvent.Type.MouseButtonPress, start, left, left)
    _pointer_event(canvas, QEvent.Type.MouseMove, end, buttons=left)
    _pointer_event(canvas, QEvent.Type.MouseButtonRelease, end, left)
    assert set(canvas.graph.positions) == {"b"}


@pytest.mark.parametrize("width", [180, 280])
def test_narrow_graph_breadcrumb_preserves_navigation(window, qtbot, monkeypatch, width):
    """窄分屏的菜单与复制功能共用完整页面路径，菜单项仍可定位页根。"""
    canvas = window.application.canvas
    engine = canvas.engine
    ancestor_title = "名称很长的祖先页面" * 6
    entry = engine.add_child(engine.root_id, ancestor_title)
    ancestor_page = engine.create_submap(entry)
    canvas.enter_submap(entry)
    ancestor_root = canvas.document_view.root_id
    owner = engine.add_child(ancestor_root, "名称很长的图入口" * 6)
    canvas.select_nodes([owner])
    _create_graph(window)
    canvas.select_nodes(["a", "b"])
    bar = canvas.path_navigation
    bar.setFixedWidth(width)
    qtbot.waitUntil(lambda: any(button.isVisible() and button.text() == "…"
                                for button in bar.findChildren(QToolButton)))
    QApplication.processEvents()
    widgets = [bar.row.itemAt(index).widget() for index in range(bar.row.count())
               if bar.row.itemAt(index).widget() is not None]
    assert all(bar.rect().contains(widget.geometry()) for widget in widgets)
    assert all(not left.geometry().intersects(right.geometry()) for left, right in zip(widgets, widgets[1:]))
    overflow = next(widget for widget in widgets if isinstance(widget, QToolButton) and widget.text() == "…")
    action = next(action for action in overflow.menu().actions() if action.text() == ancestor_title)
    assert not action.icon().isNull()
    menu = bar._menu()
    with monkeypatch.context() as patch:
        patch.setattr(bar, "_menu", lambda: menu)
        patch.setattr(menu, "exec", lambda *_: menu.actions()[0].trigger())
        bar.customContextMenuRequested.emit(QPoint())
    expected = f"{engine.get_node(engine.root_id).text} › {ancestor_title} › {engine.get_node(owner).text} · 图"
    assert QApplication.clipboard().text() == bar.full_path == expected
    action.trigger()
    assert canvas.current_page_id == ancestor_page and canvas.selected_node_ids() == [ancestor_root]


def test_graph_actions_and_status_follow_split_tabs(window):
    """分屏切换时状态按钮绑定活动画布，图动作仍按所属标签的权限启用。"""
    first = window.documents.active
    canvas = _create_graph(window)
    controls = window.application._status_controls
    assert controls._canvas is canvas and all(button.isVisible() for button in controls.buttons.values())
    assert first.duplicate()
    second = window.documents.active
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    assert second.document is first.document and second.canvas.graph == canvas.graph
    assert controls._canvas is second.canvas and second.canvas.action("graph_auto_layout").isEnabled()
    second.canvas.set_editable(False)
    assert not second.canvas.action("graph_auto_layout").isEnabled()
    assert not second.canvas.action("graph_add_edge").isEnabled()
    assert second.canvas.action("graph_text").isEnabled()
    with pytest.raises(MindMapError, match="只读"):
        second.canvas.apply_graph_text("")
    window.documents.activate_view(first.id)
    assert controls._canvas is canvas and canvas.action("graph_auto_layout").isEnabled()
    canvas.return_to_parent_page()
    assert controls._canvas is canvas and not canvas.action("graph_auto_layout").isVisible()
    window.documents.activate_view(second.id)
    assert controls._canvas is second.canvas and not second.canvas.action("graph_auto_layout").isEnabled()


def test_removed_graph_panel_is_ignored_when_restoring_layout(window):
    """旧快照中的可见图侧栏不会恢复，已注册的面板和两个分屏仍可用。"""
    first = window.documents.active
    _create_graph(window)
    assert first.duplicate()
    second = window.documents.active
    editor = window.host.editor
    editor.split_token(second.id, Qt.Orientation.Horizontal)
    groups = {view.id: editor.find_token(view.id)[0].group_id for view in (first, second)}
    assert len(set(groups.values())) == 2

    manager = window.manager
    assert manager.activate("outline", focus=False)
    snapshot = manager.capture_layout()
    legacy = next(tool for tool in snapshot["tools"] if tool["id"] == "outline").copy()
    legacy.update(id="graph_filter", position="right_bottom", last_position="right_bottom", visible=True)
    snapshot["tools"].append(legacy)
    snapshot["active"] = "graph_filter"
    manager.restore_layout(snapshot)
    QApplication.processEvents()
    assert "graph_filter" not in manager.states and "graph_filter" not in manager.panels
    assert manager.active_id != "graph_filter" and editor.find_token("tool:graph_filter") is None
    assert manager.states["outline"].visible and window.application.panels.outline.isVisible()
    assert len(editor.groups) == 2
    for view in (first, second):
        group, index = editor.find_token(view.id)
        assert group.group_id == groups[view.id] and group.widget(index) is view.widget
        assert view.canvas.current_page_kind == "graph"


def test_graph_save_restart_remove_undo_and_owner_deletion(window, qtbot, tmp_path, monkeypatch):
    view = window.documents.active
    owner = view.engine.add_child(view.engine.root_id, "附图入口")
    view.canvas.select_nodes([owner])
    canvas = _create_graph(window)
    canvas.view.zoom(1.4)
    assert view.document.save()
    assert "graph" in load_directory(view.document.path).nodes[owner].extension_pages
    state = canvas.capture_page_view_state()
    assert window.close()
    reopened = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(reopened, before_close_func=lambda item: item.dispose())
    reopened.show()
    canvas = reopened.application.canvas
    assert canvas.current_page_id == graph_page_id(owner)
    assert canvas.capture_page_view_state()["zoom"] == state["zoom"]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    canvas.action("graph_remove").trigger()
    assert canvas.graph is not None
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    canvas.action("graph_remove").trigger()
    assert canvas.current_page_kind == "main" and canvas.selected_node_ids() == [owner]
    canvas.action("undo").trigger()
    assert canvas.current_page_id == graph_page_id(owner)
    assert canvas.graph.edges == (GraphEdge("a", "b"),)
    canvas.engine.remove_nodes([owner])
    assert canvas.current_page_kind == "main"
    assert canvas.engine.page_summary(graph_page_id(owner)) is None
    canvas.action("undo").trigger()
    assert canvas.engine.node_graph(owner).edges == (GraphEdge("a", "b"),)


def test_graph_filter_is_ignored_and_cleared_when_graph_is_saved(window, tmp_path):
    """打开和编辑目录中的图，旧筛选字段不影响显示，图内容提交时清空字段。"""
    view = window.documents.active
    owner = view.engine.root_id
    _create_graph(window, THREE_NODES + "\na -> b\nb -> c\na -> a")
    assert view.document.save()
    main = view.document.path / "pages" / "main.json"
    payload = json.loads(main.read_text(encoding="utf-8"))
    graph_record = payload["nodes"][0]["extension_pages"]["graph"]
    graph_record["version"] = 1
    graph_data = graph_record["data"]
    for node in graph_data["nodes"]:
        node.pop("text_format")
    graph_data["hidden_node_ids"] = ["a"]
    graph_data["positions"] = {"a": [72, 24]}
    legacy_path = tmp_path / "旧版图"
    save_directory(view.engine.document, legacy_path)
    page = legacy_path / "pages" / "main.json"
    page.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert window.open_path(legacy_path)
    opened = window.documents.active
    canvas = opened.canvas
    canvas.open_node_graph(owner)
    assert set(canvas.branch_items) == {"a->b", "b->c", "a->a"}
    assert canvas.graph.positions == {"a": (72, 24)}
    assert not opened.engine.is_dirty and not opened.engine.can_undo
    target = tmp_path / "图.png"
    canvas.export(target)
    baseline = QImage(str(target))
    assert not baseline.isNull()
    canvas.select_nodes(["a"])
    canvas.export(target)
    assert QImage(str(target)) == baseline
    canvas.view.graph_edge_edit.select_edge(GraphEdge("a", "b"))
    canvas.export(target)
    assert QImage(str(target)) == baseline

    assert json.loads(page.read_text(encoding="utf-8")) == payload
    opened.engine.update_node(owner, text="更新标题")
    assert window.documents.save(opened)
    saved = json.loads(page.read_text(encoding="utf-8"))
    saved_graph = saved["nodes"][0]["extension_pages"]["graph"]
    assert saved_graph["version"] == 1
    assert saved_graph["data"] == {**graph_data, "hidden_node_ids": []}
    assert load_directory(legacy_path).page_view(graph_page_id(owner)).graph == canvas.graph
    assert not opened.engine.is_dirty and opened.engine.can_undo


def test_graph_node_pages_use_menu_icons_navigation_and_shared_titles(window, qtbot):
    """从产品菜单和图标创建及打开三类附页，验证最近访问和父图中的局部选择。"""
    canvas = _create_graph(window, '节点 a "数据库"\n节点 b "数据库"\na -> b')
    engine = canvas.engine
    owner = engine.root_id
    parent = canvas.current_page_id
    canvas.select_nodes(["a"])
    keys = ("create_detail_page", "create_submap", "node_graph")
    errors = []
    canvas.error_occurred.connect(errors.append)
    menu = QMenu()
    window.application.populate_edit_menu(menu)
    pages = next(action.menu() for action in menu.actions() if action.text() == "页面")
    assert all(canvas.action(key) in pages.actions() and canvas.action(key).isEnabled() for key in keys)
    assert set(keys) <= set(dict(canvas._context_menu_groups(False))["页面"])
    pages.actions()[0].trigger()

    assert not errors
    detail = canvas.current_page_id
    assert canvas.current_page_kind == "detail"
    canvas.return_to_parent_page()
    assert canvas.current_page_id == parent and canvas.selected_node_ids() == ["a"]
    canvas.action("create_submap").trigger()
    assert not errors
    submap = canvas.current_page_id
    assert canvas.current_page_kind == "submap"
    root = canvas.document_view.root_id
    canvas.enter_detail_page(root)
    assert canvas.current_page_id == detail
    canvas.return_to_parent_page()
    assert canvas.current_page_id == submap and canvas.selected_node_ids() == [root]
    engine.update_node(root, text="数据库设计")
    canvas.return_to_parent_page()
    assert canvas.current_page_id == parent and canvas.selected_node_ids() == ["a"]
    assert canvas.document_view.nodes["a"].text == "数据库设计"
    assert canvas.document_view.nodes["b"].text == "数据库"
    assert canvas.graph.edges == (GraphEdge("a", "b"),)
    canvas.action("node_graph").trigger()
    assert not errors
    nested = canvas.current_page_id
    anchor = engine.graph_node_document_id(owner, "a")
    assert nested == graph_page_id(anchor)
    canvas.apply_graph_text('节点 a "索引"')
    canvas.select_nodes(["a"])
    inner_detail = canvas.create_detail_page()
    assert canvas.path_navigation.full_path.endswith(" · 图 › 数据库设计 · 图 › 索引 · 详情")
    canvas.return_to_parent_page()
    assert canvas.current_page_id == nested and canvas.selected_node_ids() == ["a"]
    canvas.return_to_parent_page()
    assert canvas.current_page_id == parent and canvas.selected_node_ids() == ["a"]
    assert canvas.node_items["a"].visual.page_kinds == ("detail", "submap", "graph")

    canvas.set_editable(False)
    assert not canvas.action("delete_detail_page").isEnabled()
    for field, target in (("detail_page_control", detail), ("submap_control", submap), ("graph_control", nested)):
        canvas.fit_to_view()
        control = getattr(canvas.node_items["a"], field)
        point = canvas.view.mapFromScene(control.sceneBoundingRect().center())
        qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert canvas.current_page_id == target
        canvas.path_navigation.back_button.click()
        assert canvas.current_page_id == parent and canvas.selected_node_ids() == ["a"]
    window.documents.active._navigate_recent_page(inner_detail, None)
    assert canvas.current_page_id == inner_detail
    canvas.return_to_parent_page()
    assert canvas.current_page_id == nested and canvas.selected_node_ids() == ["a"]
    saved_view = canvas.capture_page_view_state()
    canvas.navigate_to(None)
    canvas.restore_page_view_state(saved_view)
    assert canvas.current_page_id == nested and canvas.selected_node_ids() == ["a"]
    canvas.navigate_to(submap)
    assert canvas.current_page_id == submap
    canvas.set_editable(True)
    canvas.set_submap_favorite(submap, True)
    canvas.return_to_parent_page()
    canvas.select_nodes(["b"])
    other_submap = canvas.create_submap()
    canvas.set_submap_favorite(other_submap, True)
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [submap, other_submap]
    window.documents.active._navigate_favorite(submap)
    assert canvas.current_page_id == submap
    assert window.documents.save(window.documents.active)
    reopened = MindMapEngine(load_directory(window.documents.active.document.path))
    assert reopened.page_summary(inner_detail).path == engine.page_summary(inner_detail).path
    canvas.return_to_parent_page()
    canvas.fit_to_view()
    start = _outlet_point(canvas, "b")
    detail_control = canvas.node_items["a"].detail_page_control
    end = canvas.view.mapFromScene(detail_control.sceneBoundingRect().center())
    _drag_edge(canvas, start, end)
    assert canvas.current_page_id == parent and GraphEdge("b", "a") in canvas.graph.edges


def test_graph_owned_page_deletion_and_text_replacement_sync_split_views(window, monkeypatch):
    """确认取消、候选拦截、删除及撤销都保持图节点附页和分屏归属一致。"""
    first = window.documents.active
    canvas = _create_graph(window)
    engine = canvas.engine
    graph_page = canvas.current_page_id
    canvas.select_nodes(["a"])
    canvas.begin_edit("a")
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    with pytest.raises(MindMapError, match="候选"):
        canvas.create_detail_page()
    assert canvas.current_page_id == graph_page and canvas.is_composing
    canvas.finish_edit(commit=False)
    assert first.duplicate()
    other = window.documents.active
    window.host.editor.split_token(other.id, Qt.Orientation.Horizontal)
    other.canvas.select_nodes(["a"])
    canvas.begin_edit("a")
    editor = canvas.editor
    editor.setPlainText("其他标签中的草稿")
    detail = other.canvas.create_detail_page()
    assert other.canvas.current_page_id == detail and canvas.current_page_id == graph_page
    assert canvas.editor is editor and editor.toPlainText() == "其他标签中的草稿"
    assert canvas.node_items["a"].detail_page_control.isVisible()
    canvas.finish_edit(commit=False)
    other.canvas.return_to_parent_page()
    other.canvas.select_nodes(["a"])
    canvas.navigate_to(detail)
    baseline = engine.document
    revision = engine.revision
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    assert other.canvas.apply_graph_text('节点 b "服务"') is False
    other.canvas.delete_selected()
    assert engine.document == baseline and engine.revision == revision
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: (
        prompts.append(args[2]) or QMessageBox.StandardButton.Yes))
    assert other.canvas.apply_graph_text('节点 b "服务"') is True
    assert "1 张详情页" in prompts[0]
    assert engine.revision == revision + 1 and engine.page_summary(detail) is None
    assert canvas.current_page_id == graph_page and other.canvas.current_page_id == graph_page
    assert set(other.canvas.document_view.nodes) == {"b"}
    other.canvas.action("undo").trigger()
    assert engine.document == baseline
    assert canvas.node_items["a"].detail_page_control.isVisible()
    other.canvas.action("redo").trigger()
    assert engine.page_summary(detail) is None
    other.canvas.action("undo").trigger()
    other.canvas.action("graph_remove").trigger()
    assert engine.page_summary(graph_page) is None and engine.page_summary(detail) is None
    other.canvas.action("undo").trigger()
    assert engine.document == baseline and engine.page_summary(detail) is not None


def test_graph_page_deletion_falls_back_to_the_owning_idea(window):
    """从最近访问直达深层附页时，删除上级图后回到实际所属想法。"""
    canvas = window.application.canvas
    engine = canvas.engine
    other_idea = engine.add_inspiration("其他想法", position=(0, 0))
    idea = engine.add_inspiration("所属想法", position=(200, 0))
    owner = engine.add_child(idea, "图入口")
    engine.set_node_graph(owner, parse_graph_text(SAMPLE))
    detail = engine.create_graph_node_page(owner, "a", kind="detail")
    canvas.show_inspiration(other_idea)
    window.documents.active._navigate_recent_page(detail, None)
    assert canvas.current_page_id == detail
    engine.remove_node_graph(owner)
    assert canvas.current_page_id == engine.inspiration_id
    assert canvas.current_inspiration_root_id == idea


def test_graph_clipboard_across_files_and_legacy_detail_restrictions(window, tmp_path, monkeypatch):
    """跨文件剪切粘贴保留附页，旧详情图及向详情粘贴沿用限制。"""
    canvas = _create_graph(window)
    source = canvas.engine
    canvas.select_nodes(["a"])
    detail = canvas.create_detail_page()
    root = canvas.document_view.root_id
    source.add_child(root, "附页正文")
    canvas.return_to_parent_page()
    canvas.select_nodes(["a", "b"])
    canvas.cut()
    assert not canvas.graph.nodes and source.page_summary(detail) is None
    assert "a -> b" in QApplication.clipboard().text()
    canvas.action("undo").trigger()
    assert source.page_summary(detail) is not None

    assert window.new_document(tmp_path / "目标")
    target = _create_graph(window, None)
    first, second = target.paste()
    assert target.graph.edges == (GraphEdge(first, second),)
    target.enter_detail_page(first)
    assert target.current_page_id != detail
    assert any(node.text == "附页正文" for node in target.document_view.nodes.values())

    target.return_to_parent_page()
    target.return_to_parent_page()
    engine = target.engine
    detail = engine.create_detail_page(engine.root_id)
    detail_root = engine.page_document(detail).root_id
    graph_owner = engine.add_child(engine.root_id, "待复制分支")
    engine.set_node_graph(graph_owner, NodeGraph())
    clipboard = QApplication.clipboard()
    graph_payload = bytes(clipboard.mimeData().data("application/x-python-mindmap-graph+json"))
    graph_text = clipboard.text()
    target.select_nodes([graph_owner])
    target.copy()
    target.navigate_to(detail)
    assert not target.action("node_graph").isVisible()
    with pytest.raises(MindMapError, match="详情页"):
        engine.set_node_graph(detail_root, NodeGraph())
    prompts = []
    original = bytes(clipboard.mimeData().data("application/x-python-mindmap+json"))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: (
        prompts.append(args[2]) or QMessageBox.StandardButton.Yes))
    pasted, = target.paste()
    assert "1 张图" in prompts[0] and not engine.get_node(pasted).extension_pages
    assert bytes(clipboard.mimeData().data("application/x-python-mindmap+json")) == original

    # 构造由旧版本写出的详情页图，作为保存、加载和继续编辑的兼容数据。
    detail_engine = MindMapEngine()
    legacy_detail = detail_engine.create_detail_page(detail_engine.root_id)
    legacy_owner = detail_engine.page_document(legacy_detail).root_id
    legacy = detail_engine.document
    legacy.nodes[legacy_owner].extension_pages["graph"] = NodeGraph().to_record()
    path = tmp_path / "旧文件"
    save_directory(legacy, path)
    assert window.open_path(path)
    reopened = window.application.canvas
    reopened.navigate_to(legacy_detail)
    reopened.open_node_graph(legacy_owner)
    reopened.apply_graph_text(SAMPLE)
    reopened.select_nodes(["a"])
    assert all(not reopened.action(key).isVisible() for key in ("create_detail_page", "create_submap", "node_graph"))
    with pytest.raises(MindMapError, match="详情页"):
        reopened.create_submap()
    mime = QMimeData()
    mime.setData("application/x-python-mindmap-graph+json", graph_payload)
    mime.setText(graph_text)
    clipboard.setMimeData(mime)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    added = reopened.paste()
    assert len(added) == 2 and all(node.document_node_id is None for node in reopened.graph.nodes)
    assert window.documents.save(window.documents.active)
    assert load_directory(path).page_view(graph_page_id(legacy_owner)).graph == reopened.graph
