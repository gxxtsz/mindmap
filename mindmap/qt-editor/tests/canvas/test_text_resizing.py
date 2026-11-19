"""验证两类画布中的正文格式、实景拖动、分屏和正式导出。"""

import json

import pytest
from mindmap_core import GraphEdge, GraphNode, MindMapEngine, NodeGraph
from mindmap_qt_editor.canvas import MindMapWidget
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture
def make_canvas(qtbot):
    widgets = []

    def create(engine, *, graph=False, visible=True):
        widget = MindMapWidget(engine, toolbar=False)
        qtbot.addWidget(widget)
        widgets.append(widget)
        widget.resize(1050, 720)
        if graph:
            widget.open_node_graph(engine.root_id)
        if visible:
            widget.show()
            widget.activateWindow()
            widget.view.setFocus()
            QApplication.processEvents()
        return widget

    yield create

    for widget in widgets:
        widget.dispose()


def mouse(view, kind, point):
    """向真实视口发送带明确左键状态的事件，覆盖画布的输入路由。"""
    move = kind == QEvent.Type.MouseMove
    release = kind == QEvent.Type.MouseButtonRelease
    left, no_button = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    event = QMouseEvent(kind, QPointF(point), QPointF(view.viewport().mapToGlobal(point)),
                        no_button if move else left, no_button if release else left,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)


@pytest.mark.parametrize("graph", [False, True])
@pytest.mark.parametrize(("zoom", "delta"), [(0.5, 130), (2, -90)])
def test_drag_previews_and_commits_width_once(make_canvas, graph, zoom, delta, tmp_path):
    engine = MindMapEngine()
    source = "**短文字**" if delta > 0 else "**" + "长正文随宽度重新换行" * 12 + "**"
    if graph:
        key = "a"
        positions = {"a": (60, 60), "b": (520, 250)}
        nodes = (GraphNode("a", source, text_format="markdown"), GraphNode("b", "下游"))
        engine.set_node_graph(engine.root_id, NodeGraph(nodes, (GraphEdge("a", "b"),), positions))
    else:
        key = engine.add_child(engine.root_id, source)
        engine.update_node(key, style={"width": 260})
        engine.add_child(key, "后续")
    engine.replace_document(engine.document)
    mirror = make_canvas(engine, graph=graph, visible=False)
    widget = make_canvas(engine, graph=graph)
    mirror.shared_views = widget.shared_views = True
    view = widget.view
    widget.select_nodes([key])
    view.zoom(zoom)
    item = widget.node_items[key]
    view.centerOn(item)
    mouse_position = view.text_resize.decoration_bounds().center()
    end = mouse_position + QPoint(round(delta * zoom), 0)
    assert view.text_resize.hit_test(mouse_position)
    view.update_cursor(mouse_position)
    assert view.viewport().cursor().shape() == Qt.CursorShape.SizeHorCursor
    if graph:
        view.update_cursor(view.mapFromScene(item.sceneBoundingRect().center()))
        outlet = view.graph_edge_edit._outlet()
        assert outlet is not None and not view.text_resize.hit_test(outlet.center.toPoint())
    else:
        fold = item.fold_control
        fold_center = view.mapFromScene(fold.mapToScene(fold.button_rect().center()))
        assert fold.contains_button(view.mapToScene(fold_center)) and not view.text_resize.hit_test(fold_center)

    output = tmp_path / "导出.png"
    widget.export(output)
    original_export = QImage(str(output))
    original, revision, layout = engine.document, engine.revision, widget.layout_result
    visual, mirror_visual = item.visual, mirror._visuals[key]
    mouse(view, QEvent.Type.MouseButtonPress, mouse_position)
    mouse(view, QEvent.Type.MouseMove, end)
    assert view.text_resize.active and not view.node_drag.active
    preview = view.text_resize._preview
    assert preview is not None and preview.width == pytest.approx(round(visual.width - 28 + delta) + 28, abs=1)
    if delta < 0:
        assert preview.height > visual.height
    assert engine.revision == revision and engine.document == original and widget.layout_result is layout
    assert item.visual is visual and mirror._visuals[key] is mirror_visual
    widget.export(output)
    assert QImage(str(output)) == original_export

    mouse(view, QEvent.Type.MouseButtonRelease, end)
    assert not view.text_resize.active and item.opacity() == 1
    assert engine.revision == revision + 1 and widget.selected_node_ids() == [key]
    assert widget._visuals[key].width == mirror._visuals[key].width == preview.width
    assert widget.layout_result.branches != layout.branches
    if graph:
        assert widget.graph.positions == positions
    widget.export(output)
    assert QImage(str(output)) != original_export
    assert engine.undo() and engine.document == original and not engine.can_undo
    assert engine.redo() and widget._visuals[key].width == preview.width
    widget.action("reset_node_width").trigger()
    assert engine.document == original and widget._visuals[key].width == visual.width


