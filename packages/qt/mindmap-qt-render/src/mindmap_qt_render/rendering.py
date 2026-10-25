"""文字与图片测量、绘制缓存及图形导出共用的视觉数据。"""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from mindmap_core import BranchPath, Curve, MindMapDocument, MindMapError, Node, Theme
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImageReader,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import QApplication, QGraphicsPathItem

from .page_icons import paint_page_icon

# 节点内容区上下各保留 8 个逻辑像素；正式测量、编辑草稿与导出共用此留白。
_NODE_VERTICAL_PADDING = 8.0

PAGE_CONTROL_SIZE = 32.0
PAGE_CONTROL_GAP = 2.0
PAGE_CONTENT_GAP = 8.0
PAGE_RIGHT_PADDING = 14.0
# 保留 32×32 的入口命中区域，上下各留 2 个逻辑像素，适配更紧凑的节点。
PAGE_MIN_HEIGHT = 36.0


def ensure_fonts() -> None:
    """补齐 Windows 离屏插件的空字体库，不改变正常桌面宿主的字体设置。

    Qt 的 Windows offscreen 插件可能返回零种字体。仅在此情况下，从当前系统
    读取微软雅黑并注册到本进程；字体不被复制进包或写入系统目录。
    """
    if QApplication.instance() is None:
        raise MindMapError("创建图形组件或导出前必须先创建 QApplication")
    if QFontDatabase.families():
        return
    if sys.platform == "win32":
        directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        family = None
        for filename in ("msyh.ttc", "msyhbd.ttc", "simsun.ttc"):
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


