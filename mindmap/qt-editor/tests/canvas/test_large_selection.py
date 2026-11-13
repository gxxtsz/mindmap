"""大规模选择的稳定性能约束与交互回归；不依赖 CI 机器的耗时阈值。"""

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, Node
from mindmap_qt_editor.canvas import MindMapWidget
from mindmap_qt_editor.canvas.selection_decoration import _outline_paths, selection_bounds
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication


def make_document(count, wide=False):
    """直接建模生成五叉树或宽树，不把引擎逐节点提交混入结构断言。"""
    document = MindMapDocument.new("选择性能")
    keys = [document.root_id]
    for index in range(1, count):
        node = Node("普通主题")
        document.nodes[keys[0 if wide else (index - 1) // 5]].children.append(node.id)
        document.nodes[node.id] = node
        keys.append(node.id)
    return document, keys


@pytest.fixture
def create_canvas(qtbot):
    widgets = []

    def create(count=1101, wide=True):
        document, keys = make_document(count, wide)
        canvas = MindMapWidget(MindMapEngine(document), toolbar=False)
        widgets.append(canvas)
        qtbot.addWidget(canvas)
        canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        canvas.resize(900, 600)
        canvas.show()
        QApplication.processEvents()
        canvas.view.centerOn(QPointF(*canvas.layout_result.nodes[keys[-30]].center))
        canvas._viewport_changed()
        QApplication.processEvents()
        return canvas, keys

    yield create
    for canvas in widgets:
        canvas.finish_edit(commit=False)
        canvas.dispose()


@pytest.mark.parametrize("count", [1000, 3000, 10000])
@pytest.mark.parametrize("wide", [False, True])
def test_select_all_keeps_virtualization_and_notifies_once(create_canvas, count, wide, monkeypatch):
    """全选保留完整逻辑集合，图元仅跟随视口；重复全选不重复同步面板及动作。"""
    canvas, keys = create_canvas(count, wide)
    assert canvas._virtualized
    notifications = []
    canvas.selection_changed.connect(notifications.append)
    before = len(canvas.node_items)
    canvas.select_all_nodes()
    expected = list(canvas.document_view.ordered_nodes())
    assert canvas.selected_node_ids() == expected
    assert canvas.selection_count == count and canvas.is_node_selected(keys[-1])
    assert len(canvas.node_items) <= before + 1 < 200
    assert notifications == [expected]
    copied = canvas.selected_node_ids()
    copied.clear()
    assert canvas.selection_count == count
    cached = canvas._ordered_selection

    def forbidden(*args, **kwargs):
        raise AssertionError("重复全选不得重复同步动作或排序整批选择")

    monkeypatch.setattr(canvas, "_update_actions", forbidden)
    monkeypatch.setattr(type(canvas.document_view), "ordered_nodes", forbidden)
    canvas.select_all_nodes()
    assert canvas._ordered_selection is cached and notifications == [expected]


def test_pan_zoom_and_pointer_never_read_full_selection(create_canvas, monkeypatch):
    """交互热路径只读取计数、成员及屏内图元；外框路径在纯平移时复用。"""
    canvas, keys = create_canvas(10000)
    canvas.select_all_nodes()
    view = canvas.view
    QApplication.processEvents()
    view.viewport().grab()
    paths = _outline_paths.cache_info().misses
    ordered = canvas._ordered_selection
    queries = []
    viewport_query = canvas.layout_result.viewport

    def query(*args, **kwargs):
        queries.append(args)
        return viewport_query(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("视口交互不得重新读取、扫描或排序完整选择")

    with monkeypatch.context() as patch:
        patch.setattr(canvas, "selected_node_ids", forbidden)
        patch.setattr(canvas.scene, "selectedItems", forbidden)
        patch.setattr(type(canvas.document_view), "ordered_nodes", forbidden)
        patch.setattr(type(canvas.layout_result.geometry), "order", forbidden)
        patch.setattr(canvas.layout_result, "viewport", query)
        for index in range(3):
            bar = view.verticalScrollBar()
            bar.setValue(bar.value() + 5)
            QApplication.processEvents()
            view.viewport().grab()
        assert _outline_paths.cache_info().misses == paths
        queries.clear()
        view.zoom(1.1, anchor=view.viewport().rect().center())
        assert len(queries) == 1
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(5, 5), QPointF(5, 5),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), event)
        QApplication.processEvents()
        view.viewport().grab()
        assert len(view._selection_decoration.frame(view.devicePixelRatioF())) < 100
    assert canvas._ordered_selection is ordered and canvas.selection_count == len(keys)


def test_recycling_restores_highlight_without_selection_notification(create_canvas):
    canvas, keys = create_canvas()
    canvas.select_all_nodes()
    notifications = []
    canvas.selection_changed.connect(notifications.append)
    key = keys[20]
    for target in (key, keys[-30], key):
        canvas.view.centerOn(QPointF(*canvas.layout_result.nodes[target].center))
        canvas._viewport_changed()
        QApplication.processEvents()
        assert canvas.node_items[target].isSelected()
        assert len(canvas.node_items) < 100
        if target != key:
            assert key not in canvas.node_items and canvas.is_node_selected(key)
    assert not notifications and canvas.selection_count == len(keys)


def test_ctrl_toggle_click_and_clear_keep_logical_selection(create_canvas, qtbot):
    canvas, keys = create_canvas()
    canvas.select_all_nodes()
    view, key = canvas.view, keys[-30]
    point = view.mapFromScene(QPointF(*canvas.layout_result.nodes[key].center))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
    assert canvas.selection_count == len(keys) - 1 and not canvas.is_node_selected(key)
    assert canvas.is_node_selected(keys[20])
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
    assert canvas.selection_count == len(keys)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert canvas.selected_node_ids() == [key]
    canvas.select_all_nodes()
    canvas.scene.clearSelection()
    assert canvas.selection_count == 0
    canvas.select_all_nodes()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert canvas.selection_count == 0


def test_ctrl_deselect_last_resident_keeps_offscreen_selection(create_canvas, qtbot):
    """没有已选图元不等于没有逻辑选择，Ctrl 取消最后一个屏内节点不能清空屏外集合。"""
    canvas, keys = create_canvas()
    near, far = keys[-30], keys[20]
    canvas.select_nodes([near, far])
    # 树序首个操作节点可以常驻但仍在屏外；显式回收模拟只剩当前可见选择的场景。
    blocked = canvas.scene.blockSignals(True)
    canvas.scene.removeItem(canvas.node_items.pop(far))
    canvas._projected_selection.discard(far)
    canvas.scene.blockSignals(blocked)
    point = canvas.view.mapFromScene(QPointF(*canvas.layout_result.nodes[near].center))
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
    assert canvas.selected_node_ids() == [far]


@pytest.mark.parametrize("control", [False, True])
def test_marquee_preserves_existing_qt_rules_across_offscreen_selection(create_canvas, qtbot, control):
    canvas, keys = create_canvas()
    near, far = keys[-30], keys[20]
    canvas.select_nodes([far])
    view = canvas.view
    rect = canvas.layout_result.nodes[near]
    start = view.mapFromScene(QPointF(rect.x, rect.y)) - QPoint(12, 12)
    end = view.mapFromScene(QPointF(rect.right, rect.bottom)) + QPoint(12, 12)
    modifier = Qt.KeyboardModifier.ControlModifier if control else Qt.KeyboardModifier.NoModifier
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, modifier, start)
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(end), Qt.MouseButton.NoButton,
                        Qt.MouseButton.LeftButton, modifier)
    QApplication.sendEvent(view.viewport(), event)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, modifier, end)
    assert canvas.is_node_selected(near)
    assert canvas.is_node_selected(far) == control


