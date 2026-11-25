"""通过实际画布视口覆盖附件拖放、系统打开和失败后的文档一致性。"""

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from mindmap_core import GraphEdge, GraphNode, MindMapEngine, MindMapError, NodeGraph
from mindmap_core.formats import directory, json_codec, load_directory, save_directory
from mindmap_core.formats.graph import format_graph_text
from mindmap_core.models import SVG_MEDIA_TYPE
from mindmap_qt_editor.canvas import MindMapWidget
from mindmap_qt_editor.canvas import attachments as attachment_actions
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QImage,
    QMouseEvent,
    QPainter,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMessageBox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 48">'
    b'<path fill="#123abc" d="M-12 -12 H108 V0 H-12 Z"/>'
    b'<path fill="#123abc" d="M8 4 H88 V44 H8 Z"/></svg>'
)


@pytest.fixture
def canvas(qtbot):
    widget = MindMapWidget(toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(820, 570)
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.dispose()


def local_files(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def send_drag(widget, mime, position, event_type):
    coordinate = QPointF(position) if event_type is QDropEvent else position
    event = event_type(coordinate, Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    QApplication.sendEvent(widget.view.viewport(), event)
    return event


def node_point(widget, key):
    QApplication.processEvents()
    item = widget.node_items[key]
    widget.view.centerOn(item)
    return widget.view.mapFromScene(item.mapToScene(item.visual.card_rect().center()))


def drop(widget, key, *paths):
    point = node_point(widget, key)
    mime = local_files(*paths)
    assert send_drag(widget, mime, point, QDragEnterEvent).isAccepted()
    assert widget.view._attachment_drop_node == key
    event = send_drag(widget, mime, point, QDropEvent)
    assert event.isAccepted() and event.dropAction() == Qt.DropAction.CopyAction
    assert widget.view._attachment_drop_node is None


@pytest.mark.parametrize("page", ["main", "submap", "detail", "inspiration", "graph", "image"])
def test_drop_and_click_attachment_on_each_node_kind(canvas, qtbot, tmp_path, monkeypatch, page):
    engine = canvas.engine
    key = engine.root_id
    if page == "inspiration":
        key = engine.add_inspiration("想法", position=(0, 0))
        canvas.show_inspiration(key)
    elif page == "graph":
        canvas.open_node_graph(key, create=True)
        key = canvas.add_graph_node()
    elif page in {"submap", "detail"}:
        owner = engine.add_child(key, "入口")
        page_id = engine.create_submap(owner) if page == "submap" else engine.create_detail_page(owner)
        canvas.navigate_to(page_id)
        key = canvas.document_view.root_id
    elif page == "image":
        image_path = tmp_path / "图片.png"
        image = QImage(48, 32, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        assert image.save(str(image_path))
        key = engine.add_image_child(key, image_path.read_bytes(), width=48)
    else:
        key = engine.add_child(key, "接收节点")

    source = tmp_path / "说明 书 #1.txt"
    contents = "附件内容".encode("utf-8")
    source.write_bytes(contents)
    drop(canvas, key, source)
    control = canvas.node_items[key].attachment_control
    assert control.isVisible() and control.toolTip() == "正在导入附件…"
    assert not control.can_activate()
    assert canvas.document_view.nodes[key].attachment is None
    qtbot.waitUntil(lambda: canvas.document_view.nodes[key].attachment is not None)
    assert source.read_bytes() == contents
    source.unlink()

    opened = []

    def open_url(url):
        assert url.isLocalFile()
        path = Path(url.toLocalFile())
        opened.append((path.name, path.read_bytes()))
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)
    monkeypatch.setattr(attachment_actions, "_opened_attachment_directories", [])
    canvas.set_editable(False)
    assert canvas.node_items[key].attachment_control is control
    assert control.isVisible() and control.toolTip() == source.name
    click = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=click)
    assert opened == [(source.name, contents)]


def test_pending_paperclip_is_visible_during_read_and_clears_on_failure(canvas, qtbot, tmp_path, monkeypatch):
    engine = canvas.engine
    key = engine.root_id
    engine.add_child(key, "子节点")
    source = tmp_path / "附件.txt"
    source.write_bytes(b"incoming")
    future = Future()
    monkeypatch.setattr(attachment_actions, "_load_pool", lambda: SimpleNamespace(submit=lambda *_: future))
    errors, opened = [], []
    canvas.error_occurred.connect(errors.append)
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    layout, revision = canvas.layout_result, engine.revision

    drop(canvas, key, source)
    item = canvas.node_items[key]
    control = item.attachment_control
    assert control.isVisible() and not control.can_activate()
    assert not control.boundingRect().intersects(item.fold_control.button_rect())
    assert canvas.layout_result is layout and engine.revision == revision
    qtbot.waitUntil(lambda: canvas._pending_attachment.prepared)
    assert control.isVisible()
    position = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
    assert canvas.view.itemAt(position) is control
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert not opened and not errors

    future.set_exception(MindMapError("读取附件失败"))
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert not control.isVisible() and errors == ["读取附件失败"]
    assert engine.revision == revision and not engine.document.assets


@pytest.mark.parametrize("graph", [False, True])
def test_confirm_replacement_and_remove_from_node_menu(canvas, qtbot, tmp_path, monkeypatch, graph):
    key = canvas.engine.root_id
    if graph:
        canvas.open_node_graph(key, create=True)
        key = canvas.add_graph_node()
    first, second = tmp_path / "旧附件.txt", tmp_path / "新附件.pdf"
    first.write_bytes(b"old")
    second.write_bytes(b"new")
    drop(canvas, key, first)
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    original = canvas.document_view.nodes[key].attachment
    revision = canvas.engine.revision
    prompts = []
    choice = QMessageBox.StandardButton.No

    def confirm(_widget, _title, message, *_args):
        prompts.append(message)
        return choice

    monkeypatch.setattr(QMessageBox, "question", confirm)
    drop(canvas, key, second)
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert first.name in prompts[0] and second.name in prompts[0]
    assert canvas.engine.revision == revision
    assert canvas.document_view.nodes[key].attachment == original

    choice = QMessageBox.StandardButton.Yes
    drop(canvas, key, second)
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert canvas.node_items[key].attachment_control.toolTip() == second.name
    assert canvas.engine.revision == revision + 1
    canvas.select_nodes([key])
    assert any("remove_attachment" in keys for _, keys in canvas._context_menu_groups(False))
    action = canvas.action("remove_attachment")
    assert action.isVisible() and action.isEnabled()
    action.trigger()
    assert canvas.document_view.nodes[key].attachment is None
    assert canvas.engine.undo()
    assert canvas.document_view.nodes[key].attachment.name == second.name
    assert canvas.engine.undo()
    assert canvas.document_view.nodes[key].attachment == original
    assert canvas.engine.redo() and canvas.engine.redo()
    assert not canvas.engine.document.assets


def test_drag_to_blank_or_drop_multiple_files_or_readonly_is_ignored(canvas, tmp_path):
    key = canvas.engine.root_id
    file = tmp_path / "说明.txt"
    file.write_bytes(b"data")
    point = node_point(canvas, key)
    blank = QPoint(5, 5)
    assert canvas.view.itemAt(blank) is None
    mime = local_files(file)
    assert send_drag(canvas, mime, blank, QDragEnterEvent).isAccepted()
    assert send_drag(canvas, mime, point, QDragMoveEvent).isAccepted()
    assert canvas.view._attachment_drop_node == key
    assert not send_drag(canvas, mime, blank, QDragMoveEvent).isAccepted()
    assert canvas.view._attachment_drop_node is None
    assert not send_drag(canvas, mime, blank, QDropEvent).isAccepted()
    assert not send_drag(canvas, local_files(file, file), point, QDragEnterEvent).isAccepted()
    canvas.set_editable(False)
    assert not send_drag(canvas, mime, point, QDragEnterEvent).isAccepted()
    assert canvas.engine.get_node(key).attachment is None


@pytest.mark.parametrize("failure", ["missing", "capacity", "changed"])
def test_failed_attachment_import_keeps_existing_reference(canvas, qtbot, tmp_path, monkeypatch, failure):
    key = canvas.engine.root_id
    canvas.engine.set_node_attachment(key, ("原附件.txt", b"original"))
    previous = canvas.engine.get_node(key).attachment
    file = tmp_path / "新附件.txt"
    file.write_bytes(b"incoming")
    errors = []
    canvas.error_occurred.connect(errors.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    if failure == "missing":
        position = node_point(canvas, key)
        mime = local_files(file)
        assert send_drag(canvas, mime, position, QDragEnterEvent).isAccepted()
        file.unlink()
        assert send_drag(canvas, mime, position, QDropEvent).isAccepted()
    elif failure == "capacity":
        monkeypatch.setattr(directory, "MAX_ARCHIVE_BYTES", 1)
        drop(canvas, key, file)
    else:
        future = Future()
        monkeypatch.setattr(attachment_actions, "_load_pool", lambda: SimpleNamespace(submit=lambda *_: future))
        owner = canvas.engine.add_child(key, "另一页")
        page_id = canvas.engine.create_submap(owner)
        drop(canvas, key, file)
        canvas.navigate_to(page_id)
        future.set_result((file.name, b"incoming"))
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert len(errors) == 1
    assert {"missing": "无法读取", "capacity": "超过保存限制", "changed": "节点已失效"}[failure] in errors[0]
    assert canvas.engine.get_node(key).attachment == previous
    assert set(canvas.engine.document.assets) == {previous.asset_id}


def test_replacement_refuses_to_overwrite_attachment_changed_during_confirmation(
    canvas, qtbot, tmp_path, monkeypatch,
):
    engine = canvas.engine
    key = engine.root_id
    engine.set_node_attachment(key, ("原附件.txt", b"original"))
    source = tmp_path / "拖入的文件.txt"
    source.write_bytes(b"incoming")
    errors = []
    canvas.error_occurred.connect(errors.append)

    def confirm(*_):
        engine.set_node_attachment(key, ("另一视图提交的附件.txt", b"updated"))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    drop(canvas, key, source)
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert any("目标发生变化" in error for error in errors)
    attachment = engine.get_node(key).attachment
    assert attachment.name == "另一视图提交的附件.txt"
    assert set(engine.document.assets) == {attachment.asset_id}


def test_open_error_discards_temporary_copy(canvas, qtbot, monkeypatch):
    key = canvas.engine.root_id
    canvas.engine.set_node_attachment(key, ("附件.txt", b"data"))
    errors = []
    opened = []
    canvas.error_occurred.connect(errors.append)

    def reject(url):
        path = Path(url.toLocalFile())
        assert path.read_bytes() == b"data"
        opened.append(path)
        return False

    monkeypatch.setattr(QDesktopServices, "openUrl", reject)
    node_point(canvas, key)
    control = canvas.node_items[key].attachment_control
    point = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert opened and not opened[0].exists()
    assert errors == ["无法打开附件「附件.txt」"]


@pytest.mark.parametrize("graph", [False, True])
def test_copy_between_canvases_preserves_attachment_inside_binary_clipboard(canvas, qtbot, monkeypatch, graph):
    target = MindMapWidget(toolbar=False)
    qtbot.addWidget(target)
    owner = canvas.engine.root_id
    key = canvas.engine.add_child(owner, "复制节点") if not graph else None
    if graph:
        canvas.open_node_graph(owner, create=True)
        key = canvas.add_graph_node()
        target.open_node_graph(target.engine.root_id, create=True)
    data = b"file contents" * 128
    canvas.engine.set_node_attachment(key, ("附带文件.bin", data), graph_owner_id=owner if graph else None)
    canvas.select_nodes([key])
    monkeypatch.setattr(json_codec, "MAX_ARCHIVE_BYTES", 128)
    try:
        canvas.copy()
        [pasted] = target.paste()
        attachment = target.document_view.nodes[pasted].attachment
        assert attachment.name == "附带文件.bin"
        assert target.engine.document.assets[attachment.asset_id].data == data
    finally:
        target.dispose()


def test_svg_drop_creates_vector_child_and_saves_source(canvas, qtbot, tmp_path, monkeypatch):
    engine = canvas.engine
    root = engine.root_id
    existing = engine.add_child(root, "已有节点")
    engine.set_expanded(root, False)
    engine.set_node_attachment(root, ("说明.txt", b"description"))
    before, revision = engine.document, engine.revision
    file = tmp_path / "矢量 图 #1.SvG"
    file.write_bytes(SVG)
    future = Future()
    with monkeypatch.context() as patch:
        patch.setattr(attachment_actions, "_load_pool", lambda: SimpleNamespace(submit=lambda *_: future))
        drop(canvas, root, file)
        item = canvas.node_items[root]
        badge = item.svg_import_control
        assert badge.isVisible() and badge.toolTip() == "正在导入 SVG…" and not badge.can_activate()
        assert item.attachment_control.can_activate()
        assert engine.revision == revision
        future.set_result((file.name, SVG))
        qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert not badge.isVisible()
    key, = canvas.selected_node_ids()
    node = engine.get_node(key)
    assert node.is_image_node and node.image_width == 96
    assert engine.get_node(root).children == [existing, key] and engine.get_node(root).expanded
    assert engine.get_node(root).attachment == before.root.attachment
    assert engine.revision == revision + 1
    asset = engine.document.assets[node.image_id]
    assert asset.data == SVG and asset.media_type == SVG_MEDIA_TYPE
    visual = canvas.node_items[key].visual
    assert isinstance(visual.image, QSvgRenderer) and visual.image_rect == QRectF(0, 0, 96, 48)
    image = QImage(212, 116, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    visual.paint_image(painter, QRectF(10, 10, 192, 96))
    painter.end()
    assert image.pixelColor(5, 5).alpha() == image.pixelColor(10, 10).alpha() == 0
    assert image.pixelColor(100, 50).name() == "#123abc"

    for suffix in ("svg", "pdf", "png"):
        target = tmp_path / f"导出.{suffix}"
        canvas.export(target)
        data = target.read_bytes()
        if suffix == "svg":
            assert b"<path" in data and b"<clipPath" in data and b"#123abc" in data
            assert b"<image" not in data
        elif suffix == "pdf":
            assert data.startswith(b"%PDF")
        else:
            assert not QImage(str(target)).isNull()

    drop(canvas, root, file)
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    second, = canvas.selected_node_ids()
    assert second != key and engine.get_node(second).image_id == node.image_id
    assert engine.get_node(root).children == [existing, key, second] and len(engine.document.assets) == 2

    folder = tmp_path / "导图"
    save_directory(engine.document, folder)
    file.unlink()
    reopened = MindMapWidget(MindMapEngine(load_directory(folder)), toolbar=False)
    qtbot.addWidget(reopened)
    try:
        assert isinstance(reopened.node_items[key].visual.image, QSvgRenderer)
        assert (folder / "assets" / node.image_id).read_bytes() == SVG
        canvas.copy()
        reopened.select_nodes([reopened.engine.root_id])
        pasted, = reopened.paste()
        assert reopened.engine.get_node(pasted).image_id == node.image_id
        assert isinstance(reopened.node_items[pasted].visual.image, QSvgRenderer)
    finally:
        reopened.dispose()


@pytest.mark.parametrize("manual", [False, True])
def test_graph_svg_drop_uses_landing_node_and_preserves_resize_and_history(canvas, qtbot, tmp_path, manual):
    engine = canvas.engine
    owner = engine.root_id
    positions = {"start": (0, 0), "other": (0, 70)} if manual else {}
    graph = NodeGraph((GraphNode("start", "落点"), GraphNode("other", "另一个节点")), positions=positions)
    engine.set_node_graph(owner, graph)
    canvas.open_node_graph(owner)
    canvas.select_nodes(["other"])
    before, revision = engine.document, engine.revision
    file = tmp_path / "图.svg"
    file.write_bytes(SVG)

    drop(canvas, "start", file)
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    key, = canvas.selected_node_ids()
    current = canvas.graph
    assert current.edges == (GraphEdge("start", key),) and engine.revision == revision + 1
    item = canvas.node_items[key]
    assert item.node.is_image_node and isinstance(item.visual.image, QSvgRenderer)
    assert not canvas.action("edit").isEnabled()
    bounds = canvas.layout_result.nodes
    if manual:
        assert {node_id: current.positions[node_id] for node_id in positions} == positions
        assert bounds[key].y > bounds["other"].bottom
    else:
        assert current.positions == {} and bounds[key].y > bounds["start"].bottom
    assert canvas.layout_result.branches[current.edges[0].key].arrow
    assert engine.undo() and engine.document == before
    assert engine.redo() and canvas.selected_node_ids() == [key]

    # 已有图片在图文本往返时按局部标识保留。
    assert canvas.apply_graph_text(format_graph_text(canvas.graph))
    item = canvas.node_items[key]
    rect = item.mapRectToScene(item.visual.image_rect)
    start = canvas.view.mapFromScene(rect.bottomRight())
    end = start + QPoint(48, 24)
    viewport = canvas.view.viewport()
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    assert canvas.view.image_resize.active
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(viewport.mapToGlobal(end)),
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, move)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    visual = canvas.node_items[key].visual
    assert visual.width > 96 and visual.width / visual.height == pytest.approx(2)
    assert engine.undo() and canvas.node_items[key].node.image_width == 96

    folder = tmp_path / "导图"
    save_directory(engine.document, folder)
    file.unlink()
    reopened = MindMapWidget(MindMapEngine(load_directory(folder)), toolbar=False)
    qtbot.addWidget(reopened)
    try:
        reopened.validate_document(reopened.engine.document, defer_inactive_pages=True)
        reopened.open_node_graph(owner)
        assert reopened.graph == current and isinstance(reopened.node_items[key].visual.image, QSvgRenderer)
        canvas.copy()
        pasted, = reopened.paste()
        assert reopened.node_items[pasted].node.image_id == canvas.node_items[key].node.image_id
        assert len(reopened.engine.document.assets) == 1
        reopened.select_nodes([key, pasted])
        reopened.action("delete").trigger()
        assert not reopened.engine.document.assets and not reopened.graph.edges
    finally:
        reopened.dispose()


@pytest.mark.parametrize(("data", "message"), [
    (b"incomplete", "SVG 文件内容无效"),
    (b'<svg width="10000" height="10000"/>', "四千万"),
    ('<svg viewBox="0 0 10 10"><image href="图.png"/></svg>'.encode(), "请先将图像嵌入 SVG"),
    (SVG, "超过保存限制"),
])
def test_failed_svg_drop_leaves_no_node_or_asset(canvas, qtbot, tmp_path, monkeypatch, data, message):
    source = tmp_path / "导入.svg"
    source.write_bytes(data)
    if data == SVG:
        monkeypatch.setattr(directory, "MAX_ARCHIVE_BYTES", 1)
    engine = canvas.engine
    before, revision = engine.document, engine.revision
    errors = []
    canvas.error_occurred.connect(errors.append)
    drop(canvas, engine.root_id, source)
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert len(errors) == 1 and message in errors[0]
    assert engine.document == before and engine.revision == revision
    assert not canvas.node_items[engine.root_id].svg_import_control.isVisible()


@pytest.mark.parametrize("reason", ["切页", "删除落点", "只读"])
def test_svg_drop_is_cancelled_when_target_is_no_longer_editable(canvas, qtbot, tmp_path, monkeypatch, reason):
    engine = canvas.engine
    root = engine.root_id
    key = engine.add_child(root, "落点")
    destination = engine.create_submap(engine.add_child(root, "另一页"))
    file = tmp_path / "导入.svg"
    file.write_bytes(SVG)
    future = Future()
    assert future.set_running_or_notify_cancel()
    monkeypatch.setattr(attachment_actions, "_load_pool", lambda: SimpleNamespace(submit=lambda *_: future))
    drop(canvas, key, file)
    if reason == "切页":
        canvas.navigate_to(destination)
        canvas.navigate_to(None)
    elif reason == "删除落点":
        engine.remove_nodes([key])
    else:
        canvas.set_editable(False)
        canvas.set_editable(True)
    before, revision = engine.document, engine.revision
    future.set_result((file.name, SVG))
    qtbot.waitUntil(lambda: canvas._pending_attachment is None)
    assert engine.document == before and engine.revision == revision
