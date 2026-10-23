"""原生组件交互、导出及资源导入测试。"""

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mindmap_core import LayoutConfig, MindMapEngine, MindMapError, Theme
from mindmap_formats import load_document, save_document
from mindmap_qt_canvas import MindMapWidget
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QImage, QInputMethodEvent, QKeyEvent, QRawFont
from PySide6.QtPdf import QPdfDocument
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMenu


@pytest.fixture
def canvas(qtbot):
    engine = MindMapEngine()
    engine.set_layout(LayoutConfig(direction="right"))
    a = engine.add_child(engine.root_id, "第一分支")
    b = engine.add_child(engine.root_id, "第二分支")
    engine.add_child(a, "子节点")
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine)
    qtbot.addWidget(widget)
    widget.resize(1100, 750)
    widget.show()
    widget.activateWindow()
    qtbot.waitUntil(lambda: widget.isVisible())
    QApplication.processEvents()
    widget.fit_to_view()
    yield widget, a, b
    widget.finish_edit(commit=False)
    widget.dispose()


@pytest.fixture
def png_bytes(qapp):
    image = QImage(120, 80, QImage.Format.Format_ARGB32)
    image.fill(QColor("#e95e38"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


@pytest.fixture
def isolated_clipboard(qapp):
    """隔离系统剪贴板；离屏用例结束时释放 Qt 所拥有的 MIME 数据。"""
    clipboard = qapp.clipboard()
    original = QMimeData()
    mime = clipboard.mimeData()
    if mime is not None:
        for fmt in mime.formats():
            original.setData(fmt, mime.data(fmt))
    yield
    if qapp.platformName() == "offscreen":
        clipboard.clear()
    else:
        clipboard.setMimeData(original)
    qapp.processEvents()


def test_item_identity_and_multi_instance(canvas, qtbot):
    widget, a, b = canvas
    original = widget.node_items[b]
    widget.engine.update_node(a, text="改名")
    assert widget.node_items[b] is original
    second = MindMapWidget()
    qtbot.addWidget(second)
    second.show()
    count = len(widget.engine.document.nodes)
    second.engine.add_child(second.engine.root_id)
    assert len(widget.engine.document.nodes) == count
    second.dispose()


def test_selection_order_tracks_tree_after_move(canvas):
    widget, a, b = canvas
    widget.engine.move_up(b)
    widget.select_nodes([a, b])
    assert widget.selected_node_ids() == [b, a]


@pytest.mark.skipif(sys.platform != "win32", reason="检查 Windows 桌面与离屏字体回归")
def test_windows_chinese_font_is_available(canvas, qapp):
    assert QRawFont.fromFont(qapp.font()).supportsCharacter(ord("中"))


def test_edit_session_ime_and_single_undo(canvas):
    widget, a, _ = canvas
    before = widget.engine.document
    widget.begin_edit(a)
    editor = widget.editor
    preedit = QInputMethodEvent("zhong", [])
    widget.scene.sendEvent(editor, preedit)
    assert editor.composing
    assert widget.engine.document == before
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Tab, Qt.Key.Key_Escape):
        widget.scene.sendEvent(
            editor, QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        )
        assert widget.editor is editor and editor.composing
        assert "\n" not in editor.toPlainText()
        assert widget.engine.document == before
    commit = QInputMethodEvent()
    commit.setCommitString("中文输入法")
    widget.scene.sendEvent(editor, commit)
    assert not editor.composing
    enter = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    widget.scene.sendEvent(editor, enter)
    assert widget.editor is None
    assert widget.engine.get_node(a).text == "中文输入法"
    widget.engine.undo()
    assert widget.engine.document == before
    assert not widget.engine.can_undo


def test_shortcut_tab_and_cancel(canvas, qtbot):
    widget, a, _ = canvas
    widget.select_nodes([a])
    widget.view.setFocus()
    before = len(widget.engine.document.nodes)
    qtbot.keyClick(widget.view, Qt.Key.Key_Tab)
    assert len(widget.engine.document.nodes) == before + 1
    assert widget.editor is not None
    widget.editor.setPlainText("未提交")
    widget.scene.sendEvent(
        widget.editor, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert widget.editor is None
    assert all(node.text != "未提交" for node in widget.engine.document.nodes.values())


def test_readonly_blocks_ui_changes(canvas, qtbot):
    widget, a, _ = canvas
    widget.select_nodes([a])
    widget.set_editable(False)
    before = widget.engine.document
    widget.begin_edit(a)
    assert widget.editor is None
    assert not widget.action("delete").isEnabled()
    qtbot.keyClick(widget.view, Qt.Key.Key_Delete)
    assert widget.engine.document == before
    with pytest.raises(MindMapError):
        widget.add_child()
    assert widget.action("copy").isEnabled()


def test_clipboard_cross_instance_nodes_and_images(canvas, qtbot, isolated_clipboard, png_bytes):
    widget, a, b = canvas
    asset = widget.engine.attach_image(a, png_bytes)
    widget.engine.attach_image(b, png_bytes)
    widget.select_nodes([a, b])
    widget.copy()
    other = MindMapWidget()
    qtbot.addWidget(other)
    other.engine.set_layout(LayoutConfig(direction="right"))
    before = other.engine.document
    copied = other.paste()
    document = other.engine.document
    assert len(copied) == 2 and a not in copied and b not in copied
    assert set(document.assets) == {asset}
    assert all(document.nodes[key].image_id == asset for key in copied)
    assert len(document.nodes[copied[0]].children) == 1
    copied_child = document.nodes[copied[0]].children[0]
    assert copied_child not in widget.engine.document.nodes
    assert document.nodes[copied_child].text == "子节点"
    assert other.selected_node_ids() == copied
    other.engine.undo()
    assert other.engine.document == before and not other.engine.document.assets
    other.engine.redo()
    assert other.engine.document == document
    assert other.selected_node_ids() == copied
    document.validate()
    other.dispose()


def test_selection_restored_after_delete_undo(canvas):
    widget, a, _ = canvas
    widget.select_nodes([a])
    widget.delete_selected()
    assert a not in widget.node_items
    widget.engine.undo()
    assert widget.selected_node_ids() == [a]


def test_drag_node_and_single_undo(canvas, qtbot):
    widget, a, b = canvas
    widget.select_nodes([a])
    start = widget.view.mapFromScene(widget.node_items[a].sceneBoundingRect().center())
    end = widget.view.mapFromScene(widget.node_items[b].sceneBoundingRect().center())
    qtbot.mousePress(widget.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(widget.view.viewport(), start + QPoint(25, 0), delay=20)
    qtbot.mouseMove(widget.view.viewport(), end, delay=20)
    qtbot.mouseRelease(widget.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert widget.engine.get_node(b).children == [a]
    widget.engine.undo()
    assert a in widget.engine.document.root.children


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_export_contents_and_no_document_changes(canvas, png_bytes, tmp_path, suffix):
    widget, a, b = canvas
    widget.engine.attach_image(a, png_bytes)
    widget.engine.update_node(b, text="**粗体**与中文\n\n- 清单", text_format="markdown", tags=["标签"])
    before = widget.engine.document
    target = tmp_path / ("图形." + suffix)
    widget.export(target)
    assert widget.engine.document == before
    assert target.stat().st_size > 500
    if suffix == "png":
        image = QImage(str(target))
        assert not image.isNull()
        # 节点内部不再预留或绘制折叠按钮，正文右侧留白保持节点背景。
        root = widget.engine.root_id
        bounds = widget.layout_result.bounds
        rect = widget.layout_result.nodes[root]
        visual = widget.node_items[root].visual
        x = round(rect.x - bounds.x + visual.width - 7)
        y = round(rect.y - bounds.y + visual.height / 2)
        assert image.pixelColor(x, y).name() == visual.background.name()
        # 找到红色图片像素，避免只生成了空背景却被当成成功导出。
        assert any(
            image.pixelColor(x, y).red() > 200 and image.pixelColor(x, y).green() < 130
            for x in range(0, image.width(), 10)
            for y in range(0, image.height(), 10)
        )
    elif suffix == "svg":
        assert QSvgRenderer(str(target)).isValid()
        assert b"image" in target.read_bytes()
    else:
        assert target.read_bytes().startswith(b"%PDF-")
        assert b"%%EOF" in target.read_bytes()
        pdf = QPdfDocument()
        assert pdf.load(str(target)) == QPdfDocument.Error.None_
        assert pdf.pageCount() == 1
        size = pdf.pagePointSize(0)
        image = pdf.render(0, QSize(900, max(1, round(900 * size.height() / size.width()))))
        assert not image.isNull()
        pdf.close()


@pytest.mark.parametrize("content", ["image", "text"])
def test_canvas_expands_for_node_content_and_shrinks_on_undo(canvas, png_bytes, content):
    """图片与多行文本改变节点尺寸后扩展画布，撤销收缩、重做恢复，父子线仍在内容边界内。"""
    widget, a, b = canvas
    original = widget.view.sceneRect()
    if content == "image":
        widget.engine.attach_image(a, png_bytes, width=900)
    else:
        widget.engine.update_node(b, text="多行文本\n" * 30)
    expanded = widget.view.sceneRect()
    assert expanded.width() > original.width() or expanded.height() > original.height()
    for item in [
        *widget.node_items.values(),
        *widget.branch_items.values(),
    ]:
        assert widget.scene.sceneRect().contains(item.sceneBoundingRect())
    widget.engine.undo()
    assert widget.view.sceneRect() == original
    widget.engine.redo()
    assert widget.view.sceneRect() == expanded


def test_export_is_independent_of_canvas_navigation_margin(canvas, tmp_path):
    widget, _, _ = canvas
    original = tmp_path / "原始导出.png"
    resized = tmp_path / "调整画布后导出.png"
    widget.export(original)
    document = widget.engine.document
    bounds = widget.view.sceneRect()
    widget.resize(1400, 900)
    QApplication.processEvents()
    widget.view.zoom(0.001)
    widget.view.horizontalScrollBar().setValue(widget.view.horizontalScrollBar().maximum())
    assert widget.view.sceneRect() != bounds
    widget.export(resized)
    assert QImage(str(resized)) == QImage(str(original))
    assert widget.engine.document == document


def test_expanded_export_does_not_change_fold_or_editor(canvas, tmp_path):
    widget, a, _ = canvas
    widget.engine.set_expanded(a, False)
    widget.begin_edit(a)
    widget.editor.setPlainText("暂未提交")
    before = widget.engine.document
    widget.export(tmp_path / "展开.svg", expand_all=True)
    assert widget.engine.document == before
    assert widget.editor.toPlainText() == "暂未提交"


def test_image_import_roundtrip_and_invalid_asset(canvas, png_bytes, tmp_path):
    widget, a, b = canvas
    path = tmp_path / "图片.png"
    path.write_bytes(png_bytes)
    widget.import_image(a, path)
    widget.import_image(b, path)
    assert len(widget.engine.document.assets) == 1
    target = tmp_path / "含图片.mindmap"
    save_document(widget.engine.document, target)
    assert load_document(target) == widget.engine.document
    path.write_bytes(b"not an image")
    with pytest.raises(MindMapError):
        widget.import_image(a, path)


def test_async_image_download_uses_gui_commit(canvas, png_bytes, qtbot):
    widget, a, _ = canvas

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.end_headers()
            self.wfile.write(png_bytes)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with qtbot.waitSignal(widget.image_imported, timeout=4000):
            widget.import_image_url(a, f"http://127.0.0.1:{server.server_port}/image.png")
        assert widget.engine.get_node(a).image_id is not None
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_theme_and_custom_actions(canvas):
    widget, a, _ = canvas
    called = []
    action = widget.register_action(
        "自定义", "自定义动作", lambda w: called.append(w.current_node_id()), mutating=False
    )
    widget.select_nodes([a])
    action.trigger()
    assert called == [a]
    widget.engine.set_theme(Theme.dark())
    assert widget.engine.document.theme == Theme.dark()
    assert widget.layout_result.bounds.width > 0
    assert widget.engine.root_id in widget.node_items


def test_removed_actions_and_shortcuts_are_absent(canvas, monkeypatch, qtbot):
    """删除的功能不再注册动作或占用快捷键，空闲 Escape 不改变选择与视口。"""
    widget, a, b = canvas
    widget.select_nodes([a, b])
    menu_actions = []

    class InspectedMenu(QMenu):
        def exec(self, position):
            # 仍构造真实 Qt 动作列表，只跳过阻塞事件循环；PySide 的内建方法不宜直接替换。
            menu_actions.extend(self.actions())

    monkeypatch.setattr("mindmap_qt_canvas.widget.QMenu", InspectedMenu)
    widget.context_menu(widget.mapToGlobal(QPoint(20, 20)))
    actions = widget.actions() + menu_actions
    assert menu_actions
    assert not {"relation", "summary", "focus", "unfocus"} & widget._actions.keys()
    assert not hasattr(widget, "focus_node")
    assert not {"Ctrl+L", "Ctrl+G", "Ctrl+F", "Esc"} & {
        key.toString() for action in actions for key in action.shortcuts()
    }
    assert all("概要" not in action.text() and "连接两个" not in action.text() for action in actions)
    assert not {"聚焦此分支", "返回完整导图"} & {action.text() for action in menu_actions}
    assert not menu_actions[0].isSeparator() and not menu_actions[-1].isSeparator()
    assert not any(left.isSeparator() and right.isSeparator()
                   for left, right in zip(menu_actions, menu_actions[1:]))
    before, revision = widget.engine.document, widget.engine.revision
    transform = widget.view.transform()
    center = widget.view.mapToScene(widget.view.viewport().rect()).boundingRect().center()
    widget.view.setFocus()
    for key in (Qt.Key.Key_L, Qt.Key.Key_G, Qt.Key.Key_F):
        qtbot.keyClick(widget.view, key, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(widget.view, Qt.Key.Key_Escape)
    assert widget.engine.document == before and widget.engine.revision == revision
    assert widget.selected_node_ids() == [a, b] and widget.editor is None
    assert widget.view.transform() == transform
    assert widget.view.mapToScene(widget.view.viewport().rect()).boundingRect().center() == center
