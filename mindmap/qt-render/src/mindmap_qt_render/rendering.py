"""文字与图片测量、绘制缓存及图形导出共用的视觉数据。"""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from mindmap_core import BranchPath, Curve, MindMapDocument, MindMapError, Node, Theme
from mindmap_core.models import SVG_MEDIA_TYPE, Asset
from mindmap_core.node_pages import GRAPH_PAGE_TYPE
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImageReader,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPalette,
    QPen,
    QPixmap,
    QTextDocument,
    QTextOption,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QGraphicsPathItem

from .markdown_code import style_code
from .markdown_math import insert_formulas, mark_formulas
from .page_icons import paint_page_icon
from .svg import load_svg

# 节点内容区上下各保留 8 个逻辑像素；正式测量、编辑草稿与导出共用此留白。
_NODE_VERTICAL_PADDING = 8.0
NODE_HORIZONTAL_PADDING = 14.0
DEFAULT_TEXT_WIDTH = 280.0
NODE_CARD_RADIUS = 7.0
_MARKDOWN_FEATURES = (
    QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML
)

PAGE_CONTROL_SIZE = 32.0
PAGE_CONTROL_GAP = 2.0
PAGE_CONTENT_GAP = 8.0
PAGE_RIGHT_PADDING = 14.0
# 保留 32×32 的入口命中区域，上下各留 2 个逻辑像素，适配更紧凑的节点。
PAGE_MIN_HEIGHT = 36.0
_FAVORITE_BORDER_WIDTH = 2.0


def _relative_luminance(color: QColor) -> float:
    """将 sRGB 色值换算为计算文字对比度所用的相对亮度。"""
    rgb = color.toRgb().getRgbF()[:3]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
              for channel in rgb]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _favorite_card_colors(theme: Theme, text: QColor) -> tuple[QColor, QColor, QColor]:
    """根据画布明暗选择收藏填充和描边，原正文色达到阅读对比度时继续使用。

    半透明正文按实际叠加在填充色上的结果计算。返回值仅用于本次绘制，
    取消收藏后重新从节点和页面主题读取原来的外观。
    """
    if QColor(theme.background).lightnessF() < 0.5:
        fill, border, fallback = QColor("#493414"), QColor("#f4b84c"), QColor("#fff2cf")
    else:
        fill, border, fallback = QColor("#fff1c2"), QColor("#a86308"), QColor("#2b2115")
    if not text.isValid():
        return fill, border, fallback
    alpha = text.alphaF()
    foreground = QColor.fromRgbF(*(channel * alpha + base * (1 - alpha)
                                    for channel, base in zip(text.toRgb().getRgbF()[:3], fill.getRgbF()[:3])))
    low, high = sorted((_relative_luminance(foreground), _relative_luminance(fill)))
    return fill, border, text if (high + 0.05) / (low + 0.05) >= 4.5 else fallback


def ensure_fonts() -> None:
    """补齐 Windows 离屏插件的空字体库，不改变正常桌面宿主的字体设置。

    Qt 的 Windows offscreen 插件可能返回零种字体。仅在此情况下，从当前系统
    读取中文和等宽字体并注册到本进程。注册只在本进程的字体库内生效。
    """
    if QApplication.instance() is None:
        raise MindMapError("创建图形组件或导出前必须先创建 QApplication")
    if QFontDatabase.families():
        return
    if sys.platform == "win32":
        directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        family = None
        for filename in ("msyh.ttc", "msyhbd.ttc", "simsun.ttc", "consola.ttf"):
            path = directory / filename
            if path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(path))
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families and family is None:
                    family = families[0]
        if family:
            QApplication.setFont(QFont(family, 9))
    if not QFontDatabase.families():
        raise MindMapError("当前 Qt 环境没有可用字体，请安装字体或使用系统桌面平台")


class _TextDocument(QTextDocument):
    """节点 Markdown 的资源读取由显式图片导入负责，测量时不隐式读取外部文件。"""

    def loadResource(self, resource_type, name):
        return None


def text_from_html(html: str, *, markdown: bool, source: str | None = None) -> str:
    """从剪贴板 HTML 提取可保存的节点正文，在 Qt GUI 线程同步返回字符串。

    markdown 为真时将网页标题、代码、表格和公式注解等转为节点源码。网页已有
    排版结构时以该结构为准。对于编辑器附带的简单 HTML，若 source 与其可见文字
    一致且含 Markdown 语法，则保留 source。纯文本模式提取可见正文。处理过程
    在进程内完成，返回的源码由调用者持有。
    """
    ensure_fonts()
    if markdown:
        from bs4 import BeautifulSoup

        from .markdown_clipboard import clipboard_html_to_markdown, has_rendered_structure

        soup = BeautifulSoup(html, "html.parser")
        if not source or has_rendered_structure(soup):
            return clipboard_html_to_markdown(soup)
    document = _TextDocument()
    document.setLayoutEnabled(False)
    document.setHtml(html)
    if not markdown:
        return document.toPlainText()
    words = source.split()
    if words == document.toPlainText().split():
        marked, _ = mark_formulas(source)
        parsed = _TextDocument()
        parsed.setLayoutEnabled(False)
        parsed.setMarkdown(marked, _MARKDOWN_FEATURES)
        if words != parsed.toPlainText().split():
            return source
    return clipboard_html_to_markdown(soup)


