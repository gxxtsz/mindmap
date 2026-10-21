"""文字与图片测量、绘制缓存及图形导出共用的视觉数据。"""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from dataclasses import dataclass
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
    document = _TextDocument()
    document.setDocumentMargin(0)
    document.setDefaultFont(font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
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
    """使用显式文本调色板绘制，避免深色导出依赖当前系统主题。"""
    painter.save()
    painter.translate(position)
    context = QAbstractTextDocumentLayout.PaintContext()
    context.palette.setColor(QPalette.ColorRole.Text, color)
    context.palette.setColor(QPalette.ColorRole.WindowText, color)
    document.documentLayout().draw(painter, context)
    painter.restore()


def decode_image(data: bytes) -> tuple[QPixmap, str]:
    """验证图片并返回可绘制资源及 MIME；限制解码像素数以避免误选超大图片。"""
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > 40_000_000:
        raise MindMapError("图片无效或像素数超过四千万")
    image = reader.read()
    if image.isNull():
        raise MindMapError(f"图片无法解码：{reader.errorString()}")
    kind = bytes(reader.format()).decode("ascii", "replace")
    return QPixmap.fromImage(image), "image/" + ("jpeg" if kind in ("jpg", "jpeg") else kind)


@dataclass
class NodeVisual:
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

    def paint(
        self, painter: QPainter, node: Node, *, selected=False, editing=False, controls=True, accent="#3478d5"
    ):
        """绘制节点正文及可选的外侧折叠标记，不改变节点或文档。

        controls 保留既有绘制接口；交互画布用独立子图元承载按钮，因此与导出、
        拖动影像一样传入 False。直接绘制时默认在右侧显示标记，调用方须将外侧
        标记计入自己的绘图范围；正文测量尺寸不再包含旧按钮的额外留白。
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(QColor(accent) if selected else self.border, 2.5 if selected else self.border_width)
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
                QRectF(12, self.height - 22, self.width - 30, 18),
                Qt.AlignmentFlag.AlignLeft,
                ("≡ " if node.note else "") + ("↗" if node.hyperlink else ""),
            )
        if node.children and controls:
            self.paint_fold_control(painter, node)

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
        """
        if rect is None:
            rect = self.fold_rect(child_count=0 if node.expanded else len(node.children))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.border, 1))
        painter.setBrush(self.background if background is None else background)
        painter.drawRoundedRect(rect, 10, 10)
        if node.expanded:
            painter.drawLine(rect.left() + 5, rect.center().y(), rect.right() - 5, rect.center().y())
        else:
            painter.setFont(self.tag_font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(len(node.children)))
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
        宿主先创建 QApplication，并在 GUI 线程使用返回的文字文档和图片对象。
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
            bool(node.children),
            bool(node.note),
            bool(node.hyperlink),
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
        text = (" ".join(node.icons) + " " if node.icons else "") + node.text
        max_width = float(node.style.get("width", 280))
        metrics = QFontMetricsF(font)
        natural = max((metrics.horizontalAdvance(line) for line in text.splitlines()), default=40)
        text_width = max(36, min(max_width, natural + 3))
        if node.text_format == "markdown":
            text_width = max(120, min(max_width, natural + 3))
        text_document = make_text(text, font, text_width, markdown=node.text_format == "markdown")
        image, image_rect = None, QRectF()
        y = 12.0
        width = text_width + 28
        if node.image_id:
            if node.image_id not in self.pixmaps:
                image, _ = decode_image(document.assets[node.image_id].data)
                self.pixmaps[node.image_id] = image
                if len(self.pixmaps) > 128:
                    self.pixmaps.popitem(last=False)
            image = self.pixmaps[node.image_id]
            iw = min(node.image_width, max_width)
            ih = iw * image.height() / image.width()
            if ih > 260:
                iw *= 260 / ih
                ih = 260
            image_rect = QRectF(14, y, iw, ih)
            width = max(width, iw + 28)
            y += ih + 10
        text_position = QPointF(14, y)
        y += max(metrics.height(), text_document.size().height())
        tag_font = QFont(font)
        tag_font.setPointSizeF(9)
        tag_metrics = QFontMetricsF(tag_font)
        tags = []
        if node.tags:
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
        if node.note or node.hyperlink:
            y += 20
        visual = NodeVisual(
            width,
            y + 12,
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
        )
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
                else self.visual(node, document.theme, document, is_root=key == document.root_id)
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
                visual = self.visual(node, document.theme, document, is_root=key == document.root_id)
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
