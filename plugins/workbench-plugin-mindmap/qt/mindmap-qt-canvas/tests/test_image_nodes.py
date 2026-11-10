"""纯图片显示、导入与真实视口缩放手势的回归验收。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.image_resizing import HANDLES
from mindmap_qt_panels import MindMapPanels
from PySide6.QtCore import QBuffer, QEvent, QIODevice, QMimeData, QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFocusEvent, QImage, QMouseEvent, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QApplication


def picture(width=200, height=120):
    """生成有透明外缘的临时 PNG，返回原图与独立字节，不接触用户文件。"""
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(4, 4, width - 8, height - 8, Qt.GlobalColor.red)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return image, bytes(buffer.data())


@pytest.fixture
def canvas(qtbot):
    engine = MindMapEngine()
    _, data = picture()
    key = engine.add_image_child(engine.root_id, data, width=200)
    other = engine.add_child(engine.root_id, "普通主题")
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine, toolbar=False)
    qtbot.addWidget(widget)
    widget.resize(1000, 720)
    widget.show()
    widget.activateWindow()
    widget.view.setFocus()
    QApplication.processEvents()
    widget.select_nodes([key])
    widget.view.centerOn(widget.node_items[key])
    yield widget, key, other
    widget.dispose()


def mouse(widget, kind, point, buttons=Qt.MouseButton.LeftButton):
    """通过真实视口事件路由发送按键状态明确的鼠标事件。"""
    button = (Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton)
    if kind == QEvent.Type.MouseButtonRelease:
        buttons = Qt.MouseButton.NoButton
    event = QMouseEvent(kind, QPointF(point), QPointF(widget.view.viewport().mapToGlobal(point)),
                        button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget.view.viewport(), event)


def handle_position(widget, key, index):
    item = widget.node_items[key]
    rect = item.mapRectToScene(item.visual.image_rect)
    x, y = HANDLES[index]
    point = QPointF(rect.center().x() + x * rect.width() / 2, rect.center().y() + y * rect.height() / 2)
    return widget.view.mapFromScene(point)


@pytest.mark.parametrize("scale", [0.5, 1, 2])
@pytest.mark.parametrize("index", range(8))
def test_all_eight_handles_keep_ratio_preview_and_single_undo(canvas, scale, index):
    widget, key, _ = canvas
    view, engine = widget.view, widget.engine
    view.zoom(scale)
    view.centerOn(widget.node_items[key])
    before, revision = engine.document, engine.revision
    layout, transform = widget.layout_result, view.transform()
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    start = handle_position(widget, key, index)
    x, y = HANDLES[index]
    end = start + QPoint(round(x * 100 * scale), round(y * 60 * scale))
    assert view.image_resize.hit_test(start) == index
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    assert view.image_resize.active and not view.node_drag.active
    mouse(widget, QEvent.Type.MouseMove, end)
    assert engine.document == before and engine.revision == revision
    assert widget.layout_result is layout
    assert view.image_resize._rect.width() / view.image_resize._rect.height() == pytest.approx(5 / 3)
    base, preview = view.image_resize._base, view.image_resize._rect
    anchor = QPointF(base.center().x() - x * base.width() / 2, base.center().y() - y * base.height() / 2)
    preview_anchor = QPointF(preview.center().x() - x * preview.width() / 2,
                            preview.center().y() - y * preview.height() / 2)
    assert (anchor - preview_anchor).manhattanLength() < 1e-7
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    assert not view.image_resize.active and widget.node_items[key].opacity() == 1
    assert engine.get_node(key).image_width == pytest.approx(300)
    assert engine.revision == revision + 1 and widget.selected_node_ids() == [key]
    assert view.transform() == transform and engine.document.assets == before.assets
    assert (view.mapToScene(view.viewport().rect()).boundingRect().center() - center).manhattanLength() <= 2 / scale
    assert engine.undo() and engine.document == before and not engine.can_undo
    assert engine.redo() and engine.get_node(key).image_width == pytest.approx(300)


@pytest.mark.parametrize("reason", ["escape", "zoom", "focus", "capture", "readonly", "change", "page", "dispose"])
def test_resize_cancellation_restores_original_visual_without_history(canvas, reason, qtbot):
    widget, key, other = canvas
    page = widget.engine.create_submap(other) if reason == "page" else None
    widget.select_nodes([key])
    start = handle_position(widget, key, 4)
    before, revision = widget.engine.document, widget.engine.revision
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    mouse(widget, QEvent.Type.MouseMove, start + QPoint(90, 54))
    assert widget.view.image_resize.active
    if reason == "escape":
        qtbot.keyClick(widget.view, Qt.Key.Key_Escape)
    elif reason == "zoom":
        widget.view.zoom(1.2)
    elif reason == "focus":
        QApplication.sendEvent(widget.view, QFocusEvent(QEvent.Type.FocusOut))
    elif reason == "capture":
        QApplication.sendEvent(widget.view.viewport(), QEvent(QEvent.Type.UngrabMouse))
    elif reason == "readonly":
        widget.set_editable(False)
    elif reason == "change":
        widget.engine.update_node(other, text="外部修改")
    elif reason == "page":
        widget.navigate_to(page)
    else:
        widget.dispose()
    assert not widget.view.image_resize.active
    assert widget.engine.get_node(key).image_width == 200
    assert widget.engine.revision == revision + (reason == "change")
    if reason != "change":
        assert widget.engine.document == before
    if key in widget.node_items:
        assert widget.node_items[key].opacity() == 1


def test_click_is_noop_and_shrinking_stops_at_one_pixel_short_edge(canvas):
    widget, key, _ = canvas
    before, revision = widget.engine.document, widget.engine.revision
    start = handle_position(widget, key, 3)
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    mouse(widget, QEvent.Type.MouseButtonRelease, start)
    assert widget.engine.document == before and widget.engine.revision == revision
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    end = start - QPoint(500, 0)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert widget.view.image_resize._rect.height() == pytest.approx(1)
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    assert widget.node_items[key].visual.height == pytest.approx(1)
    assert widget.engine.get_node(key).image_width == pytest.approx(5 / 3)


def test_body_drag_and_single_selection_handle_visibility(canvas):
    widget, key, other = canvas
    item = widget.node_items[key]
    center = widget.view.mapFromScene(item.mapRectToScene(item.visual.image_rect).center())
    mouse(widget, QEvent.Type.MouseButtonPress, center)
    mouse(widget, QEvent.Type.MouseMove, center + QPoint(30, 0))
    assert widget.view.node_drag.active and not widget.view.image_resize.active
    widget.view.cancel_interaction()
    start = handle_position(widget, key, 0)
    widget.select_nodes([key, other])
    assert widget.view.image_resize.hit_test(start) is None
    widget.select_nodes([key])
    widget.set_editable(False)
    assert widget.view.image_resize.hit_test(start) is None


def test_ime_guard_preserves_property_draft_and_prevents_resize(canvas):
    widget, key, _ = canvas
    panels = MindMapPanels(widget)
    try:
        field = panels.properties.fields["image_width"]
        field.setValue(321)
        panels.properties.timer.stop()
        panels.properties._composing.add("image_width")
        before, revision = widget.engine.document, widget.engine.revision
        errors = []
        widget.error_occurred.connect(errors.append)
        mouse(widget, QEvent.Type.MouseButtonPress, handle_position(widget, key, 0))
        assert errors and "输入法" in errors[0]
        assert not widget.view.image_resize.active
        assert widget.engine.document == before and widget.engine.revision == revision
        assert field.value() == 321 and panels.properties._pending
    finally:
        panels.dispose()


@pytest.mark.parametrize("operation", ["create_detail_page", "create_submap"])
def test_image_page_restriction_preserves_pending_property_draft(canvas, operation):
    widget, key, _ = canvas
    panels = MindMapPanels(widget)
    try:
        field = panels.properties.fields["image_width"]
        field.setValue(321)
        panels.properties.timer.stop()
        before, revision = widget.engine.document, widget.engine.revision
        with pytest.raises(MindMapError, match="纯图片"):
            getattr(widget, operation)()
        assert widget.engine.document == before and widget.engine.revision == revision
        assert widget.selected_node_ids() == [key] and field.value() == 321
        assert panels.properties._pending
    finally:
        panels.dispose()


def test_export_uses_formal_size_during_resize_and_omits_handles(canvas, tmp_path):
    widget, key, _ = canvas
    before = widget.engine.document
    first = tmp_path / "缩放前.png"
    widget.export(first)
    rendered = QImage(str(first))
    start = handle_position(widget, key, 4)
    end = start + QPoint(100, 60)
    mouse(widget, QEvent.Type.MouseButtonPress, start)
    mouse(widget, QEvent.Type.MouseMove, end)
    assert widget.view.image_resize.active
    preview = tmp_path / "预览期间.png"
    widget.export(preview)
    assert QImage(str(preview)) == rendered
    assert widget.view.image_resize.active and widget.engine.document == before
    mouse(widget, QEvent.Type.MouseButtonRelease, end)
    after = tmp_path / "缩放后.png"
    widget.export(after)
    assert QImage(str(after)) != rendered
    assert widget.engine.document.assets == before.assets


def test_legacy_image_attachment_keeps_caption_pages_and_remove_action(canvas):
    widget, _, other = canvas
    _, data = picture()
    widget.engine.attach_image(other, data)
    widget.select_nodes([other])
    visual = widget.node_items[other].visual
    assert not visual.image_only and visual.width >= 160
    assert widget.action("edit").isEnabled() and widget.action("remove_image").isVisible()
    assert widget.action("create_submap").isVisible() and widget.action("create_detail_page").isVisible()
    detail = widget.engine.create_detail_page(other)
    submap = widget.engine.create_submap(other)
    assert widget.engine.get_node(other).text == "普通主题"
    widget.action("remove_image").trigger()
    node = widget.engine.get_node(other)
    assert node.image_id is None and node.text == "普通主题"
    assert node.detail_page_id == detail and node.submap_id == submap


def test_image_display_has_no_caption_background_border_or_extra_markers(canvas, qtbot):
    widget, key, other = canvas
    widget.engine.update_node(key, tags=["不绘制标签"], icons=["图标"], note="备注", hyperlink="https://example.com",
                              style={"background": "#00ff00", "border_width": 10, "width": 40})
    item = widget.node_items[key]
    visual = item.visual
    assert visual.image_only and (visual.width, visual.height) == (200, 120)
    assert visual.image_rect == QRectF(0, 0, 200, 120)
    rendered = QImage(200, 120, QImage.Format.Format_ARGB32)
    rendered.fill(Qt.GlobalColor.transparent)
    painter = QPainter(rendered)
    visual.paint(painter, item.node, selected=False, controls=False)
    painter.end()
    assert rendered.pixelColor(0, 0).alpha() == 0
    assert rendered.pixelColor(100, 60) == Qt.GlobalColor.red
    widget.begin_edit(key)
    assert widget.editor is None and not widget.action("edit").isEnabled()
    assert not widget.action("create_submap").isVisible()
    assert not widget.action("create_detail_page").isVisible()
    assert not widget.action("remove_image").isVisible()
    panels = MindMapPanels(widget)
    try:
        assert panels.outline.model().data(panels.outline.model().index_for(key)) == "图片"
        assert panels.properties.summary.text() == "图片"
        assert not panels.properties.fields["style.font_size"].isEnabled()
        assert not panels.properties.fields["style.border_width"].isEnabled()
        assert not panels.properties.width_enabled.isEnabled()
        panels.properties.fields["image_width"].setValue(1800)
        panels.properties.flush()
        assert widget.engine.get_node(key).image_width == 1800
        assert panels.properties.summary.text() == "图片"
        widget.select_nodes([key, other])
        assert not panels.properties.fields["style.font_size"].isEnabled()
        widget.select_nodes([other])
        assert panels.properties.fields["style.font_size"].isEnabled()
    finally:
        panels.dispose()


@pytest.mark.parametrize("source", ["local", "url"])
@pytest.mark.parametrize("page_kind", ["main", "submap", "detail"])
def test_import_creates_child_at_original_large_pixel_size(canvas, tmp_path, monkeypatch, source, page_kind):
    widget, _, other = canvas
    if page_kind != "main":
        page = getattr(widget.engine, "create_submap" if page_kind == "submap" else "create_detail_page")(other)
        widget.navigate_to(page)
    target = widget.current_node_id()
    image, data = picture(1600, 900)
    path = tmp_path / "原始尺寸.png"
    path.write_bytes(data)
    QApplication.processEvents()
    transform = widget.view.transform()
    parent = widget.engine.get_node(target)
    if source == "local":
        widget.import_image(target, path)
    else:
        reply = Reply(data, widget.network)
        monkeypatch.setattr(widget.network, "get", lambda request: reply)
        widget.import_image_url(target, "https://example.com/image.png")
        reply.finished.emit()
    created = widget.selected_node_ids()[0]
    node, visual = widget.engine.get_node(created), widget.node_items[created].visual
    assert widget.engine.parent_id(created) == target and node.is_image_node
    assert node.image_width == 1600 and (visual.width, visual.height) == (1600, 900)
    assert widget.engine.get_node(target).text == parent.text
    assert widget.view.transform() == transform
    assert widget.engine.document.assets[node.image_id].data == data
    assert image.pixelColor(0, 0).alpha() == 0


@pytest.mark.parametrize("pixmap", [False, True])
def test_high_dpr_clipboard_uses_actual_pixel_dimensions(canvas, pixmap):
    widget, _, other = canvas
    image, _ = picture()
    image.setDevicePixelRatio(2)
    value = QPixmap.fromImage(image) if pixmap else image
    mime = QMimeData()
    mime.setImageData(value)
    QApplication.clipboard().setMimeData(mime)
    widget.select_nodes([other])
    key, = widget.paste()
    visual = widget.node_items[key].visual
    assert widget.engine.get_node(key).image_width == 200
    assert (visual.width, visual.height) == (200, 120)
    assert visual.image.toImage().pixelColor(0, 0).alpha() == 0


def test_submap_owner_rejects_image_before_committing_text_or_property_draft(canvas):
    widget, _, other = canvas
    widget.engine.create_submap(other)
    widget.select_nodes([other])
    widget.begin_edit(other)
    widget.editor.setPlainText("仍在编辑")
    before, revision = widget.engine.document, widget.engine.revision
    image, _ = picture()
    mime = QMimeData()
    mime.setImageData(image)
    QApplication.clipboard().setMimeData(mime)
    with pytest.raises(MindMapError, match="子导图"):
        widget.paste()
    assert widget.engine.document == before and widget.engine.revision == revision
    assert widget.editor.toPlainText() == "仍在编辑"


class Reply(QObject):
    """同步控制完成时机的临时网络回复，不发出网络请求。"""

    finished = Signal()
    downloadProgress = Signal(int, int)

    def __init__(self, data, parent):
        super().__init__(parent)
        self.data = data
        self.aborted = False

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def readAll(self):
        return self.data

    def abort(self):
        self.aborted = True


def test_url_import_keeps_original_parent_after_navigation(canvas, monkeypatch):
    widget, key, other = canvas
    page = widget.engine.create_submap(other)
    _, data = picture()
    reply = Reply(data, widget.network)
    monkeypatch.setattr(widget.network, "get", lambda request: reply)
    assert widget.import_image_url(key, "https://example.com/image.png") is reply
    widget.navigate_to(page)
    selection, transform = widget.selected_node_ids(), widget.view.transform()
    imported = []
    widget.image_imported.connect(imported.append)
    reply.finished.emit()
    assert len(imported) == 1 and widget.engine.parent_id(imported[0]) == key
    assert widget.current_page_id == page and widget.selected_node_ids() == selection
    assert widget.view.transform() == transform and not widget._downloads


@pytest.mark.parametrize("reason", ["submap", "deleted", "readonly", "reset", "dispose", "ime"])
def test_url_completion_rechecks_target_and_lifecycle(canvas, reason, monkeypatch):
    widget, _, other = canvas
    _, data = picture()
    reply = Reply(data, widget.network)
    monkeypatch.setattr(widget.network, "get", lambda request: reply)
    widget.import_image_url(other, "https://example.com/image.png")
    if reason == "submap":
        widget.engine.create_submap(other)
    elif reason == "deleted":
        widget.engine.remove_nodes([other])
    elif reason == "readonly":
        widget.set_editable(False)
    elif reason == "reset":
        widget.engine.replace_document(widget.engine.document)
    elif reason == "dispose":
        widget.dispose()
    else:
        widget.register_navigation_guard(lambda: False)
    before, revision = widget.engine.document, widget.engine.revision
    reply.finished.emit()
    assert widget.engine.document == before and widget.engine.revision == revision
    assert not widget._downloads
