"""文字与图片测量、绘制缓存及图形导出共用的视觉数据。"""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from mindmap_engine import BranchPath, Curve, MindMapDocument, MindMapError, Node, Theme
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
        """绘制节点内容；导出时关闭折叠按钮等控件，同时保留内容的测量尺寸。"""
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
        if self.has_children and controls:
            rect = self.fold_rect()
            painter.setPen(QPen(self.border, 1))
            painter.setBrush(self.background)
            painter.drawEllipse(rect)
            painter.setPen(self.text_color)
            painter.drawLine(rect.left() + 3, rect.center().y(), rect.right() - 3, rect.center().y())
            if not node.expanded:
                painter.drawLine(rect.center().x(), rect.top() + 3, rect.center().x(), rect.bottom() - 3)

    def fold_rect(self) -> QRectF:
        return QRectF(self.width - 20, self.height / 2 - 7, 14, 14)


class VisualFactory:
    """按内容缓存 QTextDocument 与图片；单节点修改不会重新测量整张导图。"""

    def __init__(self):
        ensure_fonts()
        self.cache: OrderedDict[tuple, NodeVisual] = OrderedDict()
        self.pixmaps: OrderedDict[str, QPixmap] = OrderedDict()

    def visual(self, node: Node, theme: Theme, document: MindMapDocument, *, is_root=False) -> NodeVisual:
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
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
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
        width = text_width + 28 + (20 if node.children else 0)
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
            width = max(width, iw + 28 + (20 if node.children else 0))
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
                width = max(width, x + 8 + (20 if node.children else 0))
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
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return visual

    def measure(self, document: MindMapDocument) -> tuple[dict, dict]:
        """一次获取节点视觉与布局尺寸；图片、节点标签和内边距均计入尺寸，供画布与导出共用。"""
        visuals = {
            key: self.visual(node, document.theme, document, is_root=key == document.root_id)
            for key, node in document.nodes.items()
        }
        sizes = {key: (value.width, value.height) for key, value in visuals.items()}
        return visuals, sizes


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
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(-1)
        self.set_branch(branch)

    def set_branch(self, branch: Curve | BranchPath) -> None:
        """先通知几何变化，再替换路径与箭头；已有图元可跨样式安全复用。"""
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