def make_text(
    text: str, font: QFont, width: float, *, markdown=False,
    foreground: QColor | None = None, background: QColor | None = None,
) -> QTextDocument:
    """创建按逻辑像素排版的独立文本，供节点缓存、拖动预览及导出共享。

    text 为正文或 Markdown 源码，font 为工厂已配置抗锯齿和像素对齐策略的
    字体，width 为不含节点留白的正文宽度，-1 表示先测自然宽度。markdown 为真时
    使用 GitHub Markdown、禁用 HTML。代码和公式按 foreground、background 取色，
    默认使用黑字白底。代码块可以折行，公式按逻辑尺寸参与节点排版。
    使用字体设计度量计算换行，避免绘制倍率和设备像素取整影响缓存布局。
    返回的 QTextDocument 由调用方在 Qt GUI 线程持有，发布进视觉缓存后只读使用。
    """
    document = _TextDocument()
    document.setLayoutEnabled(False)
    document.setUndoRedoEnabled(False)
    document.setDocumentMargin(0)
    document.setDefaultFont(font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    option.setUseDesignMetrics(True)
    document.setDefaultTextOption(option)
    if markdown:
        source, formulas = mark_formulas(text)
        document.setMarkdown(source, _MARKDOWN_FEATURES)
        foreground = foreground or QColor("black")
        if any(marker in text for marker in ("`", "~~~", "    ", "\t")):
            style_code(document, background or QColor("white"), foreground)
        insert_formulas(document, formulas, font, foreground)
    else:
        document.setPlainText(text)
    document.setTextWidth(width)
    document.setLayoutEnabled(True)
    return document


def draw_text(painter: QPainter, document: QTextDocument, position: QPointF, color: QColor) -> None:
    """在局部坐标绘制缓存文本，显式开启文字抗锯齿并使用指定的文本颜色。

    painter 必须处于活动状态，其变换负责画布倍率和设备像素密度；position
    为正文相对节点的逻辑坐标，color 提供明暗主题下的默认文本调色板。
    不改变 document 的布局、宽度或格式，也不将文字预先转成位图。
    正常完成或绘制异常时均恢复调用方的变换、字体及绘图提示；异常继续向外传播。
    """
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.translate(position)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, color)
        context.palette.setColor(QPalette.ColorRole.WindowText, color)
        document.documentLayout().draw(painter, context)
    finally:
        painter.restore()


def decode_image(data: bytes) -> tuple[QPixmap, str]:
    """验证编码图片字节，返回独立的可绘制 QPixmap 及实际图片 MIME。

    data 是完整图片内容，不读取路径或网络地址；调用方须在已有 QApplication 的
    GUI 线程使用。图片尺寸无效、超过四千万像素或无法解码时抛出 MindMapError，
    不修改输入、文档或历史。临时缓冲区仅在调用期间存活，返回值可由调用方持有。
    """
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > 40_000_000:
        raise MindMapError("图片无效或像素数超过四千万")
    # 部分 Qt 图像插件在 read() 后不再保留检测结果，须在读取前取得真实格式。
    kind = bytes(reader.format()).decode("ascii", "replace")
    image = reader.read()
    if image.isNull():
        raise MindMapError(f"图片无法解码：{reader.errorString()}")
    return QPixmap.fromImage(image), "image/" + ("jpeg" if kind in ("jpg", "jpeg") else kind)


