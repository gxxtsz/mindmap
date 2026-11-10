"""按真实能力归属迁移的组件与行为回归。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapEngine, MindMapError, Theme
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.clipboard import EDRAW_CLIPBOARD_MIME, read_edraw_clipboard
from mindmap_qt_canvas.widget import CLIPBOARD_MIME
from mindmap_qt_ui.dialogs import EdrawPasteOptions
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QAbstractButton, QApplication

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
LEAF = b'<Clipboard><Shapes><Shape Type="MainIdea" ID="1">' \
       b'<Text><HtmlText>&lt;p&gt;Edraw&lt;/p&gt;</HtmlText></Text></Shape></Shapes></Clipboard>'


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
                       tags=["原标签"], note="原备注", hyperlink="https://example.com", text_format="markdown")
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


def choose(monkeypatch, mode="append", keep=True, replace_root=True):
    """替换交互结果以验证真实粘贴路径；省略新参数时沿用原有替换根节点行为。"""
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                        lambda *args, **kwargs: EdrawPasteOptions(mode, keep, replace_root))


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("root_target", [False, True])
@pytest.mark.parametrize("replace_root", [False, True])
def test_paste_transaction_styles_identity_and_selection(
    canvas, monkeypatch, replace, keep, root_target, replace_root
):
    """普通及中心主题的四种替换组合均保留标识和外观，正文格式与选项一致，历史只提交一次。"""
    widget, target, old_child = canvas
    if root_target:
        target = widget.engine.root_id
        widget.engine.update_node(target, style={"branch": {"kind": "round"}}, text_format="markdown")
        widget.engine.replace_document(widget.engine.document)
        widget.select_nodes([target])
    engine = widget.engine
    before = engine.document
    before_revision = engine.revision
    old = before.nodes[target]
    operations = []
    engine.before_operation = lambda operation, args: operations.append(operation)
    choose(monkeypatch, "replace" if replace else "append", keep, replace_root)
    set_clipboard()
    ids = widget.paste()
    result = engine.document
    assert engine.revision == before_revision + 1
    assert result.layout == before.layout and result.theme == before.theme
    assert widget.selected_node_ids() == ids
    assert widget.edraw_paste_options == ("replace" if replace else "append", keep, replace_root)
    assert ("update_node" in operations) == (replace and replace_root)
    if replace:
        assert ids == [target] and result.nodes[target].text == ("亿图根😀" if replace_root else old.text)
        kept = old.children if keep else []
        children = result.nodes[target].children
        assert children[:len(kept)] == kept
        assert [result.nodes[key].text for key in children[len(kept):]] == ["来源一", "来源二\n第二行"]
        expected = deepcopy(old)
        if replace_root:
            expected.text = "亿图根😀"
            expected.text_format = "plain"
        expected.children = children
        expected.expanded = True
        assert result.nodes[target] == expected
        assert (old_child in result.nodes) == keep
        assert bool(result.assets) == keep
    else:
        assert len(ids) == 1 and ids[0] != target
        assert result.nodes[target].text == old.text
        assert result.nodes[target].text_format == old.text_format
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
    """多根来源强制追加时不保存任何单树选项，即使交互结果携带不同的后代及根节点开关。"""
    widget, target, _ = canvas
    widget.set_edraw_paste_options("replace", False, False)
    choose(monkeypatch, "append", True, True)
    set_clipboard(FOREST)
    ids = widget.paste()
    assert [widget.engine.get_node(key).text for key in ids] == ["亿图根😀", "来源二\n第二行", "来源一"]
    assert widget.engine.get_node(target).text == "原目标"
    assert widget.edraw_paste_options == ("replace", False, False)


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("replace_root", [False, True])
def test_source_without_children_respects_both_choices(canvas, monkeypatch, keep, replace_root):
    """仅有来源根时不创建多余节点；按开关更新正文和旧后代，完整空操作不生成历史。"""
    widget, target, old_child = canvas
    before = widget.engine.document
    revision = widget.engine.revision
    choose(monkeypatch, "replace", keep, replace_root)
    set_clipboard(LEAF)
    assert widget.paste() == [target]
    result = widget.engine.document
    expected = deepcopy(before.nodes[target])
    if replace_root:
        expected.text, expected.text_format = "Edraw", "plain"
    if not keep:
        expected.children = []
    assert result.nodes[target] == expected
    assert not (result.nodes.keys() - before.nodes.keys())
    assert (old_child in result.nodes) == keep and bool(result.assets) == keep
    assert widget.selected_node_ids() == [target]
    assert widget.edraw_paste_options == ("replace", keep, replace_root)
    changed = result != before
    assert widget.engine.revision == revision + int(changed)
    assert widget.engine.can_undo == changed
    if changed:
        widget.engine.undo()
        assert widget.engine.document == before and widget.selected_node_ids() == [target]
        widget.engine.redo()
        assert widget.engine.document == result and widget.selected_node_ids() == [target]
    else:
        assert not widget.engine.is_dirty and not widget._selection_undo


def snapshot(widget):
    return (widget.engine.document, widget.engine.revision, widget.engine.is_dirty,
            widget.engine.can_undo, widget.engine.can_redo, widget.selected_node_ids(),
            deepcopy(widget._selection_undo), deepcopy(widget._selection_redo), widget.edraw_paste_options)


@pytest.mark.parametrize("replace_root", [False, True])
def test_cancel_invalid_payload_and_hook_rejection_are_atomic(canvas, monkeypatch, replace_root):
    widget, _, _ = canvas
    widget.set_edraw_paste_options("replace", False, False)
    before = snapshot(widget)
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", lambda *args, **kwargs: None)
    set_clipboard()
    assert widget.paste() == [] and snapshot(widget) == before
    set_clipboard(b"<Clipboard><broken>")
    with pytest.raises(MindMapError, match="XML"):
        widget.paste()
    assert snapshot(widget) == before
    set_clipboard()
    choose(monkeypatch, "replace", False, replace_root)
    widget.engine.before_operation = lambda operation, args: operation != "paste_fragment"
    with pytest.raises(MindMapError):
        widget.paste()
    assert snapshot(widget) == before


@pytest.mark.parametrize("replace_root", [None, 0, 1, "false"])
def test_invalid_root_choice_does_not_change_document_or_preferences(canvas, monkeypatch, replace_root):
    """即使非界面调用者提供非法开关，也必须在修改文档前拒绝整个粘贴操作。"""
    widget, _, _ = canvas
    before = snapshot(widget)
    choose(monkeypatch, "replace", False, replace_root)
    set_clipboard()
    with pytest.raises(MindMapError, match="选项无效"):
        widget.paste()
    assert snapshot(widget) == before


def test_readonly_does_not_show_dialog_or_change_state(canvas, monkeypatch):
    widget, _, _ = canvas
    set_clipboard()
    widget.set_editable(False)
    before = snapshot(widget)
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", lambda *args, **kwargs: pytest.fail("不应弹窗"))
    with pytest.raises(MindMapError, match="只读"):
        widget.paste()
    assert not widget.action("paste").isEnabled() and snapshot(widget) == before


def test_document_change_during_dialog_is_not_overwritten(canvas, monkeypatch):
    widget, _, _ = canvas
    set_clipboard()

    def reset(*args, **kwargs):
        widget.engine.replace_document(MindMapEngine().document)
        return EdrawPasteOptions("replace", False)

    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", reset)
    with pytest.raises(MindMapError, match="已发生变化"):
        widget.paste()
    assert len(widget.engine.document.nodes) == 1 and not widget.engine.can_undo
    assert widget.edraw_paste_options == ("append", True, True)


def test_native_json_priority_and_text_regression(canvas, monkeypatch):
    widget, target, old_child = canvas
    monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", lambda *args, **kwargs: pytest.fail("不应弹窗"))
    widget.copy()
    mime = QApplication.clipboard().mimeData()
    assert mime.hasFormat(CLIPBOARD_MIME)
    mime.setData(EDRAW_CLIPBOARD_MIME, b"invalid")
    ids = widget.paste()
    assert widget.engine.get_node(ids[0]).style == widget.engine.get_node(target).style
    assert len(widget.engine.get_node(ids[0]).children) == 1
    assert widget.engine.get_node(old_child).image_id in widget.engine.document.assets
    # 外部文本现在与亿图共用配置弹窗；项目原生片段仍在上面验证为直接粘贴。
    choose(monkeypatch)
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
    from mindmap_qt_canvas import clipboard

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
    """新参数严格要求布尔值，三参数变更仅发送一次，旧二参数设置默认替换根节点。"""
    widget, _, _ = canvas
    before = snapshot(widget)[:-1]
    changes = []
    widget.edraw_paste_options_changed.connect(lambda *values: changes.append(values))
    widget.set_edraw_paste_options("replace", False)
    widget.set_edraw_paste_options("replace", False)
    assert changes == [("replace", False, True)] and snapshot(widget)[:-1] == before
    widget.set_edraw_paste_options("replace", False, False)
    widget.set_edraw_paste_options("replace", False, False)
    expected_changes = [("replace", False, True), ("replace", False, False)]
    assert changes == expected_changes and snapshot(widget)[:-1] == before
    for values in [("bad", True), ("append", 1), (None, False),
                   ("replace", True, None), ("replace", True, 0), ("replace", True, "false")]:
        with pytest.raises(MindMapError):
            widget.set_edraw_paste_options(*values)
    assert changes == expected_changes and widget.edraw_paste_options == ("replace", False, False)
