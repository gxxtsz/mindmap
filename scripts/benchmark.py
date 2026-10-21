"""可复现的千节点基准，不依赖桌面应用入口。

在仓库根目录执行 uv run --locked --all-packages python scripts/benchmark.py。
计时包含引擎通知、布局和 Qt 事件刷新，不包含环境安装。
"""

import argparse
import json
import platform
import statistics
import time
from copy import deepcopy
from pathlib import Path

import PySide6
from mindmap_core import MindMapDocument, MindMapEngine, Node
from mindmap_qt_canvas import MindMapWidget
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def build_document(count=1000):
    """构造五叉树，标签长度接近普通中文导图；直接建模避免把造数据算入基准。"""
    document = MindMapDocument.new("千节点性能验证")
    parents = [document.root_id]
    for index in range(1, count):
        parent = document.nodes[parents[(index - 1) // 5]]
        child = Node(f"节点 {index:04}：记录想法与行动", style=deepcopy(parent.style))
        parent.children.append(child.id)
        document.nodes[child.id] = child
        parents.append(child.id)
    document.validate()
    return document


def run():
    parser = argparse.ArgumentParser(description="思维导图性能验收")
    parser.add_argument("--nodes", type=int, default=1000, help="生成节点数量")
    parser.add_argument("--output", type=Path, help="将结果写入 JSON 文件")
    arguments = parser.parse_args()
    if arguments.nodes < 2:
        parser.error("节点数量至少为 2")
    app = QApplication([])
    document = build_document(arguments.nodes)
    start = time.perf_counter()
    engine = MindMapEngine(document)
    widget = MindMapWidget(engine)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.resize(1280, 800)
    widget.show()
    widget.fit_to_view()
    app.processEvents()
    widget.view.viewport().grab()
    first_ms = (time.perf_counter() - start) * 1000
    keys = list(document.nodes)
    timings = []
    for index in range(23):
        start = time.perf_counter()
        engine.update_node(keys[len(keys) // 2], text=f"节点更新 {index:04}：记录想法与行动")
        app.processEvents()
        # 隐藏测试窗口也强制绘制视口，避免只计布局和重绘请求而漏掉实际绘制耗时。
        widget.view.viewport().grab()
        if index >= 3:
            timings.append((time.perf_counter() - start) * 1000)
    p95 = sorted(timings)[int(len(timings) * 0.95) - 1]
    result = {
        "节点数": arguments.nodes,
        "可见图元数": len(widget.node_items),
        "Python": platform.python_version(),
        "PySide6": PySide6.__version__,
        "Qt平台": app.platformName(),
        "设备像素比": widget.devicePixelRatioF(),
        "系统": platform.platform(),
        "处理器": platform.processor(),
        "首次布局显示毫秒": round(first_ms, 2),
        "编辑刷新中位毫秒": round(statistics.median(timings), 2),
        "编辑刷新P95毫秒": round(p95, 2),
        "编辑刷新最大毫秒": round(max(timings), 2),
        "首次显示达标": first_ms <= 2000,
        "编辑刷新达标": max(timings) <= 200,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text + "\n", encoding="utf-8")
    widget.dispose()
    widget.close()
    return 0 if result["首次显示达标"] and result["编辑刷新达标"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
