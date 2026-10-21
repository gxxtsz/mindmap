"""用正式布局、分支路由和 Qt 绘制入口验收 11 种连接线，不再维护实验公式。

运行示例：
    uv run --locked --all-packages python scripts/branch_style_lab.py
    uv run --locked --all-packages python scripts/branch_style_lab.py --direction both
    uv run --locked --all-packages python scripts/branch_style_lab.py --style 10_arrow2 --show

普通分支公式的 REA 来源为 calcLinePath（0x180218b50），证据标识：
ev_154960d96c32553331bfb19d6c801745e2fb011bb1b9d9db4e1bc138d16fb966。
恢复公式、主干位置等独立参数的说明见 mindmap_layouts.connectors 和中文逆向报告。
本工具只绘制本项目节点，不包含原软件图片、特殊节点补偿、加厚或手绘效果。
"""

from __future__ import annotations

import argparse
import os
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from mindmap_core import BranchStyle, LayoutConfig, MindMapDocument, MindMapEngine, Rect, apply_branch_style
from mindmap_layouts import TreeLayout, route_branches
from mindmap_qt_render.rendering import ensure_fonts, paint_branch
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPaintEvent, QPen
from PySide6.QtSvg import QSvgGenerator, QSvgRenderer
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget

DIRECTION_LABELS = {"right": "向右", "left": "向左", "both": "双向", "down": "向下"}
# 编号仅用于对应原资源名称和兼容旧命令行；正式文档始终保存语义标识。
RESOURCE_IDS = {kind: f"{index}_{kind.value}" for index, kind in enumerate(BranchStyle, 1)}
STYLE_DESCRIPTIONS = {
    BranchStyle.LINE: "父子锚点之间的直线",
    BranchStyle.ARC: "恢复控制比例：0.4 / 0.6",
    BranchStyle.CURVE: "恢复随坡度变化的控制系数",
    BranchStyle.ELBOW: "共享主干去重后统一描画",
    BranchStyle.ROUND: "恢复半径上限 6 与控制比例 0.448",
    BranchStyle.ARC2: "恢复控制比例：0.2 / 0.25",
    BranchStyle.ROUND2: "纯折线，普通尾段长度 11",
    BranchStyle.CENTRE: "独立短主干 + 恢复曲线段",
    BranchStyle.ARROW: "正交路径，终点切向决定箭头",
    BranchStyle.ARROW2: "与凹弧同中心线；箭头独立构造",
    BranchStyle.CURVE_CONVEX: "独立重建，尚无完整恢复公式",
}


@dataclass(frozen=True)
class Sample:
    """保留真实文档与布局矩形，卡片样式仅应用于该文档的候选副本。"""

    document: MindMapDocument
    nodes: dict[str, Rect]
    bounds: Rect
    direction: str


def create_sample(direction: str) -> Sample:
    """通过公开引擎和 TreeLayout 创建样例，方向及锚点规则与正式画布一致。"""
    engine = MindMapEngine()
    engine.set_layout(LayoutConfig(direction=direction))
    engine.update_node(engine.root_id, text="中心")
    children = tuple(engine.add_child(engine.root_id, label) for label in ("甲", "乙", "丙"))
    measurements = {engine.root_id: (64, 32), **dict.fromkeys(children, (48, 26))}
    document = engine.document
    document.theme.palette = ["#43546a"]
    layout = TreeLayout().layout(document, measurements)
    bounds = layout.nodes[document.root_id]
    for rect in layout.nodes.values():
        bounds = bounds.union(rect)
    return Sample(document, layout.nodes, bounds.padded(9), direction)


def set_font(painter: QPainter, size: int, *, bold: bool = False) -> None:
    """以像素设置中文字体，PNG、SVG 和窗口共用逻辑尺寸。"""
    font = QFont(QApplication.font().family())
    font.setPixelSize(size)
    font.setBold(bold)
    painter.setFont(font)


def paint_sample(painter: QPainter, viewport: QRectF, sample: Sample, style: BranchStyle) -> None:
    """直接调用正式路由和描画，包括主干合并与箭头；此处不含路径计算公式。"""
    document = deepcopy(sample.document)
    apply_branch_style(document, [document.root_id], style)
    branches = route_branches(document, sample.nodes)
    bounds = sample.bounds
    scale = min(viewport.width() / bounds.width, viewport.height() / bounds.height)
    painter.save()
    painter.translate(viewport.center())
    painter.scale(scale, scale)
    painter.translate(-bounds.center[0], -bounds.center[1])
    for branch in branches.values():
        paint_branch(painter, branch)
    set_font(painter, 12)
    for node_id, rect in sample.nodes.items():
        box = QRectF(rect.x, rect.y, rect.width, rect.height)
        painter.setPen(QPen(QColor("#bbc7d5"), 0.8))
        painter.setBrush(QColor("#dde6f1" if node_id == document.root_id else "#edf1f6"))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor("#33455d"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, document.nodes[node_id].text)
    painter.restore()


