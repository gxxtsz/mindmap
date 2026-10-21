"""离屏检查四方向、明暗主题的折叠按钮，输出完整画布截图及按钮对照图。

在仓库根目录执行：
uv run --locked --all-packages python scripts/verify_fold_controls.py
验收只创建独立的隐藏画布，不读取、改写用户导图或本机偏好。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mindmap_core import LayoutConfig, MindMapEngine, Theme
from mindmap_qt_canvas import MindMapWidget
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication


def make_engine(direction, dark):
    """用参考图中的主题构造验收文档，第二条分支收起且恰有七个直接子节点。"""
    engine = MindMapEngine()
    engine.update_node(engine.root_id, text="TCP Socket")
    expanded = engine.add_child(engine.root_id, "16.11 TCP 编程问题")
    for text in (
        "粘包不是 TCP 协议错误", "TCP 是字节流而非消息协议", "需要自定义消息边界",
        "固定长度协议", "长度前缀协议", "分隔符协议", "半关闭连接", "半开连接",
        "连接超时", "短读与短写", "SIGPIPE 与 EPIPE",
    ):
        engine.add_child(expanded, text)
    collapsed = engine.add_child(engine.root_id, "16.12 非阻塞 Socket")
    for index in range(7):
        child = engine.add_child(collapsed, f"非阻塞主题 {index + 1}")
        if index == 0:
            engine.add_child(child, "这名孙节点不计入圆圈数字")
    engine.set_expanded(collapsed, False)
    theme = Theme.dark() if dark else Theme()
    theme.border = "#efb547" if dark else "#e9a526"
    theme.palette = [theme.border]
    if not dark:
        theme.background = "#ffffff"
    engine.set_theme(theme)
    engine.set_layout(LayoutConfig(direction=direction, compact=True))
    engine.replace_document(engine.document)
    return engine, expanded, collapsed


def node_snapshot(canvas, key):
    """从真实交互场景截取正文和外侧按钮，按固定卡片尺寸展示局部细节。"""
    item = canvas.node_items[key]
    area = item.sceneBoundingRect().united(item.fold_control.sceneBoundingRect()).adjusted(-8, -8, 8, 8)
    image = QImage(286, 104, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(canvas.scene.backgroundBrush().color())
    scale = min(1.4, (image.width() - 12) / area.width(), (image.height() - 12) / area.height())
    target = QRectF(
        (image.width() - area.width() * scale) / 2, (image.height() - area.height() * scale) / 2,
        area.width() * scale, area.height() * scale,
    )
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    canvas.scene.render(painter, target, area)
    painter.end()
    return image


def main():
    parser = argparse.ArgumentParser(description="折叠按钮的离屏视觉验收")
    parser.add_argument("--output", type=Path, default=Path(".artifacts/fold-controls"), help="截图输出目录")
    arguments = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    arguments.output.mkdir(parents=True, exist_ok=True)
    overview = QImage(1200, 720, QImage.Format.Format_ARGB32_Premultiplied)
    overview.fill(QColor("#e7e9ed"))
    records = []
    for row, (direction, title) in enumerate((
        ("right", "向右"), ("left", "向左"), ("both", "双向"), ("down", "向下"),
    )):
        for column, dark in enumerate((False, True)):
            engine, expanded, collapsed = make_engine(direction, dark)
            canvas = MindMapWidget(engine, toolbar=False)
            canvas.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            canvas.resize(1200, 850)
            canvas.show()
            app.processEvents()
            canvas.select_nodes([collapsed])
            canvas.fit_to_view()
            try:
                expanded_item, collapsed_item = canvas.node_items[expanded], canvas.node_items[collapsed]
                assert not expanded_item.fold_control.isVisible()
                if expanded_item.fold_control.direction == "left":
                    local = QPointF(-11.2, expanded_item.visual.height / 2)
                elif direction == "down":
                    local = QPointF(expanded_item.visual.width / 2, expanded_item.visual.height + 11.2)
                else:
                    local = QPointF(expanded_item.visual.width + 11.2, expanded_item.visual.height / 2)
                pointer = canvas.view.mapFromScene(expanded_item.mapToScene(local))
                event = QMouseEvent(
                    QEvent.Type.MouseMove, QPointF(pointer), QPointF(canvas.view.viewport().mapToGlobal(pointer)),
                    Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                )
                QApplication.sendEvent(canvas.view.viewport(), event)
                assert expanded_item.fold_control.isVisible()
                assert not expanded_item.isSelected()
                assert collapsed_item.fold_control.isVisible()
                assert collapsed_item.fold_control.toolTip() == "展开 7 个子节点"
                assert not expanded_item.shape().contains(expanded_item.fold_control.button_rect().center())
                prefix = f"{direction}-{'dark' if dark else 'light'}"
                hover_path = arguments.output / f"{prefix}-line-hover.png"
                assert canvas.view.viewport().grab().save(str(hover_path))
                first, second = node_snapshot(canvas, expanded), node_snapshot(canvas, collapsed)
                canvas.select_nodes([expanded])
                path = arguments.output / f"{prefix}.png"
                assert canvas.view.viewport().grab().save(str(path))
                painter = QPainter(overview)
                x, y = column * 600, row * 180
                painter.fillRect(x + 6, y + 6, 588, 168, canvas.scene.backgroundBrush())
                font = QFont(app.font())
                font.setPointSizeF(11)
                painter.setFont(font)
                painter.setPen(QColor(engine.document.theme.text))
                painter.drawText(QRectF(x + 18, y + 14, 560, 25), f"{title} · {'深色' if dark else '浅色'}")
                painter.drawImage(x + 12, y + 42, first)
                painter.drawImage(x + 302, y + 42, second)
                font.setPointSizeF(9)
                painter.setFont(font)
                painter.drawText(QRectF(x + 20, y + 145, 270, 20), "展开：在线旁悬停显示减号")
                painter.drawText(QRectF(x + 310, y + 145, 270, 20), "收起：显示 7 个直接子节点")
                painter.end()
                records.append({
                    "方向": title, "主题": "深色" if dark else "浅色", "截图": str(path.resolve()),
                    "线旁悬停截图": str(hover_path.resolve()),
                })
            finally:
                canvas.dispose()
                canvas.close()
                canvas.deleteLater()
                app.processEvents()
    summary = arguments.output / "fold-controls-overview.png"
    assert overview.save(str(summary))
    report = {"验收通过": True, "截图": records, "对照图": str(summary.resolve())}
    (arguments.output / "验收报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