def make_text(text: str, font: QFont, width: float, *, markdown=False) -> QTextDocument:
    """创建按逻辑像素排版的独立文本，供节点缓存、拖动预览及导出共享。

    text 为正文或 Markdown 源码，font 为工厂已配置抗锯齿和像素对齐策略的
    字体，width 为不含节点留白的最大正文宽度。markdown 为真时保留现有的
    GitHub Markdown 解析规则并禁止 HTML；本函数不修改输入字体或读取外部资源。
    使用字体设计度量计算换行，避免绘制倍率和设备像素取整影响缓存布局。
    返回的 QTextDocument 由调用方在 Qt GUI 线程持有，发布进视觉缓存后只读使用。
    """
    document = _TextDocument()
    document.setDocumentMargin(0)
    document.setDefaultFont(font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    option.setUseDesignMetrics(True)
    document.setDefaultTextOption(option)
    if markdown:
        document.setMarkdown(
            text,
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML,
        )
    else:
        document.setPlainText(text)
    document.setTextWidth(width)
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
    image: QPixmap | None
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
    # 纯图片只绘制原始矩形和临时选中轮廓；默认值兼容旧视觉构造接口。
    image_only: bool = False

    @property
    def page_controls_width(self) -> float:
        """返回入口增加的完整宽度；无入口时为零，原正文宽度不受影响。"""
        count = len(self.page_kinds)
        if not count:
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

        页面顺序由 page_kinds 固定为详情、子导图；按钮为 32×32，间隔为 2，
        最右侧保留 14 像素，与节点朝向无关。测量、绘制与命中共用此坐标。
        """
        if kind not in self.page_kinds:
            return QRectF()
        count = len(self.page_kinds)
        strip = count * PAGE_CONTROL_SIZE + (count - 1) * PAGE_CONTROL_GAP
        x = self.width - PAGE_RIGHT_PADDING - strip
        x += self.page_kinds.index(kind) * (PAGE_CONTROL_SIZE + PAGE_CONTROL_GAP)
        return QRectF(x, (self.height - PAGE_CONTROL_SIZE) / 2, PAGE_CONTROL_SIZE, PAGE_CONTROL_SIZE)

    def paint_page_control(self, painter: QPainter, kind: str) -> None:
        """在入口中心绘制 16×16 图形；不绘制悬停底色，也不注册导航行为。"""
        rect = self.page_control_rect(kind)
        if not rect.isEmpty():
            paint_page_icon(painter, kind, rect.adjusted(8, 8, -8, -8),
                            self.page_icon_color if self.page_icon_color is not None else self.text_color)

    def paint(
        self, painter: QPainter, node: Node, *, selected=False, editing=False, controls=True,
        page_controls=True, accent="#3478d5"
    ):
        """绘制节点正文、内嵌页面图标及可选的外侧折叠标记，不修改文档。

        controls 保留既有绘制接口；交互画布用独立子图元承载按钮，因此与导出、
        拖动影像一样传入 False。直接绘制时默认在右侧显示标记，调用方须将外侧
        标记计入自己的绘图范围。page_controls 独立控制内嵌页面图标，默认
        开启以供静态导出与拖动影像复用；画布通过独立子图元绘制时传入 False。
        正文、标签和备注均显式开启文字抗锯齿，不依赖调用方的视图配置。
        绘制结束、纯图片提前返回或绘制异常时均恢复 painter 的全部绘图状态。
        """
        painter.save()
        try:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
            if self.image_only:
                if self.image is not None:
                    painter.drawPixmap(self.image_rect, self.image, QRectF(self.image.rect()))
                if selected:
                    painter.setPen(QPen(QColor(accent), 1.5))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(self.image_rect)
                if node.children and controls:
                    self.paint_fold_control(painter, node)
                return
            # 文本控件借用焦点或鼠标选区时，节点选择标记可能暂时变化；整个编辑
            # 会话仍保留同一条圆角高亮边框，不再依赖额外的文本焦点虚线表达状态。
            highlighted = selected or editing
            painter.setPen(
                QPen(QColor(accent) if highlighted else self.border, 2.5 if highlighted else self.border_width)
            )
            painter.setBrush(self.background)
            painter.drawRoundedRect(QRectF(1, 1, self.width - 2, self.height - 2), 7, 7)
            if self.image is not None:
                painter.drawPixmap(self.image_rect, self.image, QRectF(self.image.rect()))
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
        self.pixmaps: OrderedDict[str, QPixmap] = OrderedDict()

    def visual(self, node: Node, theme: Theme, document: MindMapDocument, *, is_root=False) -> NodeVisual:
        """取得单个节点的测量与绘制数据，返回值由本工厂缓存并只读共享。

        document 提供图片资源，is_root 决定根节点的默认字号及粗体。正文、图片和
        有效外观未变时复用原视觉；分支样式不影响节点尺寸，不使文字测量缓存失效。
        普通图文内容区上下各保留 8 个逻辑像素，图片、标签和备注按实际内容撑高；
        附页入口需要的最小高度及居中偏移统一由 with_content_size 处理。
        纯图片节点仅按 image_width 和原始像素比例测量，无正文、留白、填充及常驻
        边框，也不使用普通附件的宽高限制；图片字节无效时抛出 MindMapError。
        宿主先创建 QApplication，并在 GUI 线程使用返回的文字文档和图片对象。
        字体保持节点所选的字族、字号和样式，统一优先抗锯齿并减轻像素网格对齐；
        标签和原地编辑继续借用同一策略。倍率与设备像素密度由实际绘制设备处理，
        不进入视觉缓存键，也不在缩放时重新排版或生成节点位图。
        """
        key = (
            node.text,
            node.text_format,
            # 连接线样式不改变节点文字或图片，切换/预览时复用原有测量缓存。
            repr({key: value for key, value in node.style.items() if key != "branch"}),
            tuple(node.tags),
            tuple(node.icons),
            node.image_id,
            node.image_width,
            node.is_image_node,
            bool(node.children),
            bool(node.note),
            bool(node.hyperlink),
            bool(node.detail_page_id),
            bool(node.submap_id),
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
        text = (" ".join(node.icons) + " " if node.icons else "") + node.text
        max_width = float(node.style.get("width", 280))
        metrics = QFontMetricsF(font)
        natural = max((metrics.horizontalAdvance(line) for line in text.splitlines()), default=40)
        text_width = max(36, min(max_width, natural + 3))
        if node.text_format == "markdown":
            text_width = max(120, min(max_width, natural + 3))
        text_document = make_text(text, font, text_width, markdown=node.text_format == "markdown")
        image, image_rect = None, QRectF()
        y = _NODE_VERTICAL_PADDING
        width = text_width + 28
        if node.image_id:
            if node.image_id not in self.pixmaps:
                image, _ = decode_image(document.assets[node.image_id].data)
                self.pixmaps[node.image_id] = image
                if len(self.pixmaps) > 128:
                    self.pixmaps.popitem(last=False)
            image = self.pixmaps[node.image_id]
            iw = node.image_width if node.is_image_node else min(node.image_width, max_width)
            ih = iw * image.height() / image.width()
            if not node.is_image_node and ih > 260:
                iw *= 260 / ih
                ih = 260
            if node.is_image_node:
                image_rect = QRectF(0, 0, iw, ih)
                width, y = iw, ih
            else:
                image_rect = QRectF(14, y, iw, ih)
                width = max(width, iw + 28)
                y += ih + 10
        text_position = QPointF(14, y)
        if not node.is_image_node:
            y += max(metrics.height(), text_document.size().height())
        tag_font = QFont(font)
        tag_font.setPointSizeF(9)
        tag_metrics = QFontMetricsF(tag_font)
        tags = []
        if node.tags and not node.is_image_node:
            y += 8
            x = 14.0
            for tag in node.tags:
                tag_width = min(max_width, tag_metrics.horizontalAdvance(tag) + 12)
                if x > 14 and x + tag_width > max_width + 14:
                    x, y = 14, y + 26
                tags.append((tag, QRectF(x, y, tag_width, 22)))
                x += tag_width + 6
                width = max(width, x + 8)
            y += 22
        if (node.note or node.hyperlink) and not node.is_image_node:
            y += 20
        content_height = y if node.is_image_node else y + _NODE_VERTICAL_PADDING
        visual = NodeVisual(
            width,
            content_height,
            text_document,
            text_position,
            QColor(
                node.style.get(
                    "text_color",
                    ("#18212f" if QColor(theme.accent).lightnessF() > 0.65 else "#ffffff")
                    if is_root
                    else theme.text,
                )
            ),
            QColor(node.style.get("background", theme.accent if is_root else theme.node_background)),
            QColor(node.style.get("border_color", theme.border)),
            float(node.style.get("border_width", 1)),
            image,
            image_rect,
            tags,
            tag_font,
            font,
            bool(node.children),
            page_kinds=tuple(kind for kind, page in (("detail", node.detail_page_id), ("submap", node.submap_id))
                             if page),
            content_height=content_height,
            image_only=node.is_image_node,
        )
        # 普通底色使用强调色；根节点和自定义底色跟随正文色，避免蓝底蓝图标。
        visual.page_icon_color = visual.text_color if is_root or node.style.get("background") else QColor(theme.accent)
        if visual.page_kinds:
            visual = visual.with_content_size(width, content_height)
        self.cache[key] = visual
        self._node_visuals[node.id] = (key, visual)
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return visual

    def measure(
        self, document: MindMapDocument, *, changed_node_ids=None, previous_document=None, previous_visuals=None
    ) -> tuple[dict, dict]:
        """一次取得节点视觉及完整尺寸，画布与导出共用；未变化节点直接复用排版。

        节点最近视觉按标识保留，容量随活文档节点数增长；删除或替换文档时释放失效
        标识，避免无限保留旧导图。增量调用必须同时给出 previous_document、其对应的
        previous_visuals，以及相对该快照的完整 changed_node_ids；未变化节点直接
        复用传入的正式视觉。不能从最近试算的缓存推断未变化节点，避免被拒绝的候选
        污染正式画面。省略任一条件或主题变化时逐节点检查，兼容可变候选文档。
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
        visuals = {
            key: (
                previous_visuals[key]
                if changed is not None and key not in changed and key in previous_visuals
                else self.visual(node, document.theme, document, is_root=key in document.root_ids)
            )
            for key, node in document.nodes.items()
        }
        sizes = {key: (value.width, value.height) for key, value in visuals.items()}
        return visuals, sizes

    def update_measurements(self, document, node_ids, visuals, sizes) -> None:
        """原地更新正式读模型对应的视觉和尺寸，只处理提交差量。

        调用方必须保证主题和根未变化，且 node_ids 覆盖所有变化实体。试算不能
        使用本接口修改正式字典；主题变化、装载与导出仍调用完整 measure。
        """
        for key in node_ids:
            node = document.nodes.get(key)
            if node is None:
                visuals.pop(key, None)
                sizes.pop(key, None)
                self._node_visuals.pop(key, None)
            else:
                visual = self.visual(node, document.theme, document, is_root=key in document.root_ids)
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
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(-1)
        self.set_branch(branch)

    def set_branch(self, branch: Curve | BranchPath) -> None:
        """先通知几何变化，再替换路径与箭头；已有图元可跨样式安全复用。"""
        if branch == self._branch:
            return
        self._branch = branch
        self.prepareGeometryChange()
        self._arrow = branch_arrow_path(branch)
        self.setPath(branch_path(branch))
        self.setPen(branch_pen(branch))
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.update()

    def boundingRect(self) -> QRectF:
        bounds = super().boundingRect()
        return bounds.united(self._arrow.boundingRect()) if not self._arrow.isEmpty() else bounds

    def shape(self) -> QPainterPath:
        path = super().shape()
        path.addPath(self._arrow)
        return path

    def paint(self, painter, option, widget=None) -> None:
        super().paint(painter, option, widget)
        if not self._arrow.isEmpty():
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.pen().color())
            painter.drawPath(self._arrow)
            painter.restore()
