"""内嵌页面入口的内容隔离、测量缓存和实际静态绘制回归。"""

from dataclasses import replace

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_render import VisualFactory, page_icon_body
from PySide6.QtCore import QBuffer, QIODevice, QRectF
from PySide6.QtGui import QColor, QImage, QPainter


@pytest.mark.parametrize("kinds,extra", [((), 0), (("detail",), 40), (("submap",), 40),
                                         (("detail", "submap"), 74)])
@pytest.mark.parametrize("decorated", [False, True])
def test_page_strip_reserves_space_without_squeezing_content(qapp, kinds, extra, decorated):
    """检查真实文字、图片与标签边界；仅附页入口产生额外宽度。"""
    engine = MindMapEngine()
    key = engine.add_child(engine.root_id, "编译原理、链接与运行时" * 4)
    engine.update_node(key, style={"width": 120}, note="备注", hyperlink="https://example.com")
    if decorated:
        image = QImage(90, 40, QImage.Format.Format_ARGB32)
        image.fill(QColor("#c98a39"))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(buffer, "PNG")
        engine.attach_image(key, bytes(buffer.data()))
        engine.update_node(key, tags=["标签一", "标签二", "长标签三"])
    document = engine.document
    node = document.nodes[key]
    factory = VisualFactory()
    base = factory.visual(node, document.theme, document)
    with_pages = replace(node, detail_page_id="详情" if "detail" in kinds else None,
                         submap_id="子导图" if "submap" in kinds else None)
    visual = factory.visual(with_pages, document.theme, document)
    assert visual.width == base.width + extra
    assert visual.text.textWidth() == base.text.textWidth()
    assert visual.image_rect == base.image_rect and visual.tags == base.tags
    assert visual.page_kinds == kinds
    content = [QRectF(visual.text_position.x(), visual.text_position.y(),
                     visual.text.size().width(), visual.text.size().height()),
               *(rect for _, rect in visual.tags)]
    if visual.image:
        content.append(visual.image_rect)
    rects = [visual.page_control_rect(kind) for kind in kinds]
    for rect in rects:
        assert rect.width() == rect.height() == 32
        assert QRectF(0, 0, visual.width, visual.height).contains(rect)
        assert all(not rect.intersects(bounds) for bounds in content)
    if rects:
        assert rects[-1].right() == visual.width - 14
        assert visual.height >= 36
    if len(rects) == 2:
        assert rects[0].right() + 2 == rects[1].left()
    assert visual.page_control_rect("main").isEmpty()
    # 具体 ID 不影响测量缓存，删除入口后则准确恢复原视觉。
    other_pages = replace(with_pages, detail_page_id="另一详情" if "detail" in kinds else None,
                          submap_id="另一子导图" if "submap" in kinds else None)
    assert factory.visual(other_pages, document.theme, document) is visual
    assert factory.visual(node, document.theme, document) is base


@pytest.mark.parametrize("theme", [Theme(), Theme.dark()])
@pytest.mark.parametrize("is_root,custom", [(False, False), (True, False), (False, True)])
def test_page_icons_stay_visible_and_can_be_disabled_independently(qapp, theme, is_root, custom):
    """以实际像素确认静态图标可见，关闭折叠标记不会隐藏内嵌页面图标。"""
    engine = MindMapEngine()
    document = engine.document
    node = replace(document.root, detail_page_id="详情", submap_id="子导图", text="短",
                   style={"font_size": 7, **({"background": "#3478d5", "text_color": "#ffffff"}
                                             if custom else {})})
    visual = VisualFactory().visual(node, theme, document, is_root=is_root)
    expected = visual.text_color if is_root or custom else QColor(theme.accent)
    assert visual.page_icon_color == expected
    assert visual.height == 36

    def painted(enabled):
        image = QImage(int(visual.width + 1), int(visual.height + 1), QImage.Format.Format_ARGB32)
        image.fill(visual.background)
        painter = QPainter(image)
        visual.paint(painter, node, controls=False, page_controls=enabled)
        painter.end()
        return image

    with_icons, without_icons = painted(True), painted(False)
    for kind in visual.page_kinds:
        rect = visual.page_control_rect(kind).adjusted(8, 8, -8, -8).toAlignedRect()
        assert with_icons.copy(rect) != without_icons.copy(rect)
    assert visual.text_position.y() >= 8


def test_shared_page_icon_rejects_unknown_type():
    """非法类别不应被静默画成另一种页面。"""
    with pytest.raises(ValueError, match="页面图标类别不存在"):
        page_icon_body("不存在")
