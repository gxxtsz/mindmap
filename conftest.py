"""默认以离屏 Qt 执行自动化测试；需要桌面测试时可显式设置 QT_QPA_PLATFORM。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def release_offscreen_clipboard(request):
    """在离屏 Qt 测试结束时释放剪贴板载荷，保证早于 QApplication 的销毁。

    PySide6 的离屏剪贴板可能在解释器退出时仍持有 Python 创建的 QMimeData，
    此时销毁顺序会导致原生访问冲突。仅对已经依赖 qapp 的测试清理，纯引擎测试
    不创建 Qt 应用；原生平台的系统剪贴板保持原有所有权和跨进程使用语义。
    """
    application = request.getfixturevalue("qapp") if "qapp" in request.fixturenames else None
    yield
    if application is not None and application.platformName() == "offscreen":
        application.clipboard().clear()
