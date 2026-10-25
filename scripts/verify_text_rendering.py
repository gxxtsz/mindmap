"""验证中英文的原生像素绘制，输出多倍率对照图、导出样例及中文验收记录。

在仓库根目录执行：
uv run --locked --all-packages python scripts/verify_text_rendering.py --platform windows --baseline-ref HEAD
--baseline-ref 可选，指定本地 Git 版本中的旧渲染模块作为对照；省略时仅输出当前版本。
验收使用生成的独立文档、隐藏画布和 QImage，不读取用户导图或本机偏好。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from math import ceil
from pathlib import Path
from types import ModuleType

from mindmap_core import MindMapDocument, MindMapEngine, Node, Theme
from mindmap_layouts import create_default_layout_registry
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_render import export_document, rendering
from PySide6.QtCore import QPointF, Qt, qVersion
from PySide6.QtGui import QColor, QFont, QFontInfo, QImage, QInputMethodEvent, QPainter
from PySide6.QtWidgets import QApplication

ZOOMS = (0.1, 0.5, 0.75, 1, 1.15, 1.25, 1.5, 2, 4)
PIXEL_RATIOS = (1, 1.25, 1.5, 2)


def load_baseline(revision: str) -> ModuleType:
    """读取指定本地 Git 版本的渲染模块，作为独立命名空间中的旧版本参照。

    revision 必须是当前仓库可解析的版本；只执行该版本已有的 Python 模块定义，
    不检出、不覆盖工作区文件，也不替换生产模块或全局字体。读取错误直接终止验收。
    """
    root = Path(__file__).resolve().parents[1]
    source_path = "packages/qt/mindmap-qt-render/src/mindmap_qt_render/rendering.py"
    source = subprocess.run(
        ["git", "show", f"{revision}:{source_path}"], cwd=root, check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    name = "mindmap_qt_render._text_rendering_baseline"
    module = ModuleType(name)
    module.__package__ = "mindmap_qt_render"
    sys.modules[name] = module
    exec(compile(source, f"<字体渲染基准:{revision}>", "exec"), module.__dict__)
    return module


def make_document() -> MindMapDocument:
    """构造普通中英文、混排、数字标点、字体回退、粗斜体及 Markdown 样例。"""
    document = MindMapDocument.new("推理与生成 Inference")
    for text, style, markdown in (
        ("Prefill：并行处理输入上下文，构建注意力缓存", {}, False),
        ("Decode: Token_0123 → ABC xyz / 0.75×", {}, False),
        ("中文细笔画：推理基本过程，清晰、平滑。", {}, False),
        ("Bold / Italic 粗体与斜体", {"bold": True, "italic": True}, False),
        ("Underline 下划线 ABC 0123", {"underline": True}, False),
        ("字体回退 😀 → 中文 / English", {"font_family": "Segoe UI"}, False),
        ("## Markdown 标题\n**粗体**与*斜体*，`token_id=42`\n- 中文条目\n- English text", {}, True),
    ):
        node = Node(text, style=style, text_format="markdown" if markdown else "plain")
        document.nodes[node.id] = node
        document.root.children.append(node.id)
    document.root.tags = ["中文标签 ABC"]
    document.root.expanded = False
    document.validate()
    return document


def layout_signature(visual) -> tuple:
    """记录实际换行和节点尺寸，验证绘制及像素密度变化不使缓存重新排版。"""
    lines, block = [], visual.text.begin()
    while block.isValid():
        layout = block.layout()
        lines.extend((layout.lineAt(i).textStart(), layout.lineAt(i).textLength(),
                      layout.lineAt(i).naturalTextWidth()) for i in range(layout.lineCount()))
        block = block.next()
    return visual.width, visual.height, visual.text.size(), tuple(lines)


def node_snapshot(visual, node: Node, zoom: float, dpr: float, background: str) -> QImage:
    """按指定画布倍率和设备像素比直接绘制节点，保留输出图片的真实物理像素。

    visual 是只读视觉，zoom 与 dpr 只作用于绘图设备及变换，不能写回字体或布局。
    正文四周预留逻辑像素留白，右侧另容纳折叠计数；返回值不经过二次缩放。
    """
    image = QImage(ceil((visual.width + 48) * zoom * dpr),
                   ceil((visual.height + 16) * zoom * dpr), QImage.Format.Format_RGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor(background))
    painter = QPainter(image)
    try:
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        painter.scale(zoom, zoom)
        painter.translate(8, 8)
        visual.paint(painter, node)
    finally:
        painter.end()
    # 对照图按物理像素直接拼接；清除显示缩放元数据，不改变任何图像像素。
    image.setDevicePixelRatio(1)
    return image


def comparison_image(columns: list[tuple[str, list[QImage]]], title: str, theme: Theme) -> QImage:
    """将同倍率的各组样例按原始像素并排排列，标题采用独立字体，不缩放样例。"""
    gap, header = 16, 78
    cell_width = max(image.width() for _, images in columns for image in images)
    heights = [max(images[row].height() for _, images in columns) for row in range(len(columns[0][1]))]
    output = QImage(gap + len(columns) * (cell_width + gap),
                    header + sum(height + gap for height in heights), QImage.Format.Format_RGB32)
    output.fill(QColor(theme.background))
    painter = QPainter(output)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = QFont(QApplication.font())
        font.setPixelSize(16)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        painter.setFont(font)
        painter.setPen(QColor(theme.text))
        painter.drawText(QPointF(gap, 24), title)
        for column, (label, images) in enumerate(columns):
            x, y = gap + column * (cell_width + gap), header
            painter.drawText(QPointF(x, 56), label)
            for row, image in enumerate(images):
                painter.drawImage(QPointF(x, y), image)
                y += heights[row] + gap
    finally:
        painter.end()
    return output


def verify_editor(app: QApplication) -> list[dict]:
    """在当前原生平台的隐藏画布中验证编辑几何、输入法候选、提交和取消。

    app 为本次验收唯一的 QApplication；生成独立引擎，不加载文件、不使用剪贴板。
    每个倍率都执行真实文本控件事件及引擎撤销，返回中文记录，失败直接抛出异常。
    隐藏画布在完成或异常时都取消草稿并释放，不改变用户窗口、导图或系统偏好。
    """
    engine = MindMapEngine()
    text = "中英文 Prefill → Decode 0123，输入法与换行验证 😀。"
    child = engine.add_child(engine.root_id, text)
    engine.update_node(child, style={"font_size": 11.5, "bold": True, "italic": True, "width": 160})
    canvas = MindMapWidget(engine, toolbar=False)
    canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    canvas.resize(1000, 700)
    canvas.show()
    app.processEvents()
    records = []
    try:
        for zoom in ZOOMS:
            canvas.view.zoom(zoom / canvas.view.transform().m11())
            visual = canvas.node_items[child].visual
            canvas.begin_edit(child)
            editor = canvas.editor
            editor.sync_geometry(force=True)
            assert editor.document().size() == visual.text.size(), "进入编辑后换行或文本尺寸发生变化"
            assert (editor.preview_visual.width, editor.preview_visual.height) == (visual.width, visual.height)
            canvas.scene.sendEvent(editor, QInputMethodEvent("输入法候选 ABC" * 12, []))
            assert editor.composing and engine.get_node(child).text == text, "候选提前写入文档"
            canvas.scene.sendEvent(editor, QInputMethodEvent())
            canvas.finish_edit(commit=False)
            assert canvas.node_items[child].visual is visual, "取消编辑未恢复正式视觉"
            canvas.begin_edit(child)
            canvas.editor.setPlainText(f"倍率 {zoom:g} 下的混排编辑 ABC" * 3)
            preview = canvas.editor.preview_visual
            canvas.finish_edit(commit=True)
            actual = canvas.node_items[child].visual
            assert (actual.width, actual.height) == (preview.width, preview.height), "确认编辑后几何发生跳变"
            engine.undo()
            assert engine.get_node(child).text == text, "撤销未恢复原始正文"
            assert not canvas.view.viewport().grab().isNull()
            records.append({"画布倍率": zoom, "编辑几何一致": True, "候选与正式文档隔离": True,
                            "确认取消及撤销": "通过"})
    finally:
        canvas.finish_edit(commit=False)
        canvas.dispose()
        canvas.close()
        canvas.deleteLater()
        app.processEvents()
    return records


def main() -> int:
    """运行明暗主题、完整倍率矩阵和图形导出，任一检查失败则返回非零状态。"""
    parser = argparse.ArgumentParser(description="中英文多倍率字体渲染验收")
    parser.add_argument("--output", type=Path, default=Path(".artifacts/text-rendering"), help="验收输出目录")
    parser.add_argument("--platform", default="windows" if sys.platform == "win32" else "offscreen",
                        help="Qt 平台，Windows 视觉验收应使用 windows")
    parser.add_argument("--baseline-ref", help="可选的旧版本 Git 引用，例如修改前的提交哈希")
    arguments = parser.parse_args()
    app = QApplication(["字体渲染验收", "-platform", arguments.platform])
    rendering.ensure_fonts()
    arguments.output.mkdir(parents=True, exist_ok=True)
    versions = []
    if arguments.baseline_ref:
        versions.append(("优化前", load_baseline(arguments.baseline_ref).VisualFactory()))
    versions.append(("优化后", rendering.VisualFactory()))
    document = make_document()
    records, exports = [], []
    for dark in (False, True):
        theme = Theme.dark() if dark else Theme()
        if not dark:
            theme.background = "#ffffff"
        document.theme = theme
        theme_name = "深色" if dark else "浅色"
        groups = [(label, factory, factory.measure(document)[0]) for label, factory in versions]
        signatures = {(label, key): layout_signature(visual)
                      for label, _, visuals in groups for key, visual in visuals.items()}
        for dpr in PIXEL_RATIOS:
            for zoom in ZOOMS:
                columns = []
                for label, factory, visuals in groups:
                    images = []
                    for key, node in document.nodes.items():
                        visual = visuals[key]
                        images.append(node_snapshot(visual, node, zoom, dpr, theme.background))
                        assert layout_signature(visual) == signatures[label, key], "绘制改变了文本布局"
                        assert factory.visual(node, theme, document, is_root=key == document.root_id) is visual
                    columns.append((label, images))
                title = f"{theme_name} · 屏幕 {dpr:.0%} · 画布 {zoom:.0%} · 原始像素对照"
                target = arguments.output / f"{theme_name}-屏幕{dpr:.0%}-画布{zoom:.0%}.png"
                assert comparison_image(columns, title, theme).save(str(target)), "对照图写入失败"
                records.append({"主题": theme_name, "屏幕缩放": dpr, "画布倍率": zoom,
                                "样例数": len(document.nodes), "对照图": str(target.resolve())})
        registry = create_default_layout_registry()
        for suffix, scale in (("png", 0.75), ("png", 1), ("png", 1.5), ("png", 2), ("svg", 1), ("pdf", 1)):
            target = arguments.output / f"{theme_name}-导出-{scale:g}.{suffix}"
            export_document(document, target, layout_registry=registry, expand_all=True, scale=scale)
            if suffix == "png":
                assert not QImage(str(target)).isNull(), "导出的 PNG 无法解码"
            elif suffix == "svg":
                assert b"<svg" in target.read_bytes() and b"<text" in target.read_bytes(), "SVG 丢失矢量文字"
            else:
                assert target.read_bytes().startswith(b"%PDF"), "PDF 格式无效"
            exports.append(str(target.resolve()))
    report = {
        "Qt版本": qVersion(), "Qt平台": app.platformName(), "基准Git引用": arguments.baseline_ref,
        "系统字体": QFontInfo(app.font()).family(),
        "原生屏幕像素比": [screen.devicePixelRatio() for screen in app.screens()],
        "布局及缓存检查": "所有倍率均复用原有视觉，换行及节点尺寸在绘制期间保持稳定",
        "查看方式": "按图片原始像素查看，每个文件左右使用相同画布倍率和屏幕缩放；标题不参与样例缩放",
        "对照图数量": len(records), "图形导出数量": len(exports), "矩阵": records, "导出样例": exports,
        "原生编辑验收": verify_editor(app),
    }
    target = arguments.output / "验收记录.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"验收记录": str(target.resolve()), "对照图数量": len(records),
                      "图形导出数量": len(exports)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
