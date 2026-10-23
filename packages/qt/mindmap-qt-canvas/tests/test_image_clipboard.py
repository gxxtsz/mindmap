"""剪贴板图片识别、节点附图、编辑草稿及输入法边界的交互回归。"""

from weakref import ref

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_formats import encode_document, load_document, save_document
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.clipboard import (
    EDRAW_CLIPBOARD_MIME,
    has_clipboard_image,
    read_clipboard_image,
)
from mindmap_qt_canvas.widget import CLIPBOARD_MIME
from mindmap_qt_panels import MindMapPanels
from mindmap_qt_ui.dialogs import EdrawPasteOptions
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QPoint, Qt
from PySide6.QtGui import QColor, QImage, QInputMethodEvent, QPixmap
from PySide6.QtWidgets import QApplication, QMenu


def encoded(image, kind="PNG"):
    """把测试图像编码到独立内存，不读取或改写工作区图片。"""
    buffer = QBuffer()
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, kind)
    return bytes(buffer.data())


@pytest.fixture
def clipboard(qapp):
    """隔离剪贴板载荷，并确保其早于 QApplication 完成释放。"""
    clipboard = qapp.clipboard()
    original = QMimeData()
    previous = clipboard.mimeData()
    if previous is not None:
        for kind in previous.formats():
            original.setData(kind, previous.data(kind))
        if previous.hasImage():
            original.setImageData(previous.imageData())
    yield clipboard
    if qapp.platformName() == "offscreen":
        clipboard.clear()
    else:
        clipboard.setMimeData(original)


@pytest.fixture
def image(qapp):
    image = QImage(48, 32, QImage.Format.Format_ARGB32)
    image.fill(QColor("#e94665"))
    image.setPixelColor(0, 0, QColor(0, 0, 0, 0))
    image.setPixelColor(1, 0, QColor(255, 0, 255, 128))
    image.setDevicePixelRatio(2)
    return image


@pytest.fixture
def canvas(qtbot, clipboard):
    engine = MindMapEngine()
    first = engine.add_child(engine.root_id, "第一个节点")
    second = engine.add_child(engine.root_id, "第二个节点")
    engine.add_child(first, "已有后代")
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine)
    qtbot.addWidget(widget)
    widget.resize(960, 680)
    widget.show()
    widget.activateWindow()
    QApplication.processEvents()
    widget.view.setFocus()
    yield widget, first, second
    widget.finish_edit(commit=False)
    widget.dispose()


@pytest.mark.parametrize("source", ["qimage", "qpixmap", "png", "jpeg"])
def test_image_reader_accepts_bitmap_and_encoded_data(image, source):
    mime = QMimeData()
    if source in ("qimage", "qpixmap"):
        mime.setImageData(image if source == "qimage" else QPixmap.fromImage(image))
    else:
        original = encoded(image, source.upper())
        mime.setData("image/" + source, original)
        assert not mime.hasImage()
    assert has_clipboard_image(mime)
    data, media_type = read_clipboard_image(mime)
    decoded = QImage.fromData(data)
    assert decoded.size() == image.size()
    assert media_type == ("image/jpeg" if source == "jpeg" else "image/png")
    if source in ("png", "jpeg"):
        assert data == original
    if source != "jpeg":
        assert decoded.pixelColor(0, 0).alpha() == 0
        assert decoded.pixelColor(1, 0) == image.pixelColor(1, 0)


@pytest.mark.parametrize("fallback", ["prefer_png", "encoded", "qt_image"])
def test_image_representations_can_fallback_without_using_text(image, fallback):
    mime = QMimeData()
    mime.setText("不应该粘贴的说明")
    png = encoded(image)
    if fallback == "prefer_png":
        mime.setData("image/jpeg", encoded(image, "JPEG"))
        mime.setData("image/png", png)
    else:
        mime.setData("image/png", b"invalid image")
        if fallback == "encoded":
            mime.setData("image/bmp", png)
        else:
            mime.setImageData(image)
    data, media_type = read_clipboard_image(mime)
    assert media_type == "image/png"
    assert QImage.fromData(data).pixelColor(1, 0) == image.pixelColor(1, 0)
    if fallback != "qt_image":
        assert data == png


def test_text_and_image_addresses_are_not_read_as_images():
    mime = QMimeData()
    mime.setText("C:/图片.png\nhttps://example.com/image.png")
    mime.setHtml('<img src="https://example.com/image.png">')
    assert not has_clipboard_image(mime)
    assert read_clipboard_image(mime) is None


def test_image_fallback_does_not_keep_borrowed_mime_alive(image):
    """表示回退成功后立即释放借用对象，不把异常回溯及 Qt 载荷留给循环垃圾回收。"""
    mime = QMimeData()
    mime.setData("image/png", b"invalid image")
    mime.setImageData(image)
    reference = ref(mime)
    data, media_type = read_clipboard_image(mime)
    del mime
    assert reference() is None
    assert media_type == "image/png" and not QImage.fromData(data).isNull()


