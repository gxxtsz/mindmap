"""亿图粘贴的系统载荷、交互、原子更新和选择历史回归。"""

from copy import deepcopy

import pytest
from mindmap_engine import MindMapEngine, MindMapError, Theme
from mindmap_qt import MindMapWidget
from mindmap_qt.clipboard import EDRAW_CLIPBOARD_MIME, read_edraw_clipboard
from mindmap_qt.dialogs import EdrawPasteOptions, edraw_paste_options
from mindmap_qt.ui_style import ui_colors
from mindmap_qt.widget import CLIPBOARD_MIME
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QLabel, QPushButton, QWidget

XML = '''<?xml version="1.0" encoding="UTF-8"?><Clipboard Version="14.0.4" Page="100"><Shapes>
<Shape ID="101" Type="MainIdea"><Text><HtmlText>&lt;p&gt;亿图根😀&lt;/p&gt;</HtmlText></Text>
  <LevelData><SubLevel V="104;102"/></LevelData><Layout><Toggler V="1"/></Layout>
  <ShapeFormat Color="red"/><Note>源备注</Note><Image>不导入</Image></Shape>
<Shape ID="102" Type="SubTopic"><Text><HtmlText>&lt;p&gt;来源二&lt;br/&gt;第二行&lt;/p&gt;</HtmlText></Text>
  <LevelData><Super V="101"/><SubLevel V="106"/></LevelData></Shape>
<Shape ID="104" Type="MainTopic"><Text><HtmlText>&lt;p&gt;来源一&lt;/p&gt;</HtmlText></Text>
  <LevelData><Super V="101"/></LevelData></Shape>
<Shape ID="106" Type="SubTopic"><Text><HtmlText>&lt;p&gt;更深层&lt;/p&gt;</HtmlText></Text>
  <LevelData><Super V="102"/></LevelData></Shape>
<Shape ID="103" Type="MMConnector"/><Shape ID="105" Type="Summary"/>
</Shapes></Clipboard>'''.encode()
FOREST = XML.replace(b'<Super V="101"/>', b'<Super V="999"/>').replace(
    b'<SubLevel V="104;102"/>', b""
)


@pytest.fixture
def canvas(qtbot, qapp):
    """使用离屏剪贴板，不保留 MIME 对象到 QApplication 销毁阶段。"""
    original = QMimeData()
    mime = qapp.clipboard().mimeData()
    if mime is not None:
        for fmt in mime.formats():
            original.setData(fmt, mime.data(fmt))
    engine = MindMapEngine()
    target = engine.add_child(engine.root_id, "原目标")
    old_child = engine.add_child(target, "旧后代")
    engine.add_child(old_child, "旧孙节点")
    engine.update_node(target, style={"font_size": 19, "branch": {"kind": "round"}},
                       tags=["原标签"], note="原备注", hyperlink="https://example.com")
    engine.set_theme(Theme.dark())
    image = QImage(12, 10, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    engine.attach_image(old_child, bytes(buffer.data()))
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine)
    qtbot.addWidget(widget)
    widget.resize(900, 600)
    widget.show()
    widget.select_nodes([target])
    QApplication.processEvents()
    yield widget, target, old_child
    widget.dispose()
    if qapp.platformName() == "offscreen":
        qapp.clipboard().clear()
    else:
        qapp.clipboard().setMimeData(original)


def set_clipboard(data=XML):
    mime = QMimeData()
    mime.setData(EDRAW_CLIPBOARD_MIME, data)
    mime.setText("不得降级为这段纯文本")
    QApplication.clipboard().setMimeData(mime)


