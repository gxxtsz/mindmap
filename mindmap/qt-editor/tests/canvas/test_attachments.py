"""通过实际画布视口覆盖附件拖放、系统打开和失败后的文档一致性。"""

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from mindmap_core.formats import directory, json_codec
from mindmap_qt_editor.canvas import MindMapWidget
from mindmap_qt_editor.canvas import attachments as attachment_actions
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication, QMessageBox


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
    control = canvas.node_items[key].attachment_control
    assert control.isVisible() and control.toolTip() == source.name
    click = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=click)
    assert opened == [(source.name, contents)]


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