@pytest.mark.parametrize("source", ["qimage", "png"])
def test_image_reader_rejects_more_than_forty_million_pixels(qapp, source):
    # 单色图占用约 5 MB，仍能用真实图像覆盖像素上限，而不分配大型彩色位图。
    image = QImage(6400, 6251, QImage.Format.Format_Mono)
    image.fill(0)
    mime = QMimeData()
    if source == "qimage":
        mime.setImageData(image)
    else:
        mime.setData("image/png", encoded(image))
    with pytest.raises(MindMapError, match="四千万"):
        read_clipboard_image(mime)


@pytest.mark.parametrize("page_kind", ["main", "submap", "detail"])
@pytest.mark.parametrize("entry", ["shortcut", "action", "context"])
def test_paste_image_uses_existing_entries_on_every_page(
    canvas, clipboard, image, page_kind, entry, qtbot, monkeypatch,
):
    widget, first, second = canvas
    widget.select_nodes([second])
    if page_kind == "submap":
        widget.create_submap()
    elif page_kind == "detail":
        widget.create_detail_page()
    QApplication.processEvents()
    target = widget.current_node_id()
    page = widget.current_page_id
    widget.view.zoom(1.25)
    transform = widget.view.transform()
    selection = widget.selected_node_ids()
    before = widget.engine.document
    mime = QMimeData()
    mime.setImageData(image)
    mime.setText("图片说明不能变成新节点")
    clipboard.setMimeData(mime)
    imported = []
    widget.image_imported.connect(imported.append)
    if entry == "shortcut":
        widget.view.setFocus()
        qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    elif entry == "action":
        widget.action("paste").trigger()
    else:
        class PasteMenu(QMenu):
            def exec(self, position):
                assert widget.action("paste") in self.actions()
                widget.action("paste").trigger()
        monkeypatch.setattr("mindmap_qt_canvas.widget.QMenu", PasteMenu)
        widget.context_menu(QPoint(20, 20))
    document = widget.engine.document
    assert len(imported) == 1 and imported[0] != target
    created = imported[0]
    assert len(document.nodes) == len(before.nodes) + 1
    assert document.nodes[created].image_width == image.width()
    assert document.nodes[created].is_image_node and document.nodes[created].text == ""
    assert document.nodes[target].text == before.nodes[target].text
    assert document.nodes[target].children == [*before.nodes[target].children, created]
    assert document.nodes[target].style == before.nodes[target].style
    asset = document.assets[document.nodes[created].image_id]
    assert asset.media_type == "image/png"
    assert QImage.fromData(asset.data).pixelColor(1, 0) == image.pixelColor(1, 0)
    assert widget.current_page_id == page and widget.view.transform() == transform
    assert widget.selected_node_ids() == [created]
    assert widget.engine.undo() and widget.engine.document == before
    assert widget.selected_node_ids() == selection
    assert widget.engine.redo() and widget.engine.document == document
    assert widget.selected_node_ids() == [created]


@pytest.mark.parametrize("shared_old_image", [False, True])
def test_append_to_first_selected_node_preserves_old_images_and_deduplicates_resources(
    canvas, clipboard, image, shared_old_image,
):
    widget, first, second = canvas
    old_image = QImage(20, 20, QImage.Format.Format_RGB32)
    old_image.fill(Qt.GlobalColor.blue)
    old_asset = widget.engine.attach_image(first, encoded(old_image), width=91)
    if shared_old_image:
        widget.engine.attach_image(second, encoded(old_image))
    widget.engine.replace_document(widget.engine.document)
    widget.select_nodes([second, first])
    assert widget.selected_node_ids() == [first, second]
    before = widget.engine.document
    mime = QMimeData()
    mime.setData("image/png", encoded(image))
    clipboard.setMimeData(mime)
    created, = widget.paste()
    after = widget.engine.document
    new_asset = after.nodes[created].image_id
    assert new_asset != old_asset and after.nodes[created].image_width == image.width()
    assert after.nodes[first].image_id == old_asset and after.nodes[first].image_width == 91
    assert after.nodes[first].children == [*before.nodes[first].children, created]
    assert after.nodes[second] == before.nodes[second]
    assert old_asset in after.assets
    assert widget.selected_node_ids() == [created]
    assert widget.engine.undo() and widget.engine.document == before
    assert not widget.engine.can_undo
    assert widget.selected_node_ids() == [first, second]
    assert widget.engine.redo() and widget.engine.document == after
    widget.select_nodes([second])
    another, = widget.paste()
    assert another != created and widget.engine.parent_id(another) == second
    assert set(widget.engine.document.assets) == {new_asset, old_asset}
    assert widget.engine.get_node(another).image_id == new_asset


