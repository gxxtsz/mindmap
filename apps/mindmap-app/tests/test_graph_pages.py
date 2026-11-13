"""从产品入口验证图与树共用画布、编辑命令和停靠侧栏。"""

import json
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from mindmap_app.app import build_window
from mindmap_core import GraphEdge, MindMapEngine, MindMapError, NodeGraph
from mindmap_core.formats import load_document, save_document
from mindmap_core.formats.graph import format_graph_text, parse_graph_text
from mindmap_core.node_pages import graph_page_id
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QInputMethodEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialogButtonBox,
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
    fresh = load_document(first.document.path).page_view(canvas.current_page_id).graph
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


def test_shift_edge_box_replaces_and_appends_without_changing_node_box(window, qtbot):
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
    assert canvas.selected_node_ids() == ["c"] and not controls.selected_edges
    assert canvas.engine.revision == revision


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


def test_edge_menu_rejects_entire_batch_after_another_view_removes_an_edge(window, qtbot):
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
    assert "graph" in load_document(view.document.path).nodes[owner].extension_pages
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


def test_legacy_graph_filter_is_ignored_and_cleared_on_clean_save(window, tmp_path):
    """从旧版原生容器打开、导出并显式保存，全部边可见且仍使用原文件版本。"""
    view = window.documents.active
    owner = view.engine.root_id
    _create_graph(window, THREE_NODES + "\na -> b\nb -> c\na -> a")
    assert view.document.save()
    with ZipFile(view.document.path) as archive:
        payload = json.loads(archive.read("document.json"))
    graph_data = payload["nodes"][0]["extension_pages"]["graph"]["data"]
    graph_data["hidden_node_ids"] = ["a"]
    graph_data["positions"] = {"a": [72, 24]}
    legacy_path = tmp_path / "旧版图.mindmap"
    with ZipFile(legacy_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("document.json", json.dumps(payload, ensure_ascii=False))

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

    with ZipFile(legacy_path) as archive:
        assert json.loads(archive.read("document.json")) == payload
    assert window.documents.save(opened)
    with ZipFile(legacy_path) as archive:
        saved = json.loads(archive.read("document.json"))
    saved_graph = saved["nodes"][0]["extension_pages"]["graph"]
    assert saved["schema_version"] == 7 and saved_graph["version"] == 1
    assert saved_graph["data"] == {**graph_data, "hidden_node_ids": []}
    assert load_document(legacy_path).page_view(graph_page_id(owner)).graph == canvas.graph
    assert not opened.engine.is_dirty and not opened.engine.can_undo


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
    reopened = MindMapEngine(load_document(window.documents.active.document.path))
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

    assert window.new_document(tmp_path / "目标.mindmap")
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
    path = tmp_path / "旧文件.mindmap"
    save_document(legacy, path)
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
    with ZipFile(path) as archive:
        assert json.loads(archive.read("document.json"))["schema_version"] == 7
