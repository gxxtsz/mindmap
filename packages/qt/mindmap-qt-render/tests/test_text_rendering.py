"""独立绘制的抗锯齿、绘图状态隔离及跨倍率文本布局回归。"""

from dataclasses import replace
from math import ceil
from types import SimpleNamespace

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_render import VisualFactory, rendering
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen


def painter_state(painter):
    """记录调用方可观察的状态，防止字体、调色板或抗锯齿泄漏到相邻图元。"""
    return (
        painter.renderHints(), painter.font(), painter.pen(), painter.brush(),
        painter.transform(), painter.opacity(), painter.compositionMode(),
    )


def sample_visual(dark=False, markdown=False):
    """提供包含正文、标签、备注和折叠数字的完整节点，不依赖应用外壳。"""
    engine = MindMapEngine()
    engine.add_child(engine.root_id, "后代")
    engine.set_expanded(engine.root_id, False)
    engine.update_node(
        engine.root_id,
        text="**中英文 Prefill 0123**，`token_id`" if markdown else "中英文 Prefill → Decode 0123",
        text_format="markdown" if markdown else "plain", tags=["中文标签 ABC"], note="备注",
        style={"font_family": "Microsoft YaHei UI", "font_size": 11.5, "bold": True, "italic": True},
    )
    document = engine.document
    theme = Theme.dark() if dark else Theme()
    factory = VisualFactory()
    node = document.root
    return factory, node, theme, document, factory.visual(node, theme, document)


@pytest.mark.parametrize("operation", ["node", "fold", "text", "image"])
def test_independent_painting_matches_antialiased_reference_and_restores_state(qapp, operation):
    """调用方关闭抗锯齿时仍得到相同像素，且纯图片提前返回也不泄漏状态。"""
    _, node, _, _, visual = sample_visual()
    if operation == "image":
        visual = replace(visual, image_only=True)

    def painted(antialias):
        image = QImage(600, 260, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        try:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing,
                                   antialias)
            painter.setFont(QFont("Serif", 17))
            painter.setPen(QPen(QColor("#cc3300"), 3))
            painter.setBrush(QColor("#aaddee"))
            painter.setOpacity(0.85)
            painter.translate(20.5, 20.25)
            painter.scale(1.15, 1.15)
            before = painter_state(painter)
            if operation == "fold":
                visual.paint_fold_control(painter, node)
            elif operation == "text":
                rendering.draw_text(painter, visual.text, QPointF(14, 8), visual.text_color)
            else:
                visual.paint(painter, node, selected=operation == "image")
            assert painter_state(painter) == before
        finally:
            painter.end()
        return image

    assert painted(False) == painted(True)


@pytest.mark.parametrize("operation", ["node", "text"])
def test_failed_text_draw_restores_callers_painter_state(qapp, monkeypatch, operation):
    """自定义文本布局绘制失败时，恢复变换与绘图提示后继续向外传播异常。"""
    _, node, _, _, visual = sample_visual()

    def fail(*_args):
        raise RuntimeError("模拟文本布局绘制失败")

    monkeypatch.setattr(visual.text, "documentLayout", lambda: SimpleNamespace(draw=fail))
    image = QImage(600, 260, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        painter.translate(17.5, 11.25)
        before = painter_state(painter)
        with pytest.raises(RuntimeError, match="模拟文本布局绘制失败"):
            if operation == "node":
                visual.paint(painter, node)
            else:
                rendering.draw_text(painter, visual.text, QPointF(14, 8), visual.text_color)
        assert painter_state(painter) == before
    finally:
        painter.end()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("markdown", [False, True])
def test_cached_text_layout_is_stable_across_dpi_and_zoom(qapp, monkeypatch, dark, markdown):
    """同一文档在不同目标像素密度绘制时保持换行和布局，禁止暗中重建正文缓存。"""
    factory, node, theme, document, visual = sample_visual(dark, markdown)
    assert visual.font.family() == node.style["font_family"]
    assert visual.font.pointSizeF() == node.style["font_size"]
    assert visual.font.bold() and visual.font.italic()

    def layout_signature():
        block, lines = visual.text.begin(), []
        while block.isValid():
            layout = block.layout()
            lines.extend((layout.lineAt(i).textStart(), layout.lineAt(i).textLength(),
                          layout.lineAt(i).naturalTextWidth()) for i in range(layout.lineCount()))
            block = block.next()
        return visual.text.size(), tuple(lines), visual.width, visual.height

    expected = layout_signature()

    def unexpected(*_args, **_kwargs):
        pytest.fail("缩放和跨设备绘制不应重新排版缓存正文")

    monkeypatch.setattr(rendering, "make_text", unexpected)
    for dpr in (1, 1.25, 1.5, 2):
        for zoom in (0.1, 0.5, 0.75, 1, 1.15, 1.25, 1.5, 2, 4):
            image = QImage(ceil((visual.width + 40) * zoom * dpr),
                           ceil((visual.height + 16) * zoom * dpr), QImage.Format.Format_RGB32)
            image.setDevicePixelRatio(dpr)
            image.fill(QColor(theme.background))
            empty = image.copy()
            painter = QPainter(image)
            try:
                painter.scale(zoom, zoom)
                painter.translate(8, 8)
                visual.paint(painter, node)
            finally:
                painter.end()
            assert image != empty
            assert layout_signature() == expected
            assert factory.visual(node, theme, document) is visual
