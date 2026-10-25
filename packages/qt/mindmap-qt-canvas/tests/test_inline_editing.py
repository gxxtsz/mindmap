"""无边框原地编辑的真实排版、草稿隔离、输入法及大图回归。"""

import pytest
from mindmap_core import LayoutConfig, LayoutRegistry, MindMapDocument, MindMapEngine, Node, Theme
from mindmap_layouts import TreeLayout
from mindmap_qt_canvas import MindMapWidget
from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QInputMethodEvent, QPainter, QTextCursor
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionGraphicsItem


@pytest.fixture
def canvas(qtbot):
    """提供无历史的可见画布，所有草稿均在关闭前取消，避免跨用例的焦点回调。"""
    engine = MindMapEngine()
    child = engine.add_child(engine.root_id, "新节点")
    engine.add_child(child, "后代")
    engine.add_child(engine.root_id, "相邻节点")
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine, toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(1000, 700)
    widget.show()
    widget.activateWindow()
    QApplication.processEvents()
    yield widget, child
    widget.finish_edit(commit=False)
    widget.dispose()


def assert_editor_fits(canvas):
    """从实际图元坐标检查正文、圆角留白和布局尺寸，而非重复编辑器测量公式。"""
    editor = canvas.editor
    item = canvas.node_items[editor.node_id]
    visual = item.visual
    body = QRectF(item.pos(), item.pos() + QPointF(visual.width, visual.height))
    text = editor.sceneBoundingRect()
    assert body.adjusted(13, 7, -13, -7).contains(text)
    assert editor.pos() == item.pos() + visual.text_position
    rect = canvas.layout_result.nodes[editor.node_id]
    assert (rect.width, rect.height) == pytest.approx((visual.width, visual.height))