def choose(monkeypatch, mode="append", keep=True):
    monkeypatch.setattr("mindmap_qt.widget.edraw_paste_options",
                        lambda *args, **kwargs: EdrawPasteOptions(mode, keep))


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("root_target", [False, True])
def test_paste_transaction_styles_identity_and_selection(canvas, monkeypatch, replace, keep, root_target):
    widget, target, old_child = canvas
    if root_target:
        target = widget.engine.root_id
        widget.engine.update_node(target, style={"branch": {"kind": "round"}})
        widget.engine.replace_document(widget.engine.document)
        widget.select_nodes([target])
    engine = widget.engine
    before = engine.document
    before_revision = engine.revision
    old = before.nodes[target]
    choose(monkeypatch, "replace" if replace else "append", keep)
    set_clipboard()
    ids = widget.paste()
    result = engine.document
    assert engine.revision == before_revision + 1
    assert result.layout == before.layout and result.theme == before.theme
    assert widget.selected_node_ids() == ids
    assert widget.edraw_paste_options == ("replace" if replace else "append", keep)
    if replace:
        assert ids == [target] and result.nodes[target].text == "亿图根😀"
        kept = old.children if keep else []
        children = result.nodes[target].children
        assert children[:len(kept)] == kept
        assert [result.nodes[key].text for key in children[len(kept):]] == ["来源一", "来源二\n第二行"]
        expected = deepcopy(old)
        expected.text = "亿图根😀"
        expected.children = children
        expected.expanded = True
        assert result.nodes[target] == expected
        assert (old_child in result.nodes) == keep
        assert bool(result.assets) == keep
    else:
        assert len(ids) == 1 and ids[0] != target
        assert result.nodes[target].text == old.text
        assert result.nodes[target].children == [*old.children, *ids]
        assert result.nodes[ids[0]].text == "亿图根😀"
        assert old_child in result.nodes and result.assets == before.assets
    for key in result.nodes.keys() - before.nodes.keys():
        node = result.nodes[key]
        assert node.style == {"branch": {"kind": "round"}}
        assert node.text_format == "plain" and node.expanded and node.direction is None
        assert not any((node.note, node.hyperlink, node.image_id, node.tags, node.icons, node.metadata))
    engine.undo()
    assert engine.document == before and not engine.can_undo and not engine.is_dirty
    assert widget.selected_node_ids() == [target]
    engine.redo()
    assert engine.document == result and widget.selected_node_ids() == ids


def test_multiple_roots_preserve_single_tree_preference(canvas, monkeypatch):
    widget, target, _ = canvas
    widget.set_edraw_paste_options("replace", False)
    choose(monkeypatch, "append", False)
    set_clipboard(FOREST)
    ids = widget.paste()
    assert [widget.engine.get_node(key).text for key in ids] == ["亿图根😀", "来源二\n第二行", "来源一"]
    assert widget.engine.get_node(target).text == "原目标"
    assert widget.edraw_paste_options == ("replace", False)


def snapshot(widget):
    return (widget.engine.document, widget.engine.revision, widget.engine.is_dirty,
            widget.engine.can_undo, widget.engine.can_redo, widget.selected_node_ids(),
            deepcopy(widget._selection_undo), deepcopy(widget._selection_redo), widget.edraw_paste_options)


def test_cancel_invalid_payload_and_hook_rejection_are_atomic(canvas, monkeypatch):
    widget, _, _ = canvas
    widget.set_edraw_paste_options("replace", False)
    before = snapshot(widget)
    monkeypatch.setattr("mindmap_qt.widget.edraw_paste_options", lambda *args, **kwargs: None)
    set_clipboard()
    assert widget.paste() == [] and snapshot(widget) == before
    set_clipboard(b"<Clipboard><broken>")
    with pytest.raises(MindMapError, match="XML"):
        widget.paste()
    assert snapshot(widget) == before
    set_clipboard()
    choose(monkeypatch, "replace", False)
    widget.engine.before_operation = lambda operation, args: operation != "paste_fragment"
    with pytest.raises(MindMapError):
        widget.paste()
    assert snapshot(widget) == before


def test_readonly_does_not_show_dialog_or_change_state(canvas, monkeypatch):
    widget, _, _ = canvas
    set_clipboard()
    widget.set_editable(False)
    before = snapshot(widget)
    monkeypatch.setattr("mindmap_qt.widget.edraw_paste_options", lambda *args, **kwargs: pytest.fail("不应弹窗"))
    with pytest.raises(MindMapError, match="只读"):
        widget.paste()
    assert not widget.action("paste").isEnabled() and snapshot(widget) == before


def test_document_change_during_dialog_is_not_overwritten(canvas, monkeypatch):
    widget, _, _ = canvas
    set_clipboard()

    def reset(*args, **kwargs):
        widget.engine.replace_document(MindMapEngine().document)
        return EdrawPasteOptions("replace", False)

    monkeypatch.setattr("mindmap_qt.widget.edraw_paste_options", reset)
    with pytest.raises(MindMapError, match="已发生变化"):
        widget.paste()
    assert len(widget.engine.document.nodes) == 1 and not widget.engine.can_undo
    assert widget.edraw_paste_options == ("append", True)


