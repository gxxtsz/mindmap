"""原生分支拖入的四方向、明暗、紧凑、高 DPI 及大图验收，只使用临时内存文档。

在仓库根目录执行 uv run --locked --all-packages python scripts/verify_branch_transfer_drag.py。
三个子进程分别验证 100%、150%、200% 显示缩放，中文报告和截图写入忽略的产物目录。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def send_drag(widget, mime, point, *, drop=False, enter=False):
    """经 Qt 视口分派原生事件，保留实际事件路由、接受状态及释放提交顺序。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    from PySide6.QtWidgets import QApplication

    if drop:
        event = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    else:
        event_type = QDragEnterEvent if enter else QDragMoveEvent
        event = event_type(point, Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget.view.viewport(), event)
    return event.isAccepted()


def examples(app, directory, scale):
    """每种配置截图接为子主题、同级插入和动画中间帧，并核对单次撤销和样式保留。"""
    from mindmap_core import LayoutConfig, MindMapEngine, Theme
    from PySide6.QtCore import QAbstractAnimation
    from verify_node_drag import after, body, create_canvas

    reports = []
    names = {"right": "向右", "left": "向左", "both": "双向", "down": "向下"}
    for direction, direction_name in names.items():
        for dark in (False, True):
            for compact in (False, True):
                engine = MindMapEngine()
                engine.update_node(engine.root_id, text="项目规划")
                target = engine.add_child(engine.root_id, "接收主题")
                hidden = engine.add_child(target, "放下之后才展开")
                other = engine.add_child(engine.root_id, "另一主题")
                engine.set_expanded(target, False)
                source = engine.add_inspiration("来自灵感侧栏", position=(800, -400))
                engine.update_node(source, style={"font_size": 14, "bold": True, "text_color": "#bc627b"})
                child = engine.add_child(source, "保留原样式和完整内容")
                engine.create_detail_page(child)
                engine.set_layout(LayoutConfig(direction=direction, compact=compact))
                if direction == "both":
                    engine.update_node(target, direction="right")
                    engine.update_node(other, direction="right")
                if dark:
                    engine.set_theme(Theme.dark())
                engine.replace_document(engine.document)
                widget = create_canvas(app, engine)
                label = f"{direction_name}-{'深色' if dark else '浅色'}-{'紧凑' if compact else '普通'}"
                try:
                    widget.center_root()
                    widget.select_nodes([other])
                    control = widget.view.node_drag
                    original, revision = engine.document, engine.revision
                    positions = {key: item.pos() for key, item in widget.node_items.items()}
                    mime = widget.create_branch_drag([source], origin="sidebar")
                    point = body(widget, target)
                    assert send_drag(widget, mime, point, enter=True)
                    assert send_drag(widget, mime, point)
                    assert control.target.position == "inside" and control._hint_text() == "＋ 作为子主题"
                    assert not control._connection_hint().isEmpty() and control._insertion_hint().isEmpty()
                    assert hidden not in widget.node_items and not engine.get_node(target).expanded
                    assert positions == {key: item.pos() for key, item in widget.node_items.items()}
                    assert engine.document == original and engine.revision == revision
                    assert widget.grab().save(str(directory / f"{label}-子主题提示.png"))
                    point = after(widget, other)
                    assert send_drag(widget, mime, point)
                    assert control.target.position == "after"
                    assert control._hint_text() == ("→ 插到右侧" if direction == "down" else "↓ 插到后面")
                    assert control._connection_hint().isEmpty() and not control._insertion_hint().isEmpty()
                    assert widget.grab().save(str(directory / f"{label}-同级插入.png"))
                    point = body(widget, target)
                    assert send_drag(widget, mime, point, drop=True)
                    assert engine.revision == revision + 1 and engine.parent_id(source) == target
                    assert engine.get_node(source).style == original.nodes[source].style
                    assert hidden in widget.node_items and engine.get_node(target).expanded
                    assert control._animation.state() == QAbstractAnimation.State.Running
                    widget.cancel_branch_transfer_preview()
                    assert control._animation.state() == QAbstractAnimation.State.Running
                    control._animation.setCurrentTime(35)
                    assert widget.grab().save(str(directory / f"{label}-重排动画.png"))
                    control._animation.setCurrentTime(140)
                    engine.undo()
                    assert engine.document == original and widget.selected_node_ids() == [other]
                    engine.redo()
                    assert engine.parent_id(source) == target
                    assert abs(widget.devicePixelRatioF() - scale) < 0.01
                    reports.append({"配置": label, "通过": True})
                finally:
                    widget.dispose()
                    widget.close()
    return reports


