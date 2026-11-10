"""全选到首帧及全选后交互的本机基准；使用生成导图与独立配置，不读取用户文件。

在仓库根目录执行：
uv run --locked --all-packages python plugins/workbench-plugin-mindmap/scripts/benchmark_selection.py
默认测试 1000、3000、10000 节点的五叉树/宽树，分别运行独立画布与完整工作台。
计时包括真实 Ctrl+A、同步面板、Qt 事件处理及完整视口绘制，不含造数据和打开文件。
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
from pathlib import Path
from time import perf_counter, time_ns

import PySide6
from benchmark import build_document
from mindmap_core import MindMapEngine
from mindmap_qt_editor.canvas import MindMapWidget
from personal_workbench.app import build_window
from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def summary(values):
    """保留原始样本，按最近秩法计算 P95；不把耗时阈值放入 CI。"""
    ordered = sorted(values)
    return {"中位毫秒": round(statistics.median(values), 3),
            "P95毫秒": round(ordered[(len(values) * 95 + 99) // 100 - 1], 3),
            "最大毫秒": round(max(values), 3), "样本毫秒": [round(value, 3) for value in values]}


class PaintCounter(QObject):
    """观察实际视口绘制；事件循环已完成绘制时不再额外抓图重复计算一帧。"""

    def __init__(self, viewport):
        super().__init__(viewport)
        self.count = 0
        viewport.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


def measure_case(app, count, shape, host, samples, frames, data, zoom):
    """在相同窗口、指定初始缩放及屏内不超过 300 节点的条件下测量一个场景。"""
    document = build_document(count)
    keys = list(document.nodes)
    if shape == "wide":
        for node in document.nodes.values():
            node.children = []
        document.root.children = keys[1:]
        document.validate()
    session = None
    if host == "workbench":
        window = build_window(storage_dir=data / "配置", restore=False)
        assert window.new_document(data / "生成导图.mindmap")
        session = window.plugins.instance("mindmap").active_session
        canvas = session.canvas
        session.engine.replace_document(document)
        window.manager.activate("outline", focus=False)
        window.manager.activate("properties", focus=False)
    else:
        window = canvas = MindMapWidget(MindMapEngine(document), toolbar=False)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1440, 900)
    window.show()
    QApplication.setActiveWindow(window)
    canvas.view.setFocus()
    paints = PaintCounter(canvas.view.viewport())

    def settle():
        if session is not None:
            session.document.timer.stop()
        app.processEvents()
        if session is not None:
            session.document.timer.stop()

    def render(since=None):
        previous = paints.count if since is None else since
        settle()
        # processEvents 返回时对应 paintEvent 已执行完成；没有自动重绘的平台才
        # 使用抓图补绘，避免将同一操作的两个完整帧相加后与单帧指标比较。
        if paints.count == previous:
            canvas.view.viewport().grab()
        assert paints.count > previous, "计时必须包含真实视口绘制"

    try:
        settle()
        canvas.view.resetTransform()
        canvas.view.scale(zoom, zoom)
        center = QPointF(*canvas.layout_result.nodes[keys[-30]].center)
        canvas.view.centerOn(center)
        canvas._viewport_changed()
        render()
        before_items = len(canvas.node_items)
        select_times, command_times = [], []
        for index in range(samples + 1):
            canvas.select_nodes([])
            render()
            previous = paints.count
            started = perf_counter()
            QTest.keyClick(canvas.view.viewport(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            command = (perf_counter() - started) * 1000
            assert canvas.selection_count == count, "Ctrl+A 必须经过真实快捷键入口"
            render(previous)
            elapsed = (perf_counter() - started) * 1000
            if index:
                command_times.append(command)
                select_times.append(elapsed)
        if session is not None:
            assert len(session.panels.properties._ids) == count
            assert session.panels.outline.selectionModel().isSelected(session.panels.outline.tree_model.index_for(keys[-1]))
        maximum_items, maximum_visible = len(canvas.node_items), 0
        frame_results = {}
        for operation in ("平移", "缩放", "鼠标移动"):
            times = []
            for index in range(frames + 2):
                previous = paints.count
                started = perf_counter()
                if operation == "平移":
                    bar = canvas.view.verticalScrollBar()
                    bar.setValue(bar.value() + (20 if index % 2 else -20))
                elif operation == "缩放":
                    canvas.view.zoom(1.15 if index % 2 else 1 / 1.15,
                                     anchor=canvas.view.viewport().rect().center())
                else:
                    point = QPointF(20 + index % 2, 20)
                    event = QMouseEvent(QEvent.Type.MouseMove, point, point, Qt.MouseButton.NoButton,
                                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
                    QApplication.sendEvent(canvas.view.viewport(), event)
                render(previous)
                elapsed = (perf_counter() - started) * 1000
                if index >= 2:
                    times.append(elapsed)
                maximum_items = max(maximum_items, len(canvas.node_items))
                maximum_visible = max(maximum_visible, len(canvas.view._selection_decoration.frame(
                    canvas.view.viewport().devicePixelRatioF())))
            frame_results[operation] = summary(times)
        assert maximum_visible <= 300, "交互帧指标要求固定屏内不超过 300 个节点"
        return {"节点数": count, "树形": shape, "宿主": host,
                "Qt平台": app.platformName(), "设备像素比": window.devicePixelRatioF(),
                "初始缩放": zoom,
                "平移步长逻辑像素": 20, "缩放步长": 1.15,
                "画布视口": [canvas.view.viewport().width(), canvas.view.viewport().height()],
                "启用虚拟化": canvas._virtualized,
                "全选前图元数": before_items, "全选后图元数": len(canvas.node_items),
                "交互最大图元数": maximum_items, "交互最大屏内外框数": maximum_visible,
                "全选命令": summary(command_times), "全选到首帧": summary(select_times),
                "交互帧": frame_results,
                "全选达标": statistics.median(select_times) <= 200,
                "交互达标": all(frame_results[key]["P95毫秒"] <= 33 for key in ("平移", "缩放"))}
    finally:
        window.dispose()
        window.hide()
        window.deleteLater()
        app.processEvents()


def run():
    """输出中文 JSON 原始样本及逐场景摘要；阈值未满足时返回非零退出码。"""
    parser = argparse.ArgumentParser(description="万节点全选与画布交互验收")
    parser.add_argument("--nodes", type=int, nargs="+", default=[1000, 3000, 10000], help="节点规模列表")
    parser.add_argument("--shapes", nargs="+", choices=("balanced", "wide"), default=["balanced", "wide"])
    parser.add_argument("--hosts", nargs="+", choices=("canvas", "workbench"), default=["canvas", "workbench"])
    parser.add_argument("--samples", type=int, default=5, help="预热一次后的全选采样轮数")
    parser.add_argument("--frames", type=int, default=30, help="各交互预热两帧后的采样帧数")
    parser.add_argument("--zoom", type=float, default=1, help="初始缩放，0.1～4；密集视口可使用 0.1")
    parser.add_argument("--platform", help="Qt 平台，例如 windows 或 offscreen")
    parser.add_argument("--output", type=Path, default=Path(".artifacts/performance/全选性能.json"))
    args = parser.parse_args()
    if min(args.nodes) < 30 or args.samples < 1 or args.frames < 1 or not 0.1 <= args.zoom <= 4:
        parser.error("节点数至少为 30，采样轮数和帧数至少为 1，缩放为 0.1～4")
    app = QApplication(["全选性能验收", "-platform", args.platform] if args.platform else [])
    app.setQuitOnLastWindowClosed(False)
    data = args.output.resolve().parent / f"全选验收数据-{time_ns()}"
    cases = []
    for count in args.nodes:
        for shape in args.shapes:
            for host in args.hosts:
                directory = data / f"{count}-{shape}-{host}"
                directory.mkdir(parents=True, exist_ok=True)
                case = measure_case(app, count, shape, host, args.samples, args.frames, directory, args.zoom)
                cases.append(case)
                print(f"{count} / {shape} / {host}: 全选首帧 {case['全选到首帧']['中位毫秒']} ms；"
                      f"平移 P95 {case['交互帧']['平移']['P95毫秒']} ms；"
                      f"缩放 P95 {case['交互帧']['缩放']['P95毫秒']} ms", flush=True)
    report = {"说明": "同机预热后真实 Ctrl+A 到完整视口帧；工作台同时显示大纲和属性栏。"
                       "使用隐藏原生窗口，测量 GUI 线程事件与绘制耗时，不含桌面合成及显示器呈现延迟。",
              "Python": platform.python_version(), "PySide6": PySide6.__version__,
              "系统": platform.platform(), "处理器": platform.processor(), "场景": cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(case["全选达标"] and case["交互达标"] for case in cases) else 1


if __name__ == "__main__":
    raise SystemExit(run())
