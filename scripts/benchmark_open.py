"""测量真实工作台打开导图直至首帧绘制的耗时，不写入被测导图。

在仓库根目录执行：
uv run --locked --all-packages python scripts/benchmark_open.py 第二大脑.mindmap
可使用 --runs 重复测量，使用 --profile 保存性能分析数据，使用 --output 保存结果。
配置使用独立临时目录，计时包含文件解析、校验、布局、面板同步和视口实际绘制。
"""

import argparse
import cProfile
import json
import os
import platform
import shutil
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import PySide6  # noqa: E402
from mindmap_workbench.shell_adapter import build_window  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def run() -> None:
    """创建隔离工作台并重复打开同一只读来源；输出毫秒统计及实际实例化节点数。

    --profile 只分析第一轮，分析器自身开销计入该轮，比较优化前后时必须使用
    相同参数。每轮释放所有窗口、定时器及延迟删除对象，不复用上一轮的视觉缓存。
    """
    parser = argparse.ArgumentParser(description="大型导图打开性能基准")
    parser.add_argument("path", type=Path, help="待测的原生导图文件")
    parser.add_argument("--runs", type=int, default=1, help="重复打开次数，至少为 1")
    parser.add_argument("--profile", type=Path, help="第一轮的 cProfile 输出路径")
    parser.add_argument("--output", type=Path, help="中文 JSON 结果的保存路径")
    arguments = parser.parse_args()
    if arguments.runs < 1:
        parser.error("重复次数至少为 1")
    application = QApplication([])
    samples = []
    event_gaps = []
    for index in range(arguments.runs):
        with tempfile.TemporaryDirectory(prefix="mindmap-open-") as directory:
            source = Path(directory) / arguments.path.name
            # 测量应用层打开耗时；源文件先复制到隔离目录，任何会话写入都不会触碰原件。
            shutil.copyfile(arguments.path, source)
            window = build_window(storage_dir=Path(directory), restore=False)
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            window.resize(1280, 800)
            window.show()
            application.processEvents()
            profiler = cProfile.Profile() if arguments.profile and index == 0 else None
            if profiler:
                profiler.enable()
            started = perf_counter()
            ticks = [started]
            heartbeat = QTimer(window)
            heartbeat.setInterval(5)
            heartbeat.timeout.connect(lambda: ticks.append(perf_counter()))
            heartbeat.start()
            try:
                if not window.open_path(source):
                    raise RuntimeError("导图未能打开")
                application.processEvents()
                canvas = window.active_session.canvas
                canvas.view.viewport().grab()
                painted = perf_counter()
                elapsed = (painted - started) * 1000
                ticks.append(painted)
                event_gaps.append(round(max(after - before for before, after in zip(ticks, ticks[1:])) * 1000, 2))
                if profiler:
                    profiler.disable()
                    arguments.profile.parent.mkdir(parents=True, exist_ok=True)
                    profiler.dump_stats(str(arguments.profile))
                samples.append(round(elapsed, 2))
                node_count = window.active_session.engine.node_count
                item_count = len(canvas.node_items)
            finally:
                heartbeat.stop()
                window.dispose()
                window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                application.processEvents()
    result = {
        "文件": arguments.path.name,
        "节点数": node_count,
        "首帧节点图元数": item_count,
        "各轮打开毫秒": samples,
        "打开中位毫秒": round(statistics.median(samples), 2),
        "各轮主线程最长事件间隔毫秒": event_gaps,
        "包含分析器开销": bool(arguments.profile),
        "Python": platform.python_version(),
        "PySide6": PySide6.__version__,
        "Qt平台": application.platformName(),
        "系统": platform.platform(),
    }
    content = json.dumps(result, ensure_ascii=False, indent=2)
    print(content)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(content + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