@dataclass
class NodeVisual:
    """以逻辑像素描述节点内容和内嵌入口；工厂发布后只读共享。

    页面字段提供默认值以兼容旧构造；入口矩形由完整宽高计算，不保存与
    具体节点或页面 ID 绑定的控件。content_height 保留居中前的内容尺寸。
    """
    width: float
    height: float
    text: QTextDocument
    text_position: QPointF
    text_color: QColor
    background: QColor
    border: QColor
    border_width: float
    image: QPixmap | QSvgRenderer | None
    image_rect: QRectF
    tags: list[tuple[str, QRectF]]
    tag_font: QFont
    font: QFont
    has_children: bool
    # 只保存页面类别，绝不把具体页面 ID 放入可跨节点共享的视觉缓存。
    page_kinds: tuple[str, ...] = ()
    page_icon_color: QColor | None = None
    # 记录最小高度居中前的内容高度，使编辑草稿增减行数时能准确恢复留白。
    content_height: float = 0.0
    # 两个默认值兼容旧视觉构造接口，收藏纯图片在相同画幅内增加常驻窄框。
    image_only: bool = False
    favorite: bool = False

    def paint_image(self, painter: QPainter, rect: QRectF | None = None) -> None:
        """共用正式绘制和尺寸预览，收藏的纯图片在目标矩形内保留等比图像和窄框。"""
        bounds = self.image_rect if rect is None else rect
        if self.image_only and self.favorite and not bounds.isEmpty():
            side = min(bounds.width(), bounds.height())
            edge = min(self.border_width, side / 8)
            painter.fillRect(bounds, self.border)
            painter.fillRect(bounds.adjusted(edge, edge, -edge, -edge), self.background)
            scale = 1 - 2 * min(4.0, side / 4) / side
            dx, dy = bounds.width() * (1 - scale) / 2, bounds.height() * (1 - scale) / 2
            bounds = bounds.adjusted(dx, dy, -dx, -dy)
        if isinstance(self.image, QSvgRenderer):
            painter.save()
            try:
                painter.setClipRect(bounds, Qt.ClipOperation.IntersectClip)
                self.image.render(painter, bounds)
            finally:
                painter.restore()
        elif self.image is not None:
            painter.drawPixmap(bounds, self.image, QRectF(self.image.rect()))

    @property
    def page_controls_width(self) -> float:
        """返回入口增加的完整宽度；无入口时为零，原正文宽度不受影响。"""
        count = len(self.page_kinds)
        if not count or self.image_only:
            return 0.0
        return PAGE_CONTENT_GAP + count * PAGE_CONTROL_SIZE + (count - 1) * PAGE_CONTROL_GAP

    @property
    def content_offset(self) -> float:
        """返回最小高度产生的垂直偏移，兼容未指定 content_height 的旧构造。"""
        return (self.height - self.content_height) / 2 if self.content_height else 0.0

    def with_content_size(self, width: float, height: float) -> NodeVisual:
        """根据不含入口的内容尺寸生成新视觉，统一正式测量与原地编辑几何。

        width、height 包含原有正文、图片、标签及其留白，但不含页面按钮。
        返回值额外加入按钮区，并在有入口时保证至少 36 个逻辑像素的高度；
        内容整体居中，原对象及共享的文字文档不被修改，偏移不会重复累加。
        """
        total_height = max(height, PAGE_MIN_HEIGHT) if self.page_kinds else height
        offset = (total_height - height) / 2 - self.content_offset
        return replace(
            self, width=width + self.page_controls_width, height=total_height, content_height=height,
            text_position=self.text_position + QPointF(0, offset),
            image_rect=self.image_rect.translated(0, offset),
            tags=[(value, rect.translated(0, offset)) for value, rect in self.tags],
        )

    def page_control_rect(self, kind: str) -> QRectF:
        """返回节点内部的入口命中矩形；缺少该页面时返回空矩形。

        页面按钮按详情、子导图、引用的顺序排列；按钮为 32×32，间隔为 2，
        最右侧保留 14 像素，与节点朝向无关。测量、绘制与命中共用此坐标。
        """
        if kind not in self.page_kinds:
            return QRectF()
        if self.image_only:
            side = min(PAGE_CONTROL_SIZE, self.width, self.height)
            margin = min(4.0, (min(self.width, self.height) - side) / 2)
            return QRectF(self.width - side - margin, margin, side, side)
        count = len(self.page_kinds)
        strip = count * PAGE_CONTROL_SIZE + (count - 1) * PAGE_CONTROL_GAP
        x = self.width - PAGE_RIGHT_PADDING - strip
        x += self.page_kinds.index(kind) * (PAGE_CONTROL_SIZE + PAGE_CONTROL_GAP)
        return QRectF(x, (self.height - PAGE_CONTROL_SIZE) / 2, PAGE_CONTROL_SIZE, PAGE_CONTROL_SIZE)

    def paint_page_control(self, painter: QPainter, kind: str) -> None:
        """在入口中心绘制 16×16 图形；不绘制悬停底色，也不注册导航行为。"""
        rect = self.page_control_rect(kind)
        if not rect.isEmpty():
            inset = rect.width() / 4
            color = self.page_icon_color if self.page_icon_color is not None else self.text_color
            if self.image_only:
                painter.save()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(24, 33, 47, 210))
                painter.drawRoundedRect(rect, 5, 5)
                painter.restore()
                color = QColor("white")
            paint_page_icon(painter, kind, rect.adjusted(inset, inset, -inset, -inset), color)

    def card_rect(self) -> QRectF:
        """返回节点局部坐标中的实际绘制矩形，普通卡片以此为圆角边框中心线。

        纯图片的矩形等于图片绘制范围。返回值供正文绘制、外框和遮挡路径共用，
        Qt 图元的重绘边界另行保留抗锯齿余量。
        """
        return QRectF(self.image_rect) if self.image_only else QRectF(1, 1, self.width - 2, self.height - 2)

    def card_outline(self, *, pixel_size: float) -> QPainterPath:
        """返回覆盖普通卡片填充和正式边框的圆角路径，纯图片使用绘制矩形。

        路径使用节点局部坐标。pixel_size 是当前设备的一个物理像素折合的局部长度，
        用于宽度为零的 Qt 发丝边框。调用方用同一画布变换映射到视口后裁剪前景。
        返回独立路径，不修改视觉、命中范围或文档。
        """
        rect = self.card_rect()
        path = QPainterPath()
        if self.image_only:
            path.addRect(rect)
        else:
            half = (self.border_width or pixel_size) / 2
            rx = min(NODE_CARD_RADIUS, rect.width() / 2) + half
            ry = min(NODE_CARD_RADIUS, rect.height() / 2) + half
            path.addRoundedRect(rect.adjusted(-half, -half, half, half), rx, ry)
        return path

    def paint(
        self, painter: QPainter, node: Node, *, selected=False, editing=False, controls=True,
        page_controls=True, accent="#3478d5", text_highlight=True, group_title=False
    ):
        """绘制节点正文、内嵌页面图标及可选的外侧折叠标记，不修改文档。

        controls 保留既有绘制接口；交互画布用独立子图元承载按钮，因此与导出、
        拖动影像一样传入 False。直接绘制时默认在右侧显示标记，调用方须将外侧
        标记计入自己的绘图范围。page_controls 独立控制内嵌页面图标，默认
        开启以供静态导出与拖动影像复用；画布通过独立子图元绘制时传入 False。
        text_highlight 控制文本节点内置的选中及编辑描边，默认保留独立绘制接口的
        既有行为。交互画布传入 False，由视图绘制固定屏幕尺寸的间隔外框；该开关
        不影响 editing 对正文的隐藏，也不影响纯图片的选中轮廓及尺寸手柄。
        收藏节点使用由工厂计算的正式填充和描边，图组标题也在自己的卡片范围内绘制。
        正文、标签和备注均显式开启文字抗锯齿，不依赖调用方的视图配置。
        绘制结束、纯图片提前返回或绘制异常时均恢复 painter 的全部绘图状态。
        """
        painter.save()
        try:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
            if self.image_only:
                self.paint_image(painter)
                if selected:
                    painter.setPen(QPen(QColor(accent), 1.5))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(self.image_rect)
                if node.children and controls:
                    self.paint_fold_control(painter, node)
                if page_controls:
                    self.paint_page_control(painter, "attachment")
                return
            # 独立调用仍可请求原有描边；画布把交互高亮交给前景层，正式边框不随
            # 选中或文字焦点变化，避免把根节点同色的加粗描边和新外框重复绘制。
            highlighted = text_highlight and (selected or editing) and not self.favorite
            if not group_title or self.favorite:
                painter.setPen(
                    QPen(QColor(accent) if highlighted else self.border, 2.5 if highlighted else self.border_width)
                )
                painter.setBrush(self.background)
                painter.drawRoundedRect(self.card_rect(), NODE_CARD_RADIUS, NODE_CARD_RADIUS)
            self.paint_image(painter)
            if not editing:
                draw_text(painter, self.text, self.text_position, self.text_color)
            painter.setFont(self.tag_font)
            for value, rect in self.tags:
                painter.setPen(QPen(self.border, 0.8))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, 4, 4)
                painter.setPen(self.text_color)
                painter.drawText(rect.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignVCenter, value)
            if node.note or node.hyperlink:
                painter.setPen(self.text_color)
                painter.drawText(
                    QRectF(12, self.height - self.content_offset - 22,
                           self.width - self.page_controls_width - 30, 18),
                    Qt.AlignmentFlag.AlignLeft,
                    ("≡ " if node.note else "") + ("↗" if node.hyperlink else ""),
                )
            if node.children and controls:
                self.paint_fold_control(painter, node)
            if page_controls:
                for kind in self.page_kinds:
                    self.paint_page_control(painter, kind)
        finally:
            painter.restore()

    def fold_rect(self, direction: str = "right", *, child_count: int = 0) -> QRectF:
        """返回节点局部坐标中的外侧按钮矩形，不扩大正文的测量尺寸。

        direction 使用 right、left 或 down，双向根节点由宿主映射到 right。
        child_count 为收起时的直接子节点数；零表示展开按钮，保留无参数调用。
        基础直径为 20 个逻辑像素，与正文间隔 4 像素；多位计数按真实字体宽度
        扩展为圆角徽标，不截断、不缩写数字，所有尺寸随画布正常缩放。
        """
        width = 20.0
        if child_count:
            width = max(width, QFontMetricsF(self.tag_font).horizontalAdvance(str(child_count)) + 8)
        if direction == "left":
            return QRectF(-width - 4, self.height / 2 - 10, width, 20)
        if direction == "down":
            return QRectF((self.width - width) / 2, self.height + 4, width, 20)
        return QRectF(self.width + 4, self.height / 2 - 10, width, 20)

    def paint_fold_control(
        self, painter: QPainter, node: Node, *, rect: QRectF | None = None, background: QColor | None = None
    ) -> None:
        """按当前节点状态绘制减号或直接子节点数，供独立按钮与静态调用共用。

        rect 必须来自同一视觉的 fold_rect；省略时按右侧计算。background 由画布
        传入当前场景底色，静态调用默认使用节点背景。圆框、符号和数字均采用节点
        边框颜色；计数实时读取 node.children，不依赖可跨节点共享的视觉缓存。
        单独绘制时也开启文字抗锯齿，完成或异常后恢复调用方的绘图状态。
        """
        if rect is None:
            rect = self.fold_rect(child_count=0 if node.expanded else len(node.children))
        painter.save()
        try:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
            painter.setPen(QPen(self.border, 1))
            painter.setBrush(self.background if background is None else background)
            painter.drawRoundedRect(rect, 10, 10)
            if node.expanded:
                painter.drawLine(rect.left() + 5, rect.center().y(), rect.right() - 5, rect.center().y())
            else:
                painter.setFont(self.tag_font)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(len(node.children)))
        finally:
            painter.restore()