def test_native_json_priority_and_text_regression(canvas, monkeypatch):
    widget, target, old_child = canvas
    monkeypatch.setattr("mindmap_qt.widget.edraw_paste_options", lambda *args, **kwargs: pytest.fail("不应弹窗"))
    widget.copy()
    mime = QApplication.clipboard().mimeData()
    assert mime.hasFormat(CLIPBOARD_MIME)
    mime.setData(EDRAW_CLIPBOARD_MIME, b"invalid")
    ids = widget.paste()
    assert widget.engine.get_node(ids[0]).style == widget.engine.get_node(target).style
    assert len(widget.engine.get_node(ids[0]).children) == 1
    assert widget.engine.get_node(old_child).image_id in widget.engine.document.assets
    for text, title in [("纯文本\n  子节点", "纯文本"), ("# Markdown\n- 子节点", "Markdown")]:
        QApplication.clipboard().setText(text)
        ids = widget.paste()
        assert widget.engine.get_node(ids[0]).text == title


def test_repeated_paste_generates_independent_ids(canvas, monkeypatch):
    widget, target, _ = canvas
    choose(monkeypatch)
    set_clipboard()
    first = widget.paste()[0]
    widget.select_nodes([target])
    second = widget.paste()[0]
    document = widget.engine.document
    assert set(document.descendants(first)).isdisjoint(document.descendants(second))
    document.validate()


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"])
def test_xml_encoding_and_file_payload(tmp_path, encoding):
    xml_encoding = "UTF-8" if encoding == "utf-8-sig" else "UTF-16"
    text = XML.decode().replace('encoding="UTF-8"', f'encoding="{xml_encoding}"')
    data = text.encode(encoding)
    document = read_edraw_clipboard(data)
    assert document.nodes[document.root.children[0]].text == "亿图根😀"
    directory = tmp_path / "亿图 临时目录"
    directory.mkdir()
    path = directory / "copy.xml"
    path.write_bytes(data)
    from_path = read_edraw_clipboard(str(path).encode() + b"\0")
    assert [node.text for node in from_path.nodes.values()] == [node.text for node in document.nodes.values()]


def test_missing_invalid_and_oversized_file(tmp_path, monkeypatch):
    from mindmap_qt import clipboard

    with pytest.raises(MindMapError, match="失效"):
        read_edraw_clipboard(str(tmp_path / "copy.xml").encode())
    for payload in [b"copy.xml", str(tmp_path / "other.xml").encode(), b"not xml"]:
        with pytest.raises(MindMapError, match="既不是 XML"):
            read_edraw_clipboard(payload)
    path = tmp_path / "copy.xml"
    path.write_bytes(XML)
    monkeypatch.setattr(clipboard, "MAX_ARCHIVE_BYTES", len(XML) - 1)
    with pytest.raises(MindMapError, match="大小超过"):
        read_edraw_clipboard(str(path).encode())


@pytest.mark.parametrize("root_count", [1, 3])
def test_actual_dialog_defaults_switches_and_cancel(canvas, root_count):
    widget, _, _ = canvas
    before = snapshot(widget)

    def inspect_dialog():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, QDialog) and dialog.windowTitle() == "粘贴亿图脑图"
            assert any("目标节点：<原目标>" in label.text() for label in dialog.findChildren(QLabel))
            append = dialog.findChild(QAbstractButton, "edraw_append")
            replace = dialog.findChild(QAbstractButton, "edraw_replace")
            keep = dialog.findChild(QPushButton, "edraw_keep_children")
            remove = dialog.findChild(QPushButton, "edraw_remove_children")
            descendants = dialog.findChild(QWidget, "edraw_descendants")
            confirm = dialog.findChild(QPushButton, "edraw_paste_confirm")
            assert append.palette().color(QPalette.ColorRole.WindowText) == QColor(Theme.dark().text)
            assert replace.isEnabled() == (root_count == 1)
            assert replace.isChecked() == (root_count == 1)
            assert keep.isEnabled() == (root_count == 1) and not keep.isChecked()
            assert remove.isChecked() and descendants.isVisible() == (root_count == 1)
            assert confirm.text() == ("替换粘贴" if root_count == 1 else "追加粘贴")
            append.setChecked(True)
            assert not keep.isEnabled() and not descendants.isVisible()
            assert remove.isChecked() and confirm.text() == "追加粘贴"
            if root_count == 1:
                replace.setChecked(True)
                assert keep.isEnabled() and not keep.isChecked()
                assert descendants.isVisible() and confirm.text() == "替换粘贴"
                keep.setChecked(True)
                assert not remove.isChecked()
        finally:
            dialog.accept()

    QTimer.singleShot(0, inspect_dialog)
    result = edraw_paste_options(widget, target_text="<原目标>", root_count=root_count, node_count=8,
                                 mode="replace", keep_children=False, theme=Theme.dark())
    assert result == EdrawPasteOptions("replace" if root_count == 1 else "append", root_count == 1)
    assert snapshot(widget) == before
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    assert edraw_paste_options(widget, target_text="取消", root_count=1, node_count=1) is None


