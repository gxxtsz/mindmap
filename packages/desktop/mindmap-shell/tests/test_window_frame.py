"""窗口外轮廓不占布局、不拦截输入；真实 Windows 验证系统阴影与工作区边界。"""

import ctypes
from ctypes import wintypes

import pytest
from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow
from PySide6.QtCore import QPoint, QRect, QSize, Qt
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
@pytest.mark.parametrize("compact", [False, True])
def test_outline_preserves_layout_rendering_and_input(frame_window, qtbot, theme, compact):
    """两种密度下顶栏、标签与按钮均完整可用，覆盖层仍透传真实输入。"""
    window = frame_window
    window.manager.set_theme(theme)
    window.manager.set_option("compact", compact)
    window.resize(1200, 800)
    window.show()
    QApplication.processEvents()
    title_height, status_height = (36, 24) if compact else (38, 28)
    body_height = 800 - status_height
    assert window.centralWidget().geometry() == QRect(0, 0, 1200, body_height)
    assert window.title_bar.geometry() == QRect(0, 0, 1200, title_height)
    assert window.host.geometry() == QRect(0, title_height, 1200, body_height - title_height)
    assert window.status.geometry() == QRect(0, body_height, 1200, status_height)
    assert window.host.canvas_layout.contentsMargins().top() == 2
    # 独立标签栏仍保持既有高度；窗控仅增加点击宽度，不挤压菜单或改变标题栏高度。
    group = window.host.editor.groups[0]
    assert group.tabBar().height() == (28 if compact else 32)
    bar = window.title_bar
    assert bar.menu.size() == QSize(32, 32)
    for control in (bar.pin, bar.minimize, bar.maximize, bar.close):
        assert control.size() == QSize(40, 32)
        assert control.iconSize() == QSize(16, 16)
        assert bar.rect().contains(control.geometry())
    assert bar.minimize.x() - bar.pin.geometry().right() - 1 == 2
    assert bar.maximize.x() - bar.minimize.geometry().right() - 1 == 2
    assert bar.close.x() - bar.maximize.geometry().right() - 1 == 2
    assert all(bar.rect().contains(button.geometry()) for button in bar.action_buttons)
    assert window._frame_outline.geometry() == window.rect()
    image = window.grab().toImage()
    if window._native_frame is not None and window._native_frame.native_border:
        # QWidget.grab 不包含 DWM 合成的外边框；系统属性在原生窗口用例中核验。
        assert window._frame_outline.isHidden()
    else:
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
    # 高 DPI 下工作区可能小于固定的 1120×760；验收窗口先放入屏内，避免系统
    # 主动约束越界位置与尺寸，被误判为最大化／还原改变了应用的正常窗口几何。
    available = window.screen().availableGeometry()
    original = QRect(available.topLeft() + QPoint(20, 20),
                     QSize(min(1120, available.width() - 40), min(760, available.height() - 40)))
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
    assert window._frame_outline.isVisible() == (not frame.native_border)
    window.showFullScreen()
    qtbot.waitUntil(window.isFullScreen)
    assert window._frame_outline.isHidden()
    window.showNormal()
    qtbot.waitUntil(lambda: window.geometry() == original)
    window.showMinimized()
    qtbot.waitUntil(window.isMinimized)
    window.showNormal()
    qtbot.waitUntil(lambda: window.geometry() == original)
    assert window._frame_outline.isVisible() == (not frame.native_border)


def test_native_border_follows_theme_and_window_state(frame_window, qtbot, monkeypatch):
    """系统圆角和描边由同一层绘制；切换主题、最大化及全屏后正确恢复。"""
    if QApplication.platformName() != "windows":
        pytest.skip("圆角描边需要真实 Windows 平台窗口")
    window = frame_window
    window.show()
    qtbot.waitUntil(lambda: window._native_frame is not None)
    frame = window._native_frame
    if not frame.native_border:
        pytest.skip("当前系统不支持 DWM 边框颜色")
    accepted = {}
    original = frame.dwm.DwmSetWindowAttribute

    def record(hwnd, key, address, size):
        # 边框颜色与圆角偏好是只写属性；记录真实系统调用成功接收的值，
        # 不使用 DwmGetWindowAttribute 读取不支持查询的属性。
        result = original(hwnd, key, address, size)
        if key in (33, 34) and result >= 0:
            accepted[key] = ctypes.cast(address, ctypes.POINTER(wintypes.DWORD)).contents.value
        return result

    monkeypatch.setattr(frame.dwm, "DwmSetWindowAttribute", record)

    def assert_decoration(color, corner):
        qtbot.waitUntil(lambda: accepted.get(34) == color and accepted.get(33) == corner)
        assert frame.native_border
        assert window._frame_outline.isHidden()

    for theme in ("light", "dark"):
        window.manager.set_theme(theme)
        color = window.manager.theme.color("main-window-outline")
        native_color = color.red() | (color.green() << 8) | (color.blue() << 16)
        assert_decoration(native_color, 2)
        window.showMaximized()
        assert_decoration(0xFFFFFFFE, 1)
        window.showNormal()
        assert_decoration(native_color, 2)
        window.showFullScreen()
        assert_decoration(0xFFFFFFFE, 1)
        window.showNormal()
        assert_decoration(native_color, 2)