class VisualFactory:
    """按内容缓存 QTextDocument 与图片；单节点修改不会重新测量整张导图。"""

    def __init__(self):
        ensure_fonts()
        self.cache: OrderedDict[tuple, NodeVisual] = OrderedDict()
        # 活文档中的每个节点保留一份最近视觉；4096 项内容 LRU 只负责跨节点复用。
        # 大图不能依赖有限 LRU 容纳全部节点，否则顺序测量会反复淘汰尚未访问的项。
        self._node_visuals: dict[str, tuple[tuple, NodeVisual]] = {}
        self._images: OrderedDict[tuple[str, bool], tuple[QPixmap | QSvgRenderer, QSizeF]] = OrderedDict()

    def image_for_asset(self, asset: Asset) -> tuple[QPixmap | QSvgRenderer, QSizeF]:
        """校验资源并缓存绘制对象，SVG 仅缓存解析结果与尺寸。"""
        svg = asset.media_type == SVG_MEDIA_TYPE
        key = (asset.id, svg)
        if key not in self._images:
            if svg:
                self._images[key] = load_svg(asset.data)
            else:
                pixmap, _ = decode_image(asset.data)
                self._images[key] = pixmap, QSizeF(pixmap.size())
            if len(self._images) > 128:
                self._images.popitem(last=False)
        else:
            self._images.move_to_end(key)
        return self._images[key]

    def visual(
        self, node: Node, theme: Theme, document: MindMapDocument, *, is_root=False,
        detail_page_resolver: Callable[[str], tuple[str, str | None]] | None = None,
        cache: bool = True,
    ) -> NodeVisual:
        """取得单个节点的测量与绘制数据，返回值由本工厂缓存并只读共享。

        document 提供图片资源，is_root 决定根节点的默认字号及粗体。正文、图片和
        有效外观未变时复用原视觉；分支样式不影响节点尺寸，不使文字测量缓存失效。
        普通图文内容区上下各保留 8 个逻辑像素，图片、标签和备注按实际内容撑高；
        附页入口需要的最小高度及居中偏移统一由 with_content_size 处理。
        detail_page_resolver 可提供核心共享详情解析接口，返回归属节点及页面 ID；
        仅根节点和子导图入口需要额外解析，普通主题仍取自身字段。省略时沿用
        原有测量行为。解析结果只以是否存在进入缓存，不缓存具体页面或修改节点。
        单页读模型已经投影引用正文；直接绘制完整文档时在这里借用目标根的正文。
        引用按钮只为自身预留几何，图片、标签、样式及其他入口仍取来源节点。
        纯图片节点仅按 image_width 和原始像素比例测量，收藏样式在同一矩形内绘制。
        它不使用普通附件的宽高限制；图片字节无效时抛出 MindMapError。
        宿主先创建 QApplication，并在 GUI 线程使用返回的文字文档和图片对象。
        字体保持节点所选的字族、字号和样式，统一优先抗锯齿并减轻像素网格对齐；
        标签和原地编辑继续借用同一策略。倍率与设备像素密度由实际绘制设备处理，
        不进入视觉缓存键，也不在缩放时重新排版或生成节点位图。
        style.fixed_width 固定内容区宽度，列表和表格的 Qt 排版下限也计入卡片大小。
        cache=False 用于视图单节点试算，借用图片缓存，临时结果只由调用方持有。
        """
        detail_page_id = node.detail_page_id
        if detail_page_resolver is not None and (is_root or node.submap_id):
            detail_page_id = detail_page_resolver(node.id)[1]
        reference = node.page_reference_root_id
        body = document.nodes.get(reference, node) if reference else node
        asset = document.assets[node.image_id] if node.image_id else None
        if cache:
            key = (
                body.text,
                body.text_format,
                # 连接线样式不改变节点文字或图片，切换/预览时复用原有测量缓存。
                repr({key: value for key, value in node.style.items() if key != "branch"}),
                tuple(node.tags),
                tuple(node.icons),
                node.image_id,
                asset is not None and asset.media_type == SVG_MEDIA_TYPE,
                node.image_width,
                node.is_image_node,
                node.favorite,
                bool(node.children),
                bool(node.note),
                bool(node.hyperlink),
                node.attachment is not None,
                bool(detail_page_id),
                bool(node.submap_id),
                bool(reference),
                GRAPH_PAGE_TYPE in node.extension_pages,
                any(key != GRAPH_PAGE_TYPE for key in node.extension_pages),
                is_root,
                repr(theme),
            )
            previous = self._node_visuals.get(node.id)
            if previous is not None and previous[0] == key:
                return previous[1]
            if key in self.cache:
                self.cache.move_to_end(key)
                visual = self.cache[key]
                self._node_visuals[node.id] = (key, visual)
                return visual
        font = QFont(node.style.get("font_family") or QApplication.font().family())
        font.setPointSizeF(float(node.style.get("font_size", 13 if is_root else 11)))
        font.setBold(bool(node.style.get("bold", is_root)))
        font.setItalic(bool(node.style.get("italic", False)))
        font.setUnderline(bool(node.style.get("underline", False)))
        # 默认完整 hinting 会把中英文细笔画过度吸附到像素网格；减少对齐后，
        # 非整数倍率下的曲线和字距过渡更自然，系统字体回退及 ClearType 仍由 Qt 管理。
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        markdown = body.text_format == "markdown"
        background = QColor(node.style.get("background", theme.accent if is_root else theme.node_background))
        root_text = "#18212f" if QColor(theme.accent).lightnessF() > 0.65 else "#ffffff"
        text_color = QColor(node.style.get("text_color", root_text if is_root else theme.text))
        border = QColor(node.style.get("border_color", theme.border))
        border_width = float(node.style.get("border_width", 1))
        if node.favorite:
            background, border, text_color = _favorite_card_colors(theme, text_color)
            border_width = _FAVORITE_BORDER_WIDTH
        # Markdown 正文单独起段，图标前缀不会破坏标题、列表和代码围栏。
        separator = "\n\n" if markdown else " "
        text = (" ".join(node.icons) + separator if node.icons else "") + body.text
        fixed_width = node.style.get("fixed_width")
        max_width = float(node.style.get("width", DEFAULT_TEXT_WIDTH) if fixed_width is None else fixed_width)
        metrics = QFontMetricsF(font)
        if fixed_width is not None:
            text_document = make_text(text, font, max_width, markdown=markdown,
                                      foreground=text_color, background=background)
        elif markdown:
            text_document = make_text(text, font, -1, markdown=True, foreground=text_color, background=background)
            text_document.setTextWidth(max(36, min(max_width, text_document.idealWidth() + 3)))
        else:
            natural = max((metrics.horizontalAdvance(line) for line in text.splitlines()), default=40)
            text_document = make_text(text, font, max(36, min(max_width, natural + 3)))
        text_size = text_document.size()
        if fixed_width is not None:
            max_width = max(max_width, text_size.width())
        image, image_rect = None, QRectF()
        y = _NODE_VERTICAL_PADDING
        padding = NODE_HORIZONTAL_PADDING
        width = text_size.width() + 2 * padding
        if asset is not None:
            image, size = self.image_for_asset(asset)
            iw = node.image_width if node.is_image_node else min(node.image_width, max_width)
            ih = iw * size.height() / size.width()
            if not node.is_image_node and ih > 260:
                iw *= 260 / ih
                ih = 260
            if node.is_image_node:
                image_rect = QRectF(0, 0, iw, ih)
                width, y = iw, ih
            else:
                image_rect = QRectF(padding, y, iw, ih)
                width = max(width, iw + 2 * padding)
                y += ih + 10
        text_position = QPointF(padding, y)
        if not node.is_image_node:
            y += max(metrics.height(), text_size.height())
        tag_font = QFont(font)
        tag_font.setPointSizeF(9)
        tag_metrics = QFontMetricsF(tag_font)
        tags = []
        if node.tags and not node.is_image_node:
            y += 8
            x = padding
            for tag in node.tags:
                tag_width = min(max_width, tag_metrics.horizontalAdvance(tag) + 12)
                if x > padding and x + tag_width > max_width + padding:
                    x, y = padding, y + 26
                tags.append((tag, QRectF(x, y, tag_width, 22)))
                width = max(width, x + tag_width + padding)
                x += tag_width + 6
            y += 22
        if (node.note or node.hyperlink) and not node.is_image_node:
            y += 20
        content_height = y if node.is_image_node else y + _NODE_VERTICAL_PADDING
        visual = NodeVisual(
            width,
            content_height,
            text_document,
            text_position,
            text_color,
            background,
            border,
            border_width,
            image,
            image_rect,
            tags,
            tag_font,
            font,
            bool(node.children),
            page_kinds=tuple(kind for kind, target in (
                ("detail", detail_page_id), ("submap", node.submap_id), ("reference", reference),
                ("graph", GRAPH_PAGE_TYPE in node.extension_pages),
                ("extension", any(key != GRAPH_PAGE_TYPE for key in node.extension_pages)),
                ("attachment", node.attachment),
            ) if target),
            content_height=content_height,
            image_only=node.is_image_node,
            favorite=node.favorite,
        )
        # 普通底色使用强调色；根节点和自定义底色跟随正文色，避免蓝底蓝图标。
        visual.page_icon_color = (
            text_color if node.favorite or is_root or node.style.get("background") else QColor(theme.accent)
        )
        if visual.page_kinds and not visual.image_only:
            visual = visual.with_content_size(width, content_height)
        if cache:
            self.cache[key] = visual
            self._node_visuals[node.id] = (key, visual)
            if len(self.cache) > 4096:
                self.cache.popitem(last=False)
        return visual

    def measure(
        self, document: MindMapDocument, *, changed_node_ids=None, previous_document=None,
        previous_visuals=None, detail_page_resolver: Callable[[str], tuple[str, str | None]] | None = None,
    ) -> tuple[dict, dict]:
        """一次取得节点视觉及完整尺寸，画布与导出共用；未变化节点直接复用排版。

        节点最近视觉按标识保留，容量随活文档节点数增长；删除或替换文档时释放失效
        标识，避免无限保留旧导图。增量调用必须同时给出 previous_document、其对应的
        previous_visuals，以及相对该快照的完整 changed_node_ids；未变化节点直接
        复用传入的正式视觉。不能从最近试算的缓存推断未变化节点，避免被拒绝的候选
        污染正式画面。省略任一条件或主题变化时逐节点检查，兼容可变候选文档。
        detail_page_resolver 同 visual，仅影响页面图标；增量调用时，调用方须把
        共享详情发生变化的可见别名也加入 changed_node_ids，防止复用过期的宽度。
        """
        for key in self._node_visuals.keys() - document.nodes.keys():
            del self._node_visuals[key]
        changed = (
            set(changed_node_ids)
            if changed_node_ids is not None and previous_document is not None
            and previous_visuals is not None and document.root_id == previous_document.root_id
            and document.theme == previous_document.theme
            else None
        )
        roots = () if getattr(document, "kind", None) == "graph" else document.root_ids
        visuals = {
            key: (
                previous_visuals[key]
                if changed is not None and key not in changed and key in previous_visuals
                else self.visual(node, document.theme, document, is_root=key in roots,
                                 detail_page_resolver=detail_page_resolver)
            )
            for key, node in document.nodes.items()
        }
        sizes = {key: (value.width, value.height) for key, value in visuals.items()}
        return visuals, sizes

    def update_measurements(self, document, node_ids, visuals, sizes, *, detail_page_resolver=None) -> None:
        """原地更新正式读模型对应的视觉和尺寸，只处理提交差量。

        调用方必须保证主题和根未变化，且 node_ids 覆盖所有变化实体。试算不能
        使用本接口修改正式字典；主题变化、装载与导出仍调用完整 measure。
        可选 detail_page_resolver 沿用 visual 的共享详情查询契约；node_ids 必须
        包含本次共享详情变化影响的页内别名。仅更新传入字典，不修改文档或历史。
        """
        roots = () if getattr(document, "kind", None) == "graph" else document.root_ids
        for key in node_ids:
            node = document.nodes.get(key)
            if node is None:
                visuals.pop(key, None)
                sizes.pop(key, None)
                self._node_visuals.pop(key, None)
            else:
                visual = self.visual(node, document.theme, document, is_root=key in roots,
                                     detail_page_resolver=detail_page_resolver)
                visuals[key] = visual
                sizes[key] = visual.width, visual.height