def benchmark(app):
    """千节点目标连续移动时不测量、不布局，成功放下不启动大图动画。"""
    from benchmark import build_document
    from mindmap_core import MindMapEngine
    from PySide6.QtCore import QAbstractAnimation
    from verify_node_drag import body, create_canvas, percentile

    engine = MindMapEngine(build_document(1100))
    source = engine.add_inspiration("大图中的独立想法", position=(0, 0))
    engine.add_child(source, "完整分支")
    engine.replace_document(engine.document)
    widget = create_canvas(app, engine)
    try:
        widget.center_root()
        original = engine.document
        mime = widget.create_branch_drag([source], origin="sidebar")
        point = body(widget, engine.root_id)
        assert send_drag(widget, mime, point, enter=True)
        control = widget.view.node_drag
        source_layout, target_layout = control._source.layout, widget.layout_result
        calls, timings = [], []
        layout, measure = widget.layout_registry.layout, widget.factory.measure

        def counted_layout(*args, **kwargs):
            calls.append("布局")
            return layout(*args, **kwargs)

        def counted_measure(*args, **kwargs):
            calls.append("测量")
            return measure(*args, **kwargs)

        widget.layout_registry.layout, widget.factory.measure = counted_layout, counted_measure
        try:
            candidates = [key for key in widget.node_items
                          if widget.view.viewport().rect().contains(body(widget, key))][:12]
            for _ in range(5):
                for target in candidates:
                    started = time.perf_counter()
                    send_drag(widget, mime, body(widget, target))
                    timings.append((time.perf_counter() - started) * 1000)
                    widget.view.viewport().grab()
        finally:
            widget.layout_registry.layout, widget.factory.measure = layout, measure
        assert not calls and engine.document == original
        assert control._source.layout is source_layout and widget.layout_result is target_layout
        assert send_drag(widget, mime, point, drop=True)
        assert control._animation.state() == QAbstractAnimation.State.Stopped
        assert engine.parent_id(source) == engine.root_id
        engine.undo()
        assert engine.document == original
        return {"目标节点数": 1100, "连续移动P95毫秒": percentile(timings),
                "悬停不测量或布局": True, "源影像和目标几何复用": True, "大图跳过动画": True, "通过": True}
    finally:
        widget.dispose()
        widget.close()


def main():
    """隔离各档 Qt 显示缩放，所有文件仅写入专用验收目录。"""
    parser = argparse.ArgumentParser(description="原生分支拖入统一交互验收")
    parser.add_argument("--child", type=float, choices=(1, 1.5, 2))
    options = parser.parse_args()
    if options.child:
        from PySide6.QtWidgets import QApplication

        app = QApplication([])
        directory = ROOT / ".artifacts" / "branch-transfer-drag" / f"{options.child:g}x"
        directory.mkdir(parents=True, exist_ok=True)
        report = {"显示缩放": options.child, "界面组合": examples(app, directory, options.child),
                  "大图": benchmark(app), "通过": True}
        (directory / "验收报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"显示缩放": options.child, "界面组合数量": len(report["界面组合"]),
                          "大图": report["大图"], "通过": True}, ensure_ascii=False), flush=True)
        return 0
    passed = True
    for scale in (1, 1.5, 2):
        environment = os.environ.copy()
        environment.update(QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=str(scale), PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, __file__, "--child", str(scale)], cwd=ROOT, env=environment,
                                capture_output=True, timeout=60,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        print(result.stdout.decode("utf-8", errors="replace").strip(), flush=True)
        passed &= result.returncode == 0
        if result.returncode:
            print(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