def test_short_text_uses_node_width_and_focus_does_not_draw_a_frame(canvas):
    widget, child = canvas
    widget.begin_edit(child)
    editor = widget.editor
    assert editor.textWidth() < 100
    assert editor.hasFocus() and editor.textCursor().hasSelection()
    assert widget.node_items[child].isSelected()
    assert_editor_fits(widget)

    def painted(focused):
        image = QImage(320, 160, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.translate(10, 10)
        option = QStyleOptionGraphicsItem()
        option.exposedRect = editor.boundingRect()
        if focused:
            option.state |= QStyle.StateFlag.State_HasFocus
        editor.paint(painter, option)
        painter.end()
        return bytes(image.constBits())

    # 验证实际绘制像素：焦点标志不会增加第二个框，但键盘焦点与选区仍保留。
    assert painted(True) == painted(False)
    assert editor.hasFocus() and editor.textCursor().hasSelection()


@pytest.mark.parametrize("zoom", [0.5, 0.75, 1, 1.15, 1.25, 1.5, 2, 4])
def test_mixed_text_keeps_geometry_and_painter_state_when_editing_at_different_zoom(canvas, zoom):
    """中英文及粗斜体进入、确认、取消编辑后保持换行，独立绘制不依赖视图提示。"""
    widget, child = canvas
    widget.engine.update_node(child, text="中英文 Prefill → Decode 0123，字体渲染保持清晰。",
                              style={"font_size": 11.5, "bold": True, "italic": True, "width": 160})
    visual = widget.node_items[child].visual
    expected_size = (visual.width, visual.height)
    expected_text_size = visual.text.size()
    widget.view.zoom(zoom / widget.view.transform().m11())
    widget.begin_edit(child)
    editor = widget.editor
    editor.sync_geometry(force=True)
    assert editor.font() == visual.font
    assert editor.document().size() == expected_text_size
    assert (editor.preview_visual.width, editor.preview_visual.height) == pytest.approx(expected_size)
    assert_editor_fits(widget)

    def painted(antialias):
        image = QImage(400, 160, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, antialias)
            before = (painter.renderHints(), painter.font(), painter.pen(), painter.transform())
            editor.paint(painter, QStyleOptionGraphicsItem())
            assert (painter.renderHints(), painter.font(), painter.pen(), painter.transform()) == before
        finally:
            painter.end()
        return image

    assert painted(False) == painted(True)
    widget.finish_edit(commit=True)
    actual = widget.node_items[child].visual
    assert (actual.width, actual.height) == pytest.approx(expected_size)
    widget.begin_edit(child)
    widget.editor.setPlainText("临时输入 ABC" * 8)
    widget.finish_edit(commit=False)
    assert widget.node_items[child].visual is actual


def test_zoom_reuses_existing_text_layouts_without_remeasurement(canvas, monkeypatch):
    """实际视图连续缩放、重绘及平移时复用正文缓存，不触发全图字体测量。"""
    from mindmap_qt_render import rendering

    widget, _ = canvas
    before = dict(widget._visuals)

    def unexpected(*_args, **_kwargs):
        pytest.fail("视图缩放不应重新测量文字")

    monkeypatch.setattr(rendering, "make_text", unexpected)
    for zoom in (0.1, 0.5, 0.75, 1, 1.15, 1.25, 1.5, 2, 4):
        widget.view.zoom(zoom / widget.view.transform().m11())
        QApplication.processEvents()
        assert not widget.view.viewport().grab().isNull()
        assert widget._visuals.keys() == before.keys()
        assert all(widget._visuals[key] is visual for key, visual in before.items())


@pytest.mark.parametrize("text", ["中文长文本自动换行" * 10, "unbrokenword" * 20])
@pytest.mark.parametrize("width", [120, 280])
def test_draft_wraps_resizes_and_cancel_restores_geometry(canvas, text, width):
    widget, child = canvas
    widget.engine.update_node(child, style={"width": width})
    widget.engine.replace_document(widget.engine.document)
    before, revision = widget.engine.document, widget.engine.revision
    layout = widget.layout_result
    nodes, branches = dict(layout.nodes), dict(layout.branches)
    sizes, visuals = dict(widget._measurements), dict(widget._visuals)
    widget.begin_edit(child)
    editor = widget.editor
    editor.setPlainText(text)
    assert editor.textWidth() == width
    assert editor.document().begin().layout().lineCount() > 1
    assert widget.node_items[child].visual.height > visuals[child].height
    assert dict(widget.layout_result.branches) != branches
    assert_editor_fits(widget)
    assert widget.engine.document == before and widget.engine.revision == revision
    assert not widget.engine.can_undo and not widget.engine.is_dirty
    assert widget._measurements == sizes and widget._visuals == visuals
    # 试算只能改变屏幕版本，正式索引和之前持有的布局快照仍与输入前一致。
    assert dict(layout.nodes) == nodes and dict(layout.branches) == branches
    assert dict(widget._incremental_layout.result().nodes) == nodes
    editor.setPlainText("短")
    assert editor.textWidth() < width
    assert_editor_fits(widget)
    widget.finish_edit(commit=False)
    assert widget.layout_result is layout
    assert dict(widget.layout_result.nodes) == nodes
    assert dict(widget.layout_result.branches) == branches
    assert widget.node_items[child].visual is visuals[child]
    assert widget.engine.document == before and not widget.engine.can_undo


def test_enter_commits_one_edit_and_undo_restores_size(canvas, qtbot):
    widget, child = canvas
    before = widget.engine.document
    initial = widget.node_items[child].visual
    widget.begin_edit(child)
    text = "输入变长后节点自动调整大小，提交只产生一次修改。" * 5
    widget.editor.setPlainText(text)
    preview = widget.node_items[child].visual
    qtbot.keyClick(widget.view.viewport(), Qt.Key.Key_Return)
    assert widget.editor is None and widget.engine.get_node(child).text == text
    actual = widget.node_items[child].visual
    assert (actual.width, actual.height) == (preview.width, preview.height)
    assert widget.engine.undo() and widget.engine.document == before
    assert not widget.engine.can_undo
    assert widget.node_items[child].visual is initial
    assert widget.engine.redo() and widget.engine.get_node(child).text == text


def test_editing_near_edge_pans_only_as_needed_and_tall_text_keeps_cursor_visible(canvas):
    widget, child = canvas
    widget.begin_edit(child)
    editor = widget.editor
    # 把编辑节点放在视口右边缘，模拟用户缩放和平移后直接进入原地编辑。
    widget.view.centerOn(editor.pos() - QPointF(350, 0))
    editor.setPlainText("靠近画布边缘的长文本应在输入时保持可见。" * 6)
    visible = widget.view.mapToScene(widget.view.viewport().rect()).boundingRect()
    assert visible.contains(widget.node_items[child].sceneBoundingRect())
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("\n超出视口的多行正文" * 80)
    editor.setTextCursor(cursor)
    cursor.insertText("继续输入")
    editor.setTextCursor(cursor)
    visible = widget.view.mapToScene(widget.view.viewport().rect()).boundingRect()
    caret = QRectF(editor.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle))
    assert visible.contains(editor.mapRectToScene(caret))