def test_selection_snapshot_survives_external_add_and_history(create_canvas):
    canvas, keys = create_canvas()
    canvas.shared_views = True
    canvas.select_all_nodes()
    order = canvas._node_order
    canvas.engine.update_node(keys[-1], text="更新已有节点")
    assert canvas._node_order is order
    with canvas.engine.event_source("其他视图"):
        added = canvas.engine.add_child(keys[0], "后来新增")
    assert not canvas.is_node_selected(added) and canvas.selection_count == len(keys)
    canvas.shared_views = False
    canvas.select_nodes(keys[1:])
    fragment = canvas.engine.copy_fragment(canvas.selected_node_ids())
    assert set(keys[1:]).issubset(fragment.nodes) and added not in fragment.nodes
    canvas.engine.remove_nodes(canvas.selected_node_ids())
    assert canvas.selection_count == 1
    canvas.engine.undo()
    assert set(canvas.selected_node_ids()) == set(keys[1:])
    canvas.engine.redo()
    assert canvas.selection_count == 1


def test_large_selection_fold_and_page_history(create_canvas):
    """折叠过滤隐藏选择，撤销恢复完整选择；切页仅发送最终选择并恢复主图快照。"""
    canvas, keys = create_canvas(wide=False)
    canvas.select_all_nodes()
    before = canvas.selected_node_ids()
    hidden = set(canvas.document_view.descendants(keys[1])) - {keys[1]}
    canvas.engine.set_expanded(keys[1], False)
    assert set(canvas.selected_node_ids()) == set(before) - hidden
    canvas.engine.undo()
    assert canvas.selected_node_ids() == before
    canvas.engine.redo()
    assert set(canvas.selected_node_ids()) == set(before) - hidden
    canvas.engine.undo()
    page = canvas.engine.create_submap(keys[-1])
    canvas.select_all_nodes()
    notifications = []
    canvas.selection_changed.connect(notifications.append)
    canvas.navigate_to(page)
    assert len(notifications) == 1 and canvas.selection_count == 1
    canvas.navigate_to(None)
    assert canvas.selected_node_ids() == before and len(notifications) == 2
    assert len(canvas.node_items) < 200


def test_group_drag_keeps_nonresident_sources(create_canvas):
    canvas, keys = create_canvas()
    selected = keys[1:]
    canvas.select_nodes(selected)
    drag = canvas.view.node_drag
    origin = QPointF(*canvas.layout_result.nodes[keys[-30]].center)
    before = len(canvas.node_items)
    assert drag.begin(origin)
    assert set(drag._resolver.roots) == set(selected)
    assert len(canvas.node_items) == before < 100
    drag.cancel()
    assert canvas.selected_node_ids() == selected


def test_decoration_coalesces_updates_and_shares_visible_frame(create_canvas, monkeypatch):
    canvas, keys = create_canvas()
    canvas.select_all_nodes()
    QApplication.processEvents()
    view = canvas.view
    frames = []
    original = view._selection_decoration.frame

    def record(*args):
        frame = original(*args)
        frames.append(frame)
        return frame

    monkeypatch.setattr(view._selection_decoration, "frame", record)
    for _ in range(20):
        view._update_selection_decoration()
    assert not frames
    QApplication.processEvents()
    selection_bounds(view)
    view.viewport().grab()
    assert frames and all(frame is frames[0] for frame in frames)