@pytest.mark.parametrize("temporary_focus", [False, True])
def test_paste_image_without_selection_uses_fallback_and_restores_selection_on_undo(
    canvas, clipboard, image, temporary_focus,
):
    widget, first, second = canvas
    target = widget.engine.root_id
    if temporary_focus:
        target = widget.engine.get_node(first).children[0]
        widget.engine.set_expanded(first, False)
        widget.navigate_to(None, target)
        assert widget.focus_id == target
    widget.select_nodes([])
    mime = QMimeData()
    mime.setImageData(image)
    clipboard.setMimeData(mime)
    created, = widget.paste()
    assert widget.selected_node_ids() == [created]
    assert widget.engine.parent_id(created) == target
    assert widget.engine.undo() and widget.selected_node_ids() == []
    assert widget.engine.redo() and widget.selected_node_ids() == [created]
    assert widget.focus_id == (target if temporary_focus else None)


def test_text_editor_pastes_image_after_committing_text_with_separate_undo(canvas, clipboard, image, qtbot):
    widget, first, second = canvas
    widget.begin_edit(first)
    widget.editor.setPlainText("已完成的文字草稿")
    mime = QMimeData()
    mime.setImageData(image)
    clipboard.setMimeData(mime)
    qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert widget.editor is None
    assert widget.engine.get_node(first).text == "已完成的文字草稿"
    created = widget.selected_node_ids()[0]
    assert widget.engine.get_node(created).is_image_node
    assert widget.engine.parent_id(created) == first
    assert widget.view.hasFocus()
    assert widget.engine.undo()
    assert widget.engine.get_node(first).text == "已完成的文字草稿"
    assert widget.engine.get_node(first).image_id is None
    assert created not in widget.engine.document.nodes
    assert widget.engine.undo() and widget.engine.get_node(first).text == "第一个节点"