def test_real_paste_shortcut_and_file_payload(canvas, qtbot, tmp_path):
    widget, target, _ = canvas
    path = tmp_path / "copy.xml"
    path.write_bytes(XML)
    set_clipboard(str(path).encode())
    widget.activateWindow()
    widget.view.setFocus()
    QApplication.processEvents()

    def accept():
        dialog = QApplication.activeModalWidget()
        try:
            assert dialog.objectName() == "edraw_paste_dialog"
            assert dialog.findChild(QAbstractButton, "edraw_append").isChecked()
        finally:
            dialog.accept()

    QTimer.singleShot(0, accept)
    qtbot.keyClick(widget.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert len(widget.engine.get_node(target).children) == 2
    assert widget.engine.get_node(widget.selected_node_ids()[0]).text == "亿图根😀"


def test_options_validation_signal_and_document_independence(canvas):
    widget, _, _ = canvas
    before = snapshot(widget)[:-1]
    changes = []
    widget.edraw_paste_options_changed.connect(lambda *values: changes.append(values))
    widget.set_edraw_paste_options("replace", False)
    widget.set_edraw_paste_options("replace", False)
    assert changes == [("replace", False)] and snapshot(widget)[:-1] == before
    for values in [("bad", True), ("append", 1), (None, False)]:
        with pytest.raises(MindMapError):
            widget.set_edraw_paste_options(*values)
    assert changes == [("replace", False)] and widget.edraw_paste_options == ("replace", False)


@pytest.mark.parametrize("dark", [False, True])
def test_choice_cards_show_full_selection_and_accept_padding_clicks(canvas, qtbot, dark):
    """选择靠整块底色表达，整行留白可点，键盘仍能操作；避免仅检查逻辑勾选值。"""
    widget, _, _ = canvas
    theme = Theme.dark() if dark else Theme()
    colors = ui_colors(theme)

    def sample(button):
        image = button.grab().toImage()
        ratio = image.devicePixelRatio()
        return image.pixelColor(round(10 * ratio), round(button.height() / 2 * ratio))

    def interact():
        dialog = QApplication.activeModalWidget()
        try:
            append = dialog.findChild(QAbstractButton, "edraw_append")
            replace = dialog.findChild(QAbstractButton, "edraw_replace")
            assert sample(append) == QColor(colors.selected)
            assert sample(replace) == QColor(colors.field)
            assert replace.accessibleName() == "替换当前主题" and replace.accessibleDescription()
            qtbot.mouseClick(replace, Qt.MouseButton.LeftButton, pos=QPoint(10, replace.height() - 10))
            assert replace.isChecked() and not append.isChecked()
            assert sample(replace) == QColor(colors.selected)
            append.setFocus()
            QApplication.processEvents()
            qtbot.keyClick(append, Qt.Key.Key_Space)
            assert append.isChecked() and not replace.isChecked()
            qtbot.keyClick(append, Qt.Key.Key_Down)
            assert replace.isChecked() and not append.isChecked()
        finally:
            dialog.accept()

    QTimer.singleShot(0, interact)
    assert edraw_paste_options(widget, target_text="目标", root_count=1, node_count=2530,
                               theme=theme) == EdrawPasteOptions("replace", True)
