"""窗口外轮廓不占布局、不拦截输入；真实 Windows 验证系统阴影与工作区边界。"""

import ctypes
from ctypes import wintypes

import pytest
from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QLineEdit


@pytest.fixture
def frame_window(qtbot, tmp_path):
    """使用独立配置及真实输入控件，避免测试触及用户布局或文件。"""
    calls = []
    window = WorkbenchWindow(AppSpec(
        "window-frame-test", "窗口外观验收", storage_dir=tmp_path,
        actions=(ActionSpec("sample", "测试操作", lambda: calls.append(True), toolbar=True),),
    ))
    window.manager.add_document("editor", "输入验证", QLineEdit("原有内容"))
    window._test_calls = calls
    qtbot.addWidget(window)
    yield window
    window.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_outline_preserves_layout_rendering_and_input(frame_window, qtbot, theme):
    """覆盖层显示在四边，标题底色生效；内容坐标和真实按钮点击保持原行为。"""
    window = frame_window
    window.manager.set_theme(theme)
    window.resize(1200, 800)
    window.show()
    QApplication.processEvents()
    assert window.centralWidget().geometry() == QRect(0, 0, 1200, 772)
    assert window.title_bar.geometry() == QRect(0, 0, 1200, 52)
    assert window.host.geometry() == QRect(0, 52, 1200, 720)
    assert window.status.geometry() == QRect(0, 772, 1200, 28)
    assert window._frame_outline.geometry() == window.rect()
    image = window.grab().toImage()
    assert image.pixelColor(0, image.height() // 2) == window.manager.theme.color("main-window-outline")
    title = window.title_bar.grab().toImage()
    assert title.pixelColor(3, 3) == window.manager.theme.color("main-window-titlebar-bg")
    button = window.title_bar.action_buttons[0]
    position = button.mapTo(window, button.rect().center())
    assert window.childAt(position) is button
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert window._test_calls == [True]
    editor = window.manager.documents["editor"]["widget"]
    editor.setFocus()
    qtbot.keyClicks(editor, "123")
    assert editor.text().endswith("123")


@pytest.mark.parametrize("initial_maximized", [False, True])
def test_windows_shadow_preserves_geometry_and_maximized_work_area(frame_window, qtbot, initial_maximized):
    """覆盖初次最大化、还原、全屏与最小化，防止原生样式造成窗口偏移或裁切。"""
    if QApplication.platformName() != "windows":
        pytest.skip("系统阴影需要真实 Windows 平台窗口")
    window = frame_window
    original = QRect(100, 130, 1120, 760)
    window.setGeometry(original)
    if initial_maximized:
        window.showMaximized()
    else:
        window.show()
    qtbot.waitUntil(lambda: window._native_frame is not None)
    frame = window._native_frame
    assert frame is not None
    assert frame.get_style(frame.hwnd, -16) & 0x00040000  # 系统缩放样式仍然存在。
    frame.dwm.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    frame.dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    enabled = wintypes.BOOL()
    assert frame.dwm.DwmGetWindowAttribute(frame.hwnd, 1, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0
    assert enabled.value
    if not initial_maximized:
        assert window.geometry() == original
        window.showMaximized()
    qtbot.waitUntil(lambda: window.geometry() == window.screen().availableGeometry())
    assert window._frame_outline.isHidden()
    bottom = window.status.mapToGlobal(QPoint(0, window.status.height() - 1))
    assert window.screen().availableGeometry().contains(bottom)
    window.showNormal()
    qtbot.waitUntil(lambda: window.geometry() == original)
    assert window._frame_outline.isVisible()
    window.showFullScreen()
    qtbot.waitUntil(window.isFullScreen)
    assert window._frame_outline.isHidden()
    window.showNormal()
    qtbot.waitUntil(lambda: window.geometry() == original)
    window.showMinimized()
    qtbot.waitUntil(window.isMinimized)
    window.showNormal()
    qtbot.waitUntil(lambda: window.geometry() == original)
    assert window._frame_outline.isVisible()
