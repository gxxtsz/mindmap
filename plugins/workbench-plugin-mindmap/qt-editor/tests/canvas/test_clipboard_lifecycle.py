"""验证画布剪贴板用例结束后，离屏 Qt 子进程能够正常退出。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

CANVAS_TESTS = Path(__file__).resolve().parent


@pytest.mark.parametrize("test_case", [
    "test_extensions.py::test_clipboard_uses_injected_codec_and_rejects_old_version",
    "test_image_clipboard.py::test_text_editor_pastes_image_after_committing_text_with_separate_undo[bitmap]",
    "test_image_clipboard.py::test_text_editor_pastes_image_after_committing_text_with_separate_undo[file]",
])
def test_clipboard_pytest_process_exits_normally(tmp_path, test_case):
    """用独立离屏剪贴板运行单个用例，并等待 pytest 完成 Qt 析构后检查退出码。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "child"), test_case],
        cwd=CANVAS_TESTS,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen"),
        capture_output=True, text=True, encoding="utf-8", timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