@pytest.mark.parametrize("reason", ["click", "escape", "capture", "page", "change", "readonly"])
def test_width_gesture_leaves_no_history_when_cancelled(make_canvas, qtbot, reason):
    engine = MindMapEngine()
    key = engine.add_child(engine.root_id, "待调宽")
    page = engine.create_submap(engine.add_child(engine.root_id, "子导图"))
    engine.replace_document(engine.document)
    widget = make_canvas(engine)
    widget.select_nodes([key])
    view = widget.view
    start = view.text_resize.decoration_bounds().center()
    end = start + QPoint(140, 0)
    revision = engine.revision
    mouse(view, QEvent.Type.MouseButtonPress, start)
    assert view.text_resize.active
    if reason == "click":
        mouse(view, QEvent.Type.MouseButtonRelease, start)
    else:
        mouse(view, QEvent.Type.MouseMove, end)
        assert view.text_resize._preview is not None
        if reason == "escape":
            qtbot.keyClick(view, Qt.Key.Key_Escape)
        elif reason == "capture":
            QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.UngrabMouse))
        elif reason == "page":
            widget.navigate_to(page)
        elif reason == "change":
            engine.update_node(key, note="来自其他操作的提交")
            revision += 1
        elif reason == "readonly":
            widget.set_editable(False)
            assert view.text_resize.decoration_bounds().isEmpty()
        mouse(view, QEvent.Type.MouseButtonRelease, end)
    assert not view.text_resize.active and "fixed_width" not in engine.get_node(key).style
    assert engine.revision == revision


@pytest.mark.parametrize("graph", [False, True])
def test_new_markdown_nodes_edit_source_and_toggle_format(make_canvas, graph):
    engine = MindMapEngine()
    if graph:
        engine.set_node_graph(engine.root_id, NodeGraph((GraphNode("old", "**旧文字**", fixed_width=170),)))
    widget = make_canvas(engine, graph=graph)
    source = "# 标题\n\n**粗体**\n\n- 列表"
    if graph:
        assert widget._visuals["old"].text.toPlainText() == "**旧文字**"
        assert widget.apply_graph_text('节点 old "**旧文字**"\n节点 new ' + json.dumps(source))
        assert widget.graph.nodes[0] == GraphNode("old", "**旧文字**", fixed_width=170)
        key = "new"
    else:
        key = engine.add_child(engine.root_id, source)
    widget.select_nodes([key])
    assert widget._document.nodes[key].text_format == "markdown"
    assert widget._visuals[key].text.toPlainText() == "标题\n粗体\n列表"
    widget.action("edit").trigger()
    assert widget.editor.toPlainText() == source
    widget.editor.setPlainText(source + "\n- 下一行")
    widget.finish_edit(commit=True)
    source += "\n- 下一行"
    assert widget._visuals[key].text.toPlainText().endswith("列表\n下一行")
    widget.action("toggle_text_format").trigger()
    assert widget._document.nodes[key].text_format == "plain"
    assert widget._visuals[key].text.toPlainText() == source
    assert engine.undo() and widget._document.nodes[key].text_format == "markdown"

    mirror = make_canvas(engine, graph=graph, visible=False)
    mirror.shared_views = widget.shared_views = True
    mirror.begin_edit(key)
    mirror.editor.setPlainText("未提交的草稿")
    widget.call(lambda: widget._set_node_width(key, 190))
    assert mirror.editor.toPlainText() == "未提交的草稿" and mirror.editor.textWidth() == 190

    widget.copy()
    if not graph:
        widget.select_nodes([engine.root_id])
    copied, = widget.paste()
    node = widget._document.nodes[copied]
    assert node.text == source and node.text_format == "markdown" and node.style["fixed_width"] == 190
