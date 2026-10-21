"""在真实子进程中检查退出码，覆盖测试清理及正式启动器的离屏剪贴板生命周期。"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_offscreen(arguments):
    """运行隔离 Qt 场景并保留真实退出码；不使用提前退出绕过 Qt 析构。

    载荷使用独立离屏剪贴板，不接触用户的系统剪贴板；Windows 子进程不弹出终端。
    标准输出与错误一并返回，断言失败时提供原生异常及 pytest 结果供定位。
    """
    return subprocess.run(
        [sys.executable, *arguments], cwd=ROOT,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen"),
        capture_output=True, text=True, encoding="utf-8", timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_clipboard_pytest_process_exits_normally(tmp_path):
    """原始崩溃场景不仅要通过断言，pytest 完成析构后也必须正常退出。"""
    result = run_offscreen([
        "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "child"),
        "packages/qt/mindmap-qt-canvas/tests/test_extensions.py::test_clipboard_uses_injected_codec_and_rejects_old_version",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_desktop_launcher_releases_offscreen_mime_before_qt_shutdown(tmp_path):
    """正式事件循环退出也要释放 Python MIME，不依赖 pytest 的测试清理。"""
    source = textwrap.dedent("""\
        import sys
        from mindmap_shell import AppSpec, WorkbenchWindow, run_desktop
        from PySide6.QtCore import QMimeData, QTimer
        from PySide6.QtWidgets import QApplication

        def build(options):
            window = WorkbenchWindow(AppSpec("clipboard-exit", "退出验证", storage_dir=options.state_dir))
            mime = QMimeData()
            mime.setData("application/x-test", b"clipboard-payload")
            QApplication.clipboard().setMimeData(mime)
            QTimer.singleShot(0, QApplication.instance().quit)
            return window

        raise SystemExit(run_desktop(build, ["--state-dir", sys.argv[1], "--no-restore"]))
        """)
    result = run_offscreen(["-c", source, str(tmp_path / "desktop")])
    assert result.returncode == 0, result.stdout + result.stderr
