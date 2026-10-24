"""图片辅助效果只存在于画布，保留透明内容、导出和原有缩放手势。"""

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_render.items import NodeItem as StaticNodeItem
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QStyleOptionGraphicsItem


@pytest.fixture
def canvas(qtbot):
    """使用独立透明测试图片和引擎，不读取或改写任何用户导图。"""
    source = QImage(200, 120, QImage.Format.Format_ARGB32)
    source.fill(Qt.GlobalColor.transparent)
    painter = QPainter(source)
    painter.fillRect(4, 4, 192, 112, Qt.GlobalColor.white)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert source.save(buffer, "PNG")
    engine = MindMapEngine()
    key = engine.add_image_child(engine.root_id, bytes(buffer.data()), width=200)
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine, toolbar=False, embedded=True)
    qtbot.addWidget(widget)
    widget.resize(900, 650)
    widget.show()
    QApplication.processEvents()
    widget.select_nodes([])
    widget.view.centerOn(widget.node_items[key])
    QApplication.processEvents()
    yield widget, key
    widget.dispose()


def render_item(item, scale=1):
    """单独绘制图元到透明画布，保留外缘以检查投影，不借用系统屏幕。"""
    padding = 24
    image = QImage(round(item.visual.width * scale) + padding * 2,
                   round(item.visual.height * scale) + padding * 2, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.translate(padding, padding)
    painter.scale(scale, scale)
    item.paint(painter, QStyleOptionGraphicsItem())
    painter.end()
    return image


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scale", [0.5, 1, 2])
def test_shadow_separates_image_without_filling_transparent_content_or_changing_geometry(canvas, dark, scale):
    widget, key = canvas
    widget.set_ui_colors(ui_colors(Theme(background="#202124" if dark else "#ffffff")))
    item = widget.node_items[key]
    before, revision, layout = widget.engine.document, widget.engine.revision, widget.layout_result
    static = StaticNodeItem(item.node, item.visual, item.theme)
    actual, formal = render_item(item, scale), render_item(static, scale)
    outside = actual.pixelColor(24 - max(1, round(2 * scale)), 24 + round(60 * scale))
    assert 0 < outside.alpha() < 40
    assert (outside.red() > 128) is dark
    assert formal.pixelColor(24 - max(1, round(2 * scale)), 24 + round(60 * scale)).alpha() == 0
    assert actual.pixelColor(24 + round(2 * scale), 24 + round(60 * scale)).alpha() == 0
    assert actual.pixelColor(24 + round(100 * scale), 24 + round(60 * scale)) == Qt.GlobalColor.white
    assert item.boundingRect().contains(item.visual.image_rect.adjusted(-6, -4, 6, 8))
    assert not item.shape().contains(item.visual.image_rect.topLeft() - QPoint(1, 1))
    assert (item.visual.width, item.visual.height) == (200, 120)
    assert widget.engine.document == before and widget.engine.revision == revision
    assert widget.layout_result is layout


@pytest.mark.parametrize("readonly", [False, True])
def test_actual_hover_enters_and_leaves_without_selection_or_history(canvas, readonly):
    widget, key = canvas
    widget.set_editable(not readonly)
    view, item = widget.view, widget.node_items[key]
    before, revision = widget.engine.document, widget.engine.revision
    idle = render_item(item)
    center = view.mapFromScene(item.mapToScene(item.visual.image_rect.center()))
    view.update_cursor(center)
    QApplication.processEvents()
    assert item.hovered and render_item(item) != idle
    assert widget.selected_node_ids() == []
    QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.Leave))
    assert not item.hovered and render_item(item) == idle
    assert widget.engine.document == before and widget.engine.revision == revision


def test_selection_keeps_handles_and_does_not_overlay_hover_outline(canvas):
    widget, key = canvas
    widget.select_nodes([key])
    item, view = widget.node_items[key], widget.view
    selected = render_item(item)
    item.set_hovered(True)
    assert render_item(item) == selected
    rect = item.mapRectToScene(item.visual.image_rect)
    start = view.mapFromScene(rect.bottomRight())
    assert view.image_resize.hit_test(start) == 4
    before = widget.engine.document
    view.image_resize.begin(4, start)
    view.image_resize.update(start + QPoint(100, 60))
    assert view.image_resize.active and widget.engine.document == before
    assert item.opacity() == 0
    preview = QImage(900, 650, QImage.Format.Format_ARGB32)
    preview.fill(Qt.GlobalColor.transparent)
    painter = QPainter(preview)
    painter.setTransform(view.viewportTransform())
    view.image_resize.paint(painter)
    painter.end()
    preview_rect = view.viewportTransform().mapRect(view.image_resize._rect)
    y = round(preview_rect.top() + preview_rect.height() * 0.3)
    assert preview.pixelColor(round(preview_rect.left()) - 3, y).alpha() > 0
    assert preview.pixelColor(round(preview_rect.left()) + 2, y).alpha() == 0
    view.image_resize.cancel()
    assert item.opacity() == 1 and render_item(item) == selected
    widget.set_editable(False)
    assert view.image_resize.hit_test(start) is None


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_export_never_calls_canvas_decoration(canvas, suffix, tmp_path, monkeypatch):
    widget, key = canvas
    widget.select_nodes([key])
    widget.node_items[key].set_hovered(True)
    before, revision = widget.engine.document, widget.engine.revision

    def forbidden(*args):
        raise AssertionError("正式导出不得绘制画布投影或悬停效果")

    target = tmp_path / ("正式图片." + suffix)
    with monkeypatch.context() as guard:
        guard.setattr("mindmap_qt_canvas.items.paint_image_shadow", forbidden)
        guard.setattr("mindmap_qt_canvas.items.paint_image_hover", forbidden)
        guard.setattr("mindmap_qt_canvas.image_resizing.paint_image_shadow", forbidden)
        widget.export(target)
    assert target.stat().st_size > 0
    assert widget.engine.document == before and widget.engine.revision == revision


def test_legacy_attachment_still_uses_static_visual_without_shadow(canvas):
    widget, key = canvas
    node = widget.engine.get_node(key)
    legacy = widget.engine.add_child(widget.engine.root_id, "旧图文")
    widget.engine.attach_image(legacy, widget.engine.document.assets[node.image_id].data)
    widget.select_nodes([])
    item = widget.node_items[legacy]
    static = StaticNodeItem(item.node, item.visual, item.theme)
    assert render_item(item) == render_item(static)
    item.set_hovered(True)
    assert render_item(item) == render_item(static)


def test_type_change_updates_shadow_bounds_and_undo_restores_them(canvas):
    widget, key = canvas
    pure_bounds = widget.node_items[key].boundingRect()
    widget.engine.update_node(key, metadata={})
    item = widget.node_items[key]
    assert not item.node.is_image_node
    assert item.boundingRect() == StaticNodeItem(item.node, item.visual, item.theme).boundingRect()
    assert widget.engine.undo()
    assert widget.node_items[key].boundingRect() == pure_bounds
