"""节点拖动的千节点性能与明暗主题视觉验收，输出中文报告和高 DPI 截图。

在仓库根目录执行：
uv run --locked --all-packages python scripts/verify_node_drag.py --scale-factor 1.5
每档显示缩放使用独立进程；脚本只创建隐藏的验收画布，不操作用户文档。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

from benchmark import build_document
from mindmap_core import LayoutConfig, MindMapEngine, Theme
from mindmap_qt_canvas import MindMapWidget
from PySide6.QtCore import QAbstractAnimation, QBuffer, QIODevice, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication


def create_canvas(app, engine):
    """隐藏窗口仍完成布局和绘制，计时不包含窗口建立与文字、图片的首次测量。"""
    widget = MindMapWidget(engine, toolbar=False)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.resize(1280, 800)
    widget.show()
    widget.view.setFocus()
    app.processEvents()
    widget.view.resetTransform()
    widget.view.scale(0.7, 0.7)
    widget.view.viewport().grab()
    return widget


def body(widget, key, fraction=0.5):
    """选取固定节点的正文；整个正文都表示接为子主题。"""
    item = widget.node_items[key]
    down = widget._document.layout.direction == "down"
    local = QPointF(
        item.visual.width * (fraction if down else 0.5), item.visual.height * (0.5 if down else fraction)
    )
    return widget.view.mapFromScene(item.mapToScene(local))


def after(widget, key):
    """从主题外缘向外取 12 个视口像素，向下布局对应右侧排序区。"""
    rect = widget.layout_result.nodes[key]
    offset = 12 / widget.view.transform().m11()
    point = (
        QPointF(rect.right + offset, rect.center[1])
        if widget._document.layout.direction == "down"
        else QPointF(rect.center[0], rect.bottom + offset)
    )
    return widget.view.mapFromScene(point)


def percentile(values):
    """采用最近秩 P95，同时报告中位数和最大值，便于识别偶发系统调度抖动。"""
    return round(sorted(values)[max(0, int(len(values) * 0.95 + 0.999999) - 1)], 2)


def benchmark(app, count):
    document = build_document(count)
    widget = create_canvas(app, MindMapEngine(document))
    source = list(document.nodes)[-1]
    # 从相邻的末端叶子中取目标，避免把超出视口的坐标误计为“无落点”的快速路径。
    leaves = [key for key, node in document.nodes.items() if not node.children and key != source]
    targets = leaves[-15:]
    layout_calls = []
    original_layout = widget.layout_registry.layout

    def tracked_layout(*args, **kwargs):
        layout_calls.append(1)
        return original_layout(*args, **kwargs)

    widget.layout_registry.layout = tracked_layout
    candidates, following, sorting = [], [], []
    positions = {key: item.pos() for key, item in widget.node_items.items()}
    try:
        for index, target in enumerate(targets):
            a, b = document.nodes[source], document.nodes[target]
            widget.select_nodes([a.id])
            rect_a, rect_b = widget.layout_result.nodes[a.id], widget.layout_result.nodes[b.id]
            widget.view.centerOn(
                QPointF((rect_a.center[0] + rect_b.center[0]) / 2, (rect_a.center[1] + rect_b.center[1]) / 2)
            )
            origin, end = body(widget, a.id), body(widget, b.id)
            control = widget.view.node_drag
            assert control.begin(widget.view.mapToScene(origin))
            started = time.perf_counter()
            control.update(end)
            elapsed = (time.perf_counter() - started) * 1000
            assert control.target is not None, "性能样本必须有有效结构落点"
            if index >= 3:
                candidates.append(elapsed)
            for step in range(8):
                started = time.perf_counter()
                control.update(end + QPoint(step % 2, 0))
                following.append((time.perf_counter() - started) * 1000)
            # 正文走优先命中，节点外排序还需要比较扩大后的落区；两条路径都计时。
            # 某些样本恰好回到源节点原顺序，应按空操作跳过，不能冒充有效排序样本。
            started = time.perf_counter()
            control.update(after(widget, target))
            elapsed = (time.perf_counter() - started) * 1000
            if control.target is not None and control.target.position != "inside" and index >= 3:
                sorting.append(elapsed)
            assert not layout_calls, "整个拖动阶段均不应重算布局，包括切换候选"
            assert control._animation.state() == QAbstractAnimation.State.Stopped
            assert positions == {key: item.pos() for key, item in widget.node_items.items()}
            widget.view.viewport().grab()
            control.cancel()
        assert widget.engine.document == document and not widget.engine.can_undo
        return {
            "节点数": count,
            "候选样本数": len(candidates),
            "候选更新中位毫秒": round(statistics.median(candidates), 2),
            "候选更新P95毫秒": percentile(candidates),
            "候选更新最大毫秒": round(max(candidates), 2),
            "稳定跟随P95毫秒": percentile(following),
            "同级排序样本数": len(sorting),
            "同级排序P95毫秒": percentile(sorting),
            "候选更新达标": percentile(candidates) < 16,
            "稳定跟随达标": percentile(following) < 16,
            "同级排序达标": percentile(sorting) < 16,
            "整个拖动期间不重算布局": True,
            "拖动期间所有节点位置固定": True,
            "正式文档及历史未改变": True,
        }
    finally:
        widget.dispose()
        widget.close()


def render_examples(app, directory, scale):
    """四方向明暗主题验收：悬停不展开、放下才动画及展开、同级插入提示。"""
    screenshots = []
    image = QImage(120, 56, QImage.Format.Format_ARGB32)
    image.fill(QColor("#42a5a0"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    names = {"right": "向右", "left": "向左", "both": "双向", "down": "向下"}
    for direction in names:
        for dark in (False, True):
            engine = MindMapEngine()
            engine.update_node(engine.root_id, text="产品迭代计划")
            source = engine.add_child(engine.root_id, "交互设计")
            target = engine.add_child(engine.root_id, "开发与验收")
            other = engine.add_child(engine.root_id, "需求分析")
            child = engine.add_child(source, "整条分支拖动\n保留文字、层级和图片")
            engine.attach_image(child, bytes(buffer.data()), width=120)
            hidden = engine.add_child(target, "回归验证")
            engine.add_child(other, "访谈与需求整理")
            engine.set_expanded(target, False)
            engine.set_layout(LayoutConfig(direction=direction))
            if dark:
                engine.set_theme(Theme.dark())
            engine.replace_document(engine.document)
            widget = create_canvas(app, engine)
            try:
                widget.view.scale(1 / 0.7, 1 / 0.7)
                widget.view.update_navigation_rect(center=QPointF(*widget.layout_result.bounds.center))
                widget.select_nodes([source])
                control = widget.view.node_drag
                origin, end = body(widget, source), body(widget, target)
                positions = {key: item.pos() for key, item in widget.node_items.items()}
                assert control.begin(widget.view.mapToScene(origin))
                control.update(end)
                assert control.target is not None and control.target.position == "inside"
                assert control._hint_text() == "＋ 作为子主题"
                assert not control._connection_hint().isEmpty() and control._insertion_hint().isEmpty()
                assert positions == {key: item.pos() for key, item in widget.node_items.items()}
                assert hidden not in widget.node_items and not widget.node_items[target].node.expanded
                assert control._animation.state() == QAbstractAnimation.State.Stopped
                label = "深色" if dark else "浅色"
                path = directory / f"节点拖动-{names[direction]}-{label}-{scale:g}.png"
                assert widget.grab().save(str(path))
                screenshots.append(str(path.resolve()))
                # 真正放下后再展示动画中间帧和最终状态，并验证展开和移动共同撤销。
                drop = control.finish(end)
                assert drop is not None and control.commit(drop)
                assert hidden in widget.node_items and engine.get_node(target).expanded
                assert control._animation.state() == QAbstractAnimation.State.Running
                control._animation.setCurrentTime(35)
                path = directory / f"放下重排-{names[direction]}-{label}-{scale:g}.png"
                assert widget.grab().save(str(path))
                screenshots.append(str(path.resolve()))
                control._animation.setCurrentTime(140)
                assert control._reflow is None
                path = directory / f"放下完成-{names[direction]}-{label}-{scale:g}.png"
                assert widget.grab().save(str(path))
                screenshots.append(str(path.resolve()))
                assert engine.undo()
                assert not engine.get_node(target).expanded
                # 再验收同级间隙短条；排序不绘制目标高亮或预连接提示。
                origin, end = body(widget, source), after(widget, other)
                assert control.begin(widget.view.mapToScene(origin))
                control.update(end)
                assert control.target is not None and control.target.position == "after"
                assert control._hint_text() == ("→ 插到右侧" if direction == "down" else "↓ 插到后面")
                assert control._connection_hint().isEmpty() and not control._insertion_hint().isEmpty()
                assert positions == {key: item.pos() for key, item in widget.node_items.items()}
                path = directory / f"节点排序-{names[direction]}-{label}-{scale:g}.png"
                assert widget.grab().save(str(path))
                screenshots.append(str(path.resolve()))
                control.cancel()
            finally:
                widget.dispose()
                widget.close()
    return screenshots


def run():
    parser = argparse.ArgumentParser(description="节点拖动性能与高 DPI 视觉验收")
    parser.add_argument("--nodes", type=int, default=1000, help="性能验收节点数量，至少 50")
    parser.add_argument(
        "--scale-factor",
        type=float,
        choices=(1, 1.5, 2),
        default=1,
        help="Qt 显示缩放，独立进程分别执行 1、1.5、2",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".artifacts/node-drag"), help="中文报告与截图的输出目录"
    )
    args = parser.parse_args()
    if args.nodes < 50:
        parser.error("性能验收至少需要 50 个节点")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["QT_SCALE_FACTOR"] = str(args.scale_factor)
    app = QApplication([])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "Qt平台": app.platformName(),
        "显示缩放": args.scale_factor,
        "性能": benchmark(app, args.nodes),
        "截图": render_examples(app, args.output_dir, args.scale_factor),
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    (args.output_dir / f"验收-{args.scale_factor:g}.json").write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if all(result["性能"][key] for key in ("候选更新达标", "稳定跟随达标", "同级排序达标")) else 1


if __name__ == "__main__":
    raise SystemExit(run())
