"""完整工作台的大图交互基准；隐藏窗口使用生成导图及独立配置，不操作用户文件。

在仓库根目录执行：
uv run --locked --all-packages python scripts/benchmark_interactions.py --nodes 10000
同步命令与后续事件处理、视口绘制分别计时，500 毫秒自动保存单独测量。
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from pathlib import Path

import PySide6
from benchmark import build_document
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtWidgets import QApplication


def run():
    """执行局部折叠、单选、拖动及保存，输出实际命令耗时和有效性检查结果。"""
    parser = argparse.ArgumentParser(description="思维导图大图交互基准")
    parser.add_argument("--nodes", type=int, default=10000, help="生成节点数，至少 30 个")
    parser.add_argument("--samples", type=int, default=3, help="折叠与拖动查询的采样轮数")
    parser.add_argument("--output", type=Path, help="中文 JSON 报告路径，默认写入 .artifacts/performance")
    parser.add_argument("--platform", help="可选 Qt 平台，例如 windows 或 offscreen")
    parser.add_argument("--shape", choices=("balanced", "wide"), default="balanced", help="五叉树或宽同级树")
    parser.add_argument("--summary-only", action="store_true", help="终端仅输出汇总，完整逐次结果仍写入文件")
    args = parser.parse_args()
    if args.nodes < 30 or args.samples < 1:
        parser.error("节点数至少为 30，采样轮数至少为 1")
    output = args.output or Path(".artifacts/performance") / f"优化后交互-{args.nodes}.json"
    data = output.resolve().parent / f"生成验收数据-{time.time_ns()}"
    data.mkdir(parents=True, exist_ok=True)
    app = QApplication(["性能验收", "-platform", args.platform] if args.platform else [])
    app.setQuitOnLastWindowClosed(False)
    document = build_document(args.nodes)
    keys = list(document.nodes)
    if args.shape == "wide":
        for node in document.nodes.values():
            node.children = []
        document.root.children = keys[1:-5]
        document.nodes[keys[1]].children = keys[-5:]
        document.validate()
    window = build_window(storage_dir=data / "配置", restore=False)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1440, 900)
    assert window.new_document(data / "生成导图.mindmap")
    session = window.active_session
    canvas, engine = session.canvas, session.engine
    results = []

    def measure(name, action, *, render=True):
        """计时器只读取已提交状态，防止防抖保存混入当前命令；绘制单独计时。"""
        started = time.perf_counter()
        value = action()
        elapsed = (time.perf_counter() - started) * 1000
        session.timer.stop()
        row = {"阶段": name, "命令毫秒": round(elapsed, 3)}
        if render:
            started = time.perf_counter()
            app.processEvents()
            canvas.view.viewport().grab()
            row["后续事件与绘制毫秒"] = round((time.perf_counter() - started) * 1000, 3)
            session.timer.stop()
        results.append(row)
        return value

    try:
        measure("装载生成导图", lambda: engine.replace_document(document), render=False)
        window.show()
        app.processEvents()
        session.timer.stop()
        branch = next(
            key for key in reversed(keys)
            if len(document.nodes[key].children) == 5 and len(document.descendants(key)) == 6
        )
        canvas.select_nodes([branch])
        canvas.view.centerOn(QPointF(*canvas.layout_result.nodes[branch].center))
        for _ in range(args.samples):
            measure("收起五个叶子", lambda: engine.set_expanded(branch, False))
            measure("展开五个叶子", lambda: engine.set_expanded(branch, True))
        for _ in range(args.samples):
            assert measure("撤销局部展开", engine.undo)
            assert measure("重做局部展开", engine.redo)
        measure("收起根节点", lambda: engine.set_expanded(engine.root_id, False))
        assert len(canvas.node_items) == 1
        measure("展开根节点", lambda: engine.set_expanded(engine.root_id, True))
        assert len(canvas.layout_result.nodes) == args.nodes
        source, target = keys[-1], keys[-3]
        measure("单选叶子", lambda: canvas.select_nodes([source]))
        a, b = canvas.layout_result.nodes[source], canvas.layout_result.nodes[target]
        view, control = canvas.view, canvas.view.node_drag
        view.centerOn(QPointF((a.center[0] + b.center[0]) / 2, (a.center[1] + b.center[1]) / 2))
        origin = QPointF(*a.center)
        end = view.mapFromScene(QPointF(*b.center))
        assert view.viewport().rect().contains(end)
        revision = engine.revision
        assert measure("开始拖动", lambda: control.begin(origin), render=False)
        for index in range(args.samples * 3):
            measure("正文命中", lambda offset=index % 2: control.update(end + QPoint(offset, 0)), render=False)
            assert control.target is not None and control.target.position == "inside"
        sorting = view.mapFromScene(QPointF(b.center[0], b.bottom + 12))
        for index in range(args.samples * 3):
            measure("排序命中", lambda offset=index % 2: control.update(sorting + QPoint(offset, 0)), render=False)
            assert control.target is not None and control.target.position != "inside"
        blank = QPoint(view.viewport().width() - 20, view.viewport().height() // 2)
        for index in range(args.samples * 3):
            measure("空白处查询", lambda offset=index % 2: control.update(blank + QPoint(offset, 0)), render=False)
        assert engine.revision == revision
        drop = measure("松手结束手势", lambda: control.finish(end), render=False)
        assert drop is not None
        assert measure("松手试算及正式提交", lambda: control.commit(drop), render=False)
        animation_skipped = control._reflow is None
        control._settle_reflow()
        assert source in engine.get_node(target).children
        save_started = time.perf_counter()
        assert measure("定时自动保存派发", lambda: session.autosave(background=True), render=False)
        background = session._background_save is not None
        intervals = []
        if background:
            loop, pulse = QEventLoop(), QTimer()
            pulse.setInterval(5)
            previous = time.perf_counter()

            def tick():
                nonlocal previous
                now = time.perf_counter()
                intervals.append((now - previous) * 1000)
                previous = now
                if session._background_save is None or now - save_started > 20:
                    loop.quit()

            pulse.timeout.connect(tick)
            pulse.start()
            loop.exec()
            pulse.stop()
        assert session._background_save is None and not engine.is_dirty, session.last_error
        save_ms = (time.perf_counter() - save_started) * 1000
        stages = {}
        for name in dict.fromkeys(row["阶段"] for row in results):
            values = [row["命令毫秒"] for row in results if row["阶段"] == name]
            stages[name] = {
                "样本数": len(values), "中位毫秒": round(statistics.median(values), 3),
                "P95毫秒": sorted(values)[(len(values) * 95 + 99) // 100 - 1],
                "最大毫秒": max(values),
            }
            rows = [row for row in results if row["阶段"] == name]
            if all("后续事件与绘制毫秒" in row for row in rows):
                complete = sorted(row["命令毫秒"] + row["后续事件与绘制毫秒"] for row in rows)
                stages[name]["含绘制P95毫秒"] = round(complete[(len(complete) * 95 + 99) // 100 - 1], 3)
        report = {
            "说明": "真实工作台隐藏窗口，不同中文标题，默认圆角连接线，无图片；命令与绘制分别计时。",
            "树形": "五叉树" if args.shape == "balanced" else "宽同级树",
            "节点数": args.nodes, "Python": platform.python_version(), "PySide6": PySide6.__version__,
            "Qt平台": app.platformName(), "设备像素比": window.devicePixelRatioF(),
            "大图跳过重排动画": animation_skipped,
            "视口节点图元数": len(canvas.node_items), "视口连线图元数": len(canvas.branch_items),
            "自动保存使用后台任务": background,
            "保存派发到完成毫秒": round(save_ms, 3),
            "保存期间事件循环最大间隔毫秒": round(max(intervals), 3) if intervals else None,
            "拖动期间未提交文档": True, "落点与最终结构已验证": True,
            "阶段汇总": stages, "逐次结果": results,
        }
        text = json.dumps(report, ensure_ascii=False, indent=2)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        if args.summary_only:
            print(json.dumps({key: value for key, value in report.items() if key != "逐次结果"},
                             ensure_ascii=False, indent=2))
        else:
            print(text)
    finally:
        window.dispose()
        window.hide()
        window.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    run()