def curve_path(points) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    path.cubicTo(QPointF(*points[1]), QPointF(*points[2]), QPointF(*points[3]))
    return path


def branch_path(branch: Curve | BranchPath) -> QPainterPath:
    """把无界面几何转换成 Qt 路径，画布、图标和导出共用此入口。"""
    if isinstance(branch, Curve):
        return curve_path(branch.points)
    path = QPainterPath()
    for command in branch.commands:
        points = [QPointF(*point) for point in command.points]
        if command.op == "M":
            path.moveTo(points[0])
        elif command.op == "L":
            path.lineTo(points[0])
        elif command.op == "C":
            path.cubicTo(*points)
        else:
            path.closeSubpath()
    return path


def branch_arrow_path(branch: Curve | BranchPath) -> QPainterPath:
    """箭头独立填充，切换到无箭头的路径时返回空轮廓，避免旧装饰残留。"""
    path = QPainterPath()
    if isinstance(branch, BranchPath) and branch.arrow is not None:
        path.moveTo(QPointF(*branch.arrow[0]))
        for point in branch.arrow[1:]:
            path.lineTo(QPointF(*point))
        path.closeSubpath()
    return path


def branch_pen(branch: Curve | BranchPath) -> QPen:
    """保留旧 Curve 的默认端帽；新路径使用圆端帽和明确的折线连接。"""
    if isinstance(branch, Curve):
        return QPen(QColor(branch.color), 1.8)
    pen = QPen(
        QColor(branch.color),
        branch.width,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.MiterJoin,
    )
    # 与核心 BranchPath.bounds 的保守留边约定一致，不依赖平台默认斜接上限。
    pen.setMiterLimit(2.0)
    return pen