class LabDrawing:
    """同一份绘图入口供光栅、矢量和可选窗口调用，避免三套路径实现不一致。"""

    def __init__(self, sample: Sample, selected: BranchStyle | None):
        self.sample = sample
        self.styles = [selected] if selected else list(BranchStyle)
        self.single = selected is not None
        self.size = QSize(900, 570) if self.single else QSize(1440, 1020)

    def paint(self, painter: QPainter) -> None:
        """绘制实验说明与样式卡片；原软件图片不会进入此绘图流程。"""
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(QRectF(0, 0, self.size.width(), self.size.height()), QColor("#f3f6fa"))
        painter.setPen(QColor("#24364d"))
        set_font(painter, 28, bold=True)
        painter.drawText(QRectF(36, 23, 1340, 43), "分支连接线 · 正式路由验收")
        set_font(painter, 15)
        painter.setPen(QColor("#52647a"))
        description = (
            f"{DIRECTION_LABELS[self.sample.direction]}布局 · 画布与导出共用路由 · 锚点来自 TreeLayout"
        )
        painter.drawText(QRectF(36, 70, 1340, 28), description)
        for index, style in enumerate(self.styles):
            column, row = index % 4, index // 4
            card = (
                QRectF(36, 117, 828, 372)
                if self.single
                else QRectF(36 + column * 350, 117 + row * 276, 318, 250)
            )
            painter.setPen(QPen(QColor("#dce3ed"), 1))
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(card, 12, 12)
            label, explanation = style.label, STYLE_DESCRIPTIONS[style]
            painter.setPen(QColor("#253b57"))
            set_font(painter, 19 if self.single else 17, bold=True)
            title_box = card.adjusted(18, 13, -15, -card.height() + 44)
            title = f"{RESOURCE_IDS[style]}  {label}"
            if painter.fontMetrics().horizontalAdvance(title) > title_box.width():
                set_font(painter, 14, bold=True)
            painter.drawText(title_box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
            if self.single:
                diagram = card.adjusted(110, 61, -110, -57)
            else:
                diagram = card.adjusted(31, 54, -31, -45)
            paint_sample(painter, diagram, self.sample, style)
            painter.setPen(QColor("#65768a"))
            set_font(painter, 14 if self.single else 12)
            painter.drawText(card.adjusted(18, card.height() - 35, -12, -9), explanation)
        painter.setPen(QColor("#697a8f"))
        set_font(painter, 13)
        footers = (
            "普通分支采用已恢复公式；主干比例、短边保护与箭头尺寸为本项目独立参数。",
            "11_curveconvex 为独立重建；不包含原生特殊节点补偿、加厚轮廓与手绘效果。",
        )
        for index, footer in enumerate(footers):
            painter.drawText(
                QRectF(36, self.size.height() - 65 + index * 24, self.size.width() - 72, 23), footer
            )


def export_drawing(drawing: LabDrawing, output: Path, stem: str) -> tuple[Path, Path]:
    """导出同源 PNG 与 SVG，并立即检查 Qt 是否能解码结果；默认不创建窗口。"""
    output.mkdir(parents=True, exist_ok=True)
    png_path, svg_path = output / f"{stem}.png", output / f"{stem}.svg"
    image = QImage(drawing.size, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        drawing.paint(painter)
    finally:
        painter.end()
    if not image.save(str(png_path)):
        raise RuntimeError(f"PNG 导出失败：{png_path}")
    generator = QSvgGenerator()
    generator.setFileName(str(svg_path))
    generator.setSize(drawing.size)
    generator.setViewBox(QRectF(0, 0, drawing.size.width(), drawing.size.height()))
    generator.setTitle("分支连接线正式路由验收")
    generator.setDescription("节点矩形取自本项目 TreeLayout；普通分支采用恢复公式，11 与箭头尺寸独立重建。")
    painter = QPainter(generator)
    try:
        drawing.paint(painter)
    finally:
        painter.end()
    if QImage(str(png_path)).isNull() or not QSvgRenderer(str(svg_path)).isValid():
        raise RuntimeError("生成的 PNG 或 SVG 无法重新读取")
    return png_path, svg_path


class LabWidget(QWidget):
    """仅在传入 --show 时建立的预览窗口；窗口仍调用相同 QPainter 入口。"""

    def __init__(self, drawing: LabDrawing):
        super().__init__()
        self.drawing = drawing
        self.setFixedSize(drawing.size)

    def paintEvent(self, event: QPaintEvent) -> None:
        """窗口重绘只读取实验图，不读取或修改用户正在编辑的思维导图。"""
        painter = QPainter(self)
        try:
            self.drawing.paint(painter)
        finally:
            painter.end()


def main() -> int:
    """兼容原资源编号及正式语义名称，生成文件写入指定目录，默认不显示窗口。"""
    parser = argparse.ArgumentParser(description="使用 TreeLayout 节点矩形运行 11 种分支正式路由验收")
    parser.add_argument("--output", type=Path, default=Path(".artifacts/branch-style-lab"), help="输出目录")
    parser.add_argument("--style", choices=[*BranchStyle, *RESOURCE_IDS.values()], help="仅导出某一种样式")
    parser.add_argument("--direction", choices=list(DIRECTION_LABELS), default="right", help="布局方向")
    parser.add_argument("--show", action="store_true", help="导出后打开本实验图的独立预览窗口")
    arguments = parser.parse_args()
    if not arguments.show:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication([sys.argv[0]])
    ensure_fonts()
    app.setFont(QFont("Microsoft YaHei"))
    aliases = {value: kind for kind, value in RESOURCE_IDS.items()}
    selected = BranchStyle(aliases.get(arguments.style, arguments.style)) if arguments.style else None
    drawing = LabDrawing(create_sample(arguments.direction), selected)
    stem = f"{RESOURCE_IDS[selected] if selected else 'branch-styles'}-{arguments.direction}"
    paths = export_drawing(drawing, arguments.output.resolve(), stem)
    for path in paths:
        print(path)
    if arguments.show:
        preview = QScrollArea()
        preview.setWindowTitle("分支连接线正式路由验收")
        preview.setWidget(LabWidget(drawing))
        preview.resize(min(drawing.size.width() + 26, 1460), min(drawing.size.height() + 26, 960))
        preview.show()
        return app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