def test_text_editor_keeps_normal_text_paste_and_other_controls_keep_their_paste(
    canvas, clipboard, image, qtbot,
):
    widget, first, second = canvas
    panels = MindMapPanels(widget)
    try:
        widget.begin_edit(first)
        mime = QMimeData()
        mime.setText("普通粘贴正文")
        clipboard.setMimeData(mime)
        before = widget.engine.document
        qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert widget.editor.toPlainText() == "普通粘贴正文"
        assert widget.engine.document == before
        widget.finish_edit(commit=False)
        widget.select_nodes([first])
        field = panels.properties.fields["style.font_family"].lineEdit()
        mime = QMimeData()
        mime.setImageData(image)
        mime.setText("Arial")
        clipboard.setMimeData(mime)
        field.selectAll()
        qtbot.keyClick(field, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert field.text() == "Arial"
        assert widget.engine.get_node(first).image_id is None
    finally:
        panels.dispose()


@pytest.mark.parametrize("source", ["broken", "null", "encode_failure"])
def test_invalid_image_preserves_editor_draft_and_never_falls_back_to_text(
    canvas, clipboard, image, source, monkeypatch, qtbot,
):
    widget, first, second = canvas
    widget.begin_edit(first)
    widget.editor.setPlainText("不能丢失的草稿")
    editor = widget.editor
    before, revision = widget.engine.document, widget.engine.revision
    mime = QMimeData()
    mime.setText("不能降级粘贴的文字")
    if source == "broken":
        mime.setData("image/png", b"not an image")
    else:
        mime.setImageData(QImage() if source == "null" else image)
        if source == "encode_failure":
            monkeypatch.setattr(QImage, "save", lambda *args: False)
    clipboard.setMimeData(mime)
    errors = []
    widget.error_occurred.connect(errors.append)
    qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert len(errors) == 1 and "图片" in errors[0]
    assert widget.editor is editor and editor.toPlainText() == "不能丢失的草稿"
    assert widget.engine.document == before and widget.engine.revision == revision


@pytest.mark.parametrize("composing_in", ["editor", "properties"])
def test_image_paste_preserves_input_method_candidates(canvas, clipboard, image, composing_in, qtbot):
    widget, first, second = canvas
    panels = MindMapPanels(widget)
    try:
        widget.select_nodes([first])
        if composing_in == "editor":
            widget.begin_edit(first)
            widget.editor.setPlainText("未提交的正文")
            widget.scene.sendEvent(widget.editor, QInputMethodEvent("候选", []))
        else:
            field = panels.properties.fields["style.font_family"].lineEdit()
            QApplication.sendEvent(field, QInputMethodEvent("候选", []))
        mime = QMimeData()
        mime.setImageData(image)
        clipboard.setMimeData(mime)
        before, revision = widget.engine.document, widget.engine.revision
        with pytest.raises(MindMapError, match="候选"):
            widget.paste()
        if composing_in == "editor":
            errors = []
            widget.error_occurred.connect(errors.append)
            qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            assert len(errors) == 1 and widget.is_composing
            assert widget.editor.toPlainText() == "未提交的正文"
            widget.scene.sendEvent(widget.editor, QInputMethodEvent())
        else:
            assert panels.is_composing
            QApplication.sendEvent(field, QInputMethodEvent())
        assert widget.engine.document == before and widget.engine.revision == revision
    finally:
        panels.dispose()


def test_bad_image_keeps_property_draft_and_success_commits_it_before_image(canvas, clipboard, image):
    widget, first, second = canvas
    panels = MindMapPanels(widget)
    try:
        widget.select_nodes([first])
        panels.properties.fields["style.font_size"].setValue(29)
        panels.properties.timer.stop()
        before = widget.engine.document
        mime = QMimeData()
        mime.setData("image/png", b"broken")
        clipboard.setMimeData(mime)
        with pytest.raises(MindMapError, match="图片"):
            widget.paste()
        assert widget.engine.document == before
        mime = QMimeData()
        mime.setImageData(image)
        clipboard.setMimeData(mime)
        created, = widget.paste()
        assert widget.engine.get_node(first).style["font_size"] == 29
        assert widget.engine.get_node(created).is_image_node
        assert widget.engine.undo()
        assert created not in widget.engine.document.nodes
        assert widget.engine.get_node(first).image_id is None
        assert widget.engine.get_node(first).style["font_size"] == 29
        assert widget.engine.undo() and widget.engine.document == before
    finally:
        panels.dispose()


@pytest.mark.parametrize("blocked_by", ["readonly", "hook", "disposed"])
def test_rejected_image_operation_leaves_document_and_selection_unchanged(
    canvas, clipboard, image, blocked_by,
):
    widget, first, second = canvas
    widget.select_nodes([first])
    mime = QMimeData()
    mime.setImageData(image)
    clipboard.setMimeData(mime)
    before, revision = widget.engine.document, widget.engine.revision
    if blocked_by == "readonly":
        widget.set_editable(False)
        assert not widget.action("paste").isEnabled()
    elif blocked_by == "hook":
        widget.engine.before_operation = lambda operation, args: operation != "add_image_child"
    else:
        widget.dispose()
    with pytest.raises(MindMapError):
        widget.paste()
    assert widget.engine.document == before and widget.engine.revision == revision
    assert widget.selected_node_ids() == [first]


@pytest.mark.parametrize("source", ["native", "edraw"])
def test_node_fragments_take_precedence_over_images(canvas, clipboard, image, source, monkeypatch, qtbot):
    widget, first, second = canvas
    widget.select_nodes([second])
    mime = QMimeData()
    mime.setImageData(image)
    mime.setText("不使用的文字")
    if source == "native":
        mime.setData(CLIPBOARD_MIME, encode_document(widget.engine.copy_fragment([first]), format_id="json"))
    else:
        mime.setData(EDRAW_CLIPBOARD_MIME, b'<Clipboard><Shapes><Shape Type="MainIdea" ID="1">'
                     b'<Text><HtmlText>Edraw</HtmlText></Text></Shape></Shapes></Clipboard>')
        monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                            lambda *args, **kwargs: EdrawPasteOptions("append", True, True))
    clipboard.setMimeData(mime)
    ids = widget.paste()
    assert len(ids) == 1 and ids[0] != second
    assert widget.engine.get_node(second).image_id is None
    assert widget.engine.get_node(ids[0]).image_id is None
    assert not widget.engine.document.assets
    widget.begin_edit(second)
    before = widget.engine.document
    qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert widget.editor.toPlainText() == "不使用的文字"
    assert widget.engine.document == before


def test_pasted_image_roundtrips_and_appears_in_export(canvas, clipboard, image, tmp_path):
    widget, first, second = canvas
    widget.select_nodes([first])
    mime = QMimeData()
    data = encoded(image)
    mime.setData("image/png", data)
    clipboard.setMimeData(mime)
    created, = widget.paste()
    target = tmp_path / "剪贴板图片.mindmap"
    save_document(widget.engine.document, target)
    reopened = load_document(target)
    assert reopened == widget.engine.document
    assert reopened.assets[reopened.nodes[created].image_id].data == data
    output = tmp_path / "剪贴板图片.png"
    widget.export(output)
    rendered = QImage(str(output))
    assert not rendered.isNull()
    assert any(rendered.pixelColor(x, y).name() == "#e94665"
               for x in range(0, rendered.width(), 3) for y in range(0, rendered.height(), 3))