def test_shift_enter_empty_text_and_readonly_cancel(canvas, qtbot):
    widget, child = canvas
    before = widget.engine.document
    visual = widget.node_items[child].visual
    widget.begin_edit(child)
    widget.editor.setPlainText("第一行")
    cursor = widget.editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    widget.editor.setTextCursor(cursor)
    qtbot.keyClick(widget.view.viewport(), Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert widget.editor is not None and widget.editor.toPlainText().endswith("\n")
    assert_editor_fits(widget)
    widget.editor.setPlainText("")
    assert_editor_fits(widget)
    widget.set_editable(False)
    assert widget.editor is None and widget.node_items[child].visual is visual
    assert widget.engine.document == before


def test_ime_preedit_resizes_without_committing_and_escape_restores(canvas, qtbot):
    widget, child = canvas
    widget.engine.update_node(child, style={"width": 120})
    widget.engine.replace_document(widget.engine.document)
    before = widget.engine.document
    original = widget.node_items[child].visual
    widget.begin_edit(child)
    editor = widget.editor
    editor.setPlainText("😀")
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    widget.scene.sendEvent(editor, QInputMethodEvent("zhongwen" * 12, []))
    assert editor.composing and editor.toPlainText() == "😀"
    assert editor.textWidth() == 120
    assert widget.node_items[child].visual.height > original.height
    assert_editor_fits(widget)
    assert widget.engine.document == before
    widget.scene.sendEvent(editor, QInputMethodEvent())
    assert not editor.composing
    qtbot.keyClick(widget.view.viewport(), Qt.Key.Key_Escape)
    assert widget.editor is None and widget.node_items[child].visual is original
    assert widget.engine.document == before and not widget.engine.can_undo


@pytest.mark.parametrize("markdown", [False, True])
def test_decorations_follow_wrapped_text_without_mutating_shared_visual(canvas, markdown):
    widget, child = canvas
    image = QImage(100, 60, QImage.Format.Format_ARGB32)
    image.fill(QColor("#d58242"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    widget.engine.attach_image(child, bytes(buffer.data()))
    widget.engine.update_node(child, tags=["标签一", "标签二"], note="备注",
                              text_format="markdown" if markdown else "plain")
    before = widget.engine.document
    base = widget.node_items[child].visual
    tag_rects = [QRectF(rect) for _, rect in base.tags]
    widget.begin_edit(child)
    editor = widget.editor
    editor.setPlainText("**Markdown 源码与普通文字均在原节点内编辑**" * 10)
    assert_editor_fits(widget)
    preview = widget.node_items[child].visual
    assert preview.image_rect == base.image_rect and preview.image is base.image
    assert all(rect.top() > preview.text_position.y() + editor.document().size().height()
               for _, rect in preview.tags)
    assert all(rect.bottom() < preview.height - 20 for _, rect in preview.tags)
    assert [rect for _, rect in base.tags] == tag_rects
    assert widget.engine.document == before
    widget.finish_edit(commit=False)
    assert widget.node_items[child].visual is base


@pytest.mark.parametrize("text", ["短", "长标题与页面入口保持独立" * 12])
def test_inline_editing_keeps_both_internal_controls_and_commits_same_geometry(canvas, text):
    """长短草稿与正式测量共用页面按钮几何，候选和编辑状态不允许切页。"""
    widget, branch = canvas
    child = widget.engine.add_child(branch, "初始标题")
    widget.engine.create_detail_page(child)
    widget.engine.create_submap(child)
    widget.engine.update_node(child, style={"width": 120, "font_size": 7})
    widget.begin_edit(child)
    widget.editor.setPlainText(text)
    assert_editor_fits(widget)

    def assert_controls_fit():
        item = widget.node_items[child]
        body = QRectF(0, 0, item.visual.width, item.visual.height)
        editor = widget.editor.mapRectToItem(item, widget.editor.boundingRect())
        for control in (item.detail_page_control, item.submap_control):
            assert body.contains(control.boundingRect())
            assert not editor.intersects(control.boundingRect())
            assert not control.can_activate()

    assert_controls_fit()
    widget.scene.sendEvent(widget.editor, QInputMethodEvent("输入法候选" * 10, []))
    assert widget.is_composing
    assert_controls_fit()
    widget.scene.sendEvent(widget.editor, QInputMethodEvent())
    preview = widget.node_items[child].visual
    widget.finish_edit(commit=True)
    actual = widget.node_items[child].visual
    assert (actual.width, actual.height) == pytest.approx((preview.width, preview.height))
    assert actual.text_position == preview.text_position
    for kind in ("detail", "submap"):
        assert actual.page_control_rect(kind) == preview.page_control_rect(kind)


def test_custom_layout_and_navigation_temporary_display_support_preview(canvas, qtbot):
    widget, child = canvas
    widget.engine.set_expanded(widget.engine.root_id, False)
    widget.navigate_to(None, child)
    assert widget.focus_id == child
    before = widget.engine.document
    widget.begin_edit(child)
    widget.editor.setPlainText("导航临时显示范围内的长文本" * 12)
    assert_editor_fits(widget)
    widget.finish_edit(commit=False)
    assert widget.focus_id == child and widget.engine.document == before

    class TrackingLayout(TreeLayout):
        """保留第三方布局的正式注册入口，避免临时排版绕过宿主算法。"""

        def __init__(self):
            self.calls = 0

        def layout(self, *args, **kwargs):
            self.calls += 1
            return super().layout(*args, **kwargs)

    registry = LayoutRegistry()
    layout = TrackingLayout()
    registry.register(layout)
    other = MindMapWidget(layout_registry=registry)
    qtbot.addWidget(other)
    try:
        other.begin_edit(other.engine.root_id)
        calls = layout.calls
        other.editor.setPlainText("自定义布局同样调整节点和连接线" * 10)
        assert layout.calls > calls
        assert_editor_fits(other)
    finally:
        other.finish_edit(commit=False)
        other.dispose()


def test_virtual_canvas_keeps_draft_during_pan_without_scanning_document(qtbot, monkeypatch):
    document = MindMapDocument.new("大图编辑")
    document.layout = LayoutConfig(direction="right")
    document.theme = Theme.dark()
    for index in range(1100):
        node = Node(f"主题 {index}")
        document.nodes[node.id] = node
        document.root.children.append(node.id)
    widget = MindMapWidget(MindMapEngine(document), toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(900, 600)
    widget.show()
    QApplication.processEvents()
    child = document.root.children[550]

    class NoScanNodes(dict):
        """逐次按键只能查询所需节点；误用整页遍历立即使回归失败。"""

        def __iter__(self):
            raise AssertionError("输入期间不应扫描整页节点")

        def items(self):
            raise AssertionError("输入期间不应扫描整页节点")

        def values(self):
            raise AssertionError("输入期间不应扫描整页节点")

    try:
        assert widget._virtualized
        widget.select_nodes([child])
        widget.view.centerOn(QPointF(*widget.layout_result.nodes[child].center))
        widget.begin_edit(child)
        monkeypatch.setattr(widget._document, "nodes", NoScanNodes(widget._document.nodes))
        widget.editor.setPlainText("大图中的长文本实时换行" * 10)
        assert_editor_fits(widget)
        preview = widget.node_items[child].visual
        widget.view.verticalScrollBar().setValue(widget.view.verticalScrollBar().value() + 500)
        assert widget.node_items[child].visual is preview
        assert widget.editor is not None and len(widget.node_items) < 200
        widget.finish_edit(commit=False)
        assert widget.engine.document == document
    finally:
        widget.finish_edit(commit=False)
        widget.dispose()
