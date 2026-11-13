"""通过正式启动器验证离屏桌面进程的剪贴板退出清理。"""

import os
import subprocess
import sys
import textwrap


def test_desktop_launcher_releases_offscreen_mime_before_qt_shutdown(tmp_path):
    """在独立进程运行正式事件循环，检查持有剪贴板载荷时的退出码。"""
    source = textwrap.dedent("""\
        import sys
        from mindmap_desktop_runtime.ui.shell import AppSpec, DesktopWindow, run_desktop
        from PySide6.QtCore import QMimeData, QTimer
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication

        def build(options):
            window = DesktopWindow(AppSpec("clipboard-exit", "退出验证", storage_dir=options.state_dir))
            mime = QMimeData()
            mime.setData("application/x-test", b"clipboard-payload")
            image = QImage(8, 8, QImage.Format.Format_ARGB32)
            image.fill(0x805599aa)
            mime.setImageData(image)
            QApplication.clipboard().setMimeData(mime)
            QTimer.singleShot(0, QApplication.instance().quit)
            return window

        raise SystemExit(run_desktop(build, ["--state-dir", sys.argv[1], "--no-restore"]))
        """)
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path / "desktop")],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen"),
        capture_output=True, text=True, encoding="utf-8", timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