def paint_branch(painter: QPainter, branch: Curve | BranchPath) -> None:
    """直接绘制一条分支及其箭头，供预览图标与实验脚本复用。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(branch_pen(branch))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(branch_path(branch))
    arrow = branch_arrow_path(branch)
    if not arrow.isEmpty():
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(branch.color))
        painter.drawPath(arrow)
    painter.restore()


class BranchItem(QGraphicsPathItem):
    """画布和导出的统一分支图元，边界及命中形状同时覆盖描边和填充箭头。"""

    def __init__(self, branch: Curve | BranchPath):
        super().__init__()
        self._arrow = QPainterPath()
        self._branch = None
        self._hit_shape: QPainterPath | None = None
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(-1)
        self.set_branch(branch)

    def set_branch(self, branch: Curve | BranchPath) -> None:
        """先通知几何变化，再替换路径与箭头；已有图元可跨样式安全复用。"""
        if branch == self._branch:
            return
        self.prepareGeometryChange()
        self._branch = branch
        self._arrow = branch_arrow_path(branch)
        self.setPath(branch_path(branch))
        self.setPen(branch_pen(branch))
        self._hit_shape = None
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.update()

    def boundingRect(self) -> QRectF:
        """返回覆盖线身和箭头的外接矩形，供 Qt 索引和重绘，精确命中由 shape 负责。"""
        if self._arrow.isEmpty():
            return super().boundingRect()
        # Qt 的默认边界会调用 shape。这里用路径的外接矩形留足斜接余量，避免只为
        # 索引、绘制或导出就计算描边与箭头的并集。
        pen = self.pen()
        margin = pen.widthF() * pen.miterLimit()
        bounds = self.path().boundingRect().adjusted(-margin, -margin, margin, margin)
        return bounds.united(self._arrow.boundingRect())

    def shape(self) -> QPainterPath:
        """有箭头时返回实际描边和箭头填充的并集，供点选及框选命中。

        Qt 默认形状还包含原始路径，会将开放的曲线按首尾闭合来判断内部。这里单独
        取线身的描边，再与箭头求并集，既排除弯线间的空白，也保留箭头与线身的交叠。
        首次命中时计算并缓存，set_branch 更新图元时失效。返回独立路径供调用方使用。
        """
        if self._arrow.isEmpty():
            return super().shape()
        if self._hit_shape is None:
            stroke = QPainterPathStroker(self.pen()).createStroke(self.path())
            self._hit_shape = stroke.united(self._arrow)
        return QPainterPath(self._hit_shape)

    def paint(self, painter, option, widget=None) -> None:
        super().paint(painter, option, widget)
        if not self._arrow.isEmpty():
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.pen().color())
            painter.drawPath(self._arrow)
            painter.restore()
