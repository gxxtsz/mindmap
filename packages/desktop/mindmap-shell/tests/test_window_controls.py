"""窗口按钮回归：核验实际像素与原生消息，不把 QSS 文本当成交互结果。"""

import ctypes
from ctypes import wintypes

import pytest
from mindmap_shell import AppSpec, WorkbenchWindow
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QCursor, QEnterEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget


@pytest.fixture
def control_window(qtbot, tmp_path):
    """创建独立配置的按钮验收窗口；真实桌面时将四个按钮完整放入可用屏幕。"""
    window = WorkbenchWindow(AppSpec("window-controls-test", "窗口按钮验收", storage_dir=tmp_path))
    window.manager.add_document("editor", "测试文档", QLineEdit("编辑区域"))
    available = QApplication.primaryScreen().availableGeometry()
    window.resize(min(1000, available.width() - 40), min(650, available.height() - 40))
    window.move(available.topLeft() + QPoint(20, 20))
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    QApplication.processEvents()
    yield window
    window.close()


def button_background(button):
    """从完整窗口采样图标左侧的背景，按设备像素比定位，兼容 150% 等分数缩放。"""
    window = button.window()
    image = window.grab().toImage()
    point = button.mapTo(window, QPoint(5, button.height() // 2))
    ratio = image.devicePixelRatio()
    return image.pixelColor(int((point.x() + 0.5) * ratio), int((point.y() + 0.5) * ratio))


def send_qt_hover(button, hovered):
    """直接投递 Qt 进入／离开事件，隔离平台光标缓存；真实系统移动另由原生用例核验。"""
    if hovered:
        point = button.rect().center()
        event = QEnterEvent(QPointF(point), QPointF(button.mapTo(button.window(), point)),
                            QPointF(button.mapToGlobal(point)))
    else:
        event = QEvent(QEvent.Type.Leave)
    QApplication.sendEvent(button, event)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("role", ["pin", "minimize", "maximize", "close"])
def test_qt_window_button_feedback(control_window, qtbot, theme, compact, role):
    """四类按钮的默认、悬浮、按下与移出颜色可区分，交互过程不改变几何尺寸。"""
    window = control_window
    window.manager.set_theme(theme)
    window.manager.set_option("compact", compact)
    QApplication.processEvents()
    button = getattr(window.title_bar, role)
    send_qt_hover(button, False)
    original = button.geometry()
    background = window.manager.theme.color("main-window-titlebar-bg")
    hovered = QColor("#C42B1C") if role == "close" else window.manager.theme.color("window-control-bg-hovered")
    pressed = QColor("#A82318") if role == "close" else window.manager.theme.color("window-control-bg-pressed")
    assert len({background.name(), hovered.name(), pressed.name()}) == 3
    assert button_background(button) == background

    send_qt_hover(button, True)
    assert button_background(button) == hovered
    qtbot.mousePress(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
    assert button_background(button) == pressed
    # 按钮外松开不会关闭或最小化验收窗口，也不会意外激活最大化。
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-5, -5))
    send_qt_hover(button, False)
    assert button_background(button) == background
    assert button.geometry() == original
    assert not window.isMaximized() and not window.isMinimized()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_pin_button_toggles_actual_flag_and_selected_feedback(control_window, qtbot, theme, compact):
    """点击切换实际窗口层级；选中、悬浮及按下均有像素反馈，刷新主题不重复发信号。"""
    window = control_window
    window.manager.set_theme(theme)
    window.manager.set_option("compact", compact)
    button = window.title_bar.pin
    original = window.geometry()
    changes = []
    window.always_on_top_changed.connect(changes.append)
    assert not window.is_always_on_top() and not button.isChecked()
    assert button.toolTip() == button.accessibleName() == "置顶窗口"

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert window.is_always_on_top() and button.isChecked()
    assert button.toolTip() == button.accessibleName() == "取消置顶"
    assert window.isVisible() and window.geometry() == original and changes == [True]
    for current_theme in ("dark" if theme == "light" else "light", theme):
        window.manager.set_theme(current_theme)
        send_qt_hover(button, False)
        assert button_background(button) == window.manager.theme.color("toolbar-selected-bg-active")
        # QIcon 非空不代表 SVG 真正画出像素；选中态使用的颜色也必须能被 SVG 解析。
        icon_image = button.icon().pixmap(button.iconSize()).toImage()
        assert any(icon_image.pixelColor(x, y).alpha()
                   for y in range(icon_image.height()) for x in range(icon_image.width()))
        send_qt_hover(button, True)
        assert button_background(button) == window.manager.theme.color("window-control-bg-hovered")
        qtbot.mousePress(button, Qt.MouseButton.LeftButton)
        assert button_background(button) == window.manager.theme.color("window-control-bg-pressed")
        qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-5, -5))
        assert button.isChecked() and changes == [True]

    window.set_always_on_top(True)
    assert changes == [True]
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    send_qt_hover(button, False)
    assert not window.is_always_on_top() and not button.isChecked()
    assert button.toolTip() == button.accessibleName() == "置顶窗口"
    assert button_background(button) == window.manager.theme.color("main-window-titlebar-bg")
    assert window.isVisible() and window.geometry() == original and changes == [True, False]


@pytest.mark.parametrize("native_title_bar", [False, True])
def test_pin_api_preserves_hidden_window_and_other_flags(qtbot, tmp_path, native_title_bar):
    """首次显示前应用偏好始终保持隐藏，原生标题栏模式同样支持公开置顶接口。"""
    window = WorkbenchWindow(AppSpec("hidden-pin-test", "置顶初始化验收", storage_dir=tmp_path,
                                    native_title_bar=native_title_bar))
    qtbot.addWidget(window)
    geometry, flags = window.geometry(), window.windowFlags()
    changes = []
    window.always_on_top_changed.connect(changes.append)
    for enabled in (True, True, False, False):
        window.set_always_on_top(enabled)
        assert window.is_always_on_top() == window.title_bar.pin.isChecked() == enabled
        assert window.isHidden() and window.geometry() == geometry
        assert window.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint == flags
    assert changes == [True, False]
    window.close()


def test_pin_api_keeps_editor_focus_and_native_handle(control_window, qtbot):
    """直接设置置顶不隐藏或重建窗口，正在编辑的输入控件继续持有焦点。"""
    window = control_window
    editor = window.manager.documents["editor"]["widget"]
    editor.setFocus()
    qtbot.waitUntil(editor.hasFocus)
    hwnd = window.winId()
    for enabled in (True, False):
        window.set_always_on_top(enabled)
        QApplication.processEvents()
        assert editor.hasFocus() and window.winId() == hwnd
        assert bool(window.windowHandle().flags() & Qt.WindowType.WindowStaysOnTopHint) == enabled


@pytest.mark.parametrize("maximized", [False, True])
def test_pin_preserves_previously_created_hidden_window(control_window, qtbot, maximized):
    """已有原生句柄的窗口隐藏后切换置顶，系统也保持隐藏，重新显示和还原位置正确。"""
    window = control_window
    original = window.geometry()
    if maximized:
        window.showMaximized()
        QApplication.processEvents()
    window.hide()
    for enabled in (True, False, True):
        window.set_always_on_top(enabled)
        QApplication.processEvents()
        assert window.isHidden() and not window.windowHandle().isVisible()
        if QApplication.platformName() == "windows":
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            assert not user32.IsWindowVisible(int(window.winId()))
    window.show()
    QApplication.processEvents()
    assert window.isMaximized() == maximized
    window.showNormal()
    QApplication.processEvents()
    assert window.geometry() == original and window.is_always_on_top()


@pytest.mark.parametrize("state", ["normal", "maximized", "minimized", "minimized_maximized", "fullscreen"])
def test_pin_preserves_window_state_and_normal_geometry(control_window, qtbot, state):
    """置顶切换保留当前状态及还原位置；最小化恢复后仍保持置顶。"""
    window = control_window
    original = window.geometry()
    if state == "minimized_maximized":
        window.showMaximized()
    {"normal": window.showNormal, "maximized": window.showMaximized,
     "minimized": window.showMinimized, "minimized_maximized": window.showMinimized,
     "fullscreen": window.showFullScreen}[state]()
    QApplication.processEvents()
    previous_state = window.windowState()
    for enabled in (True, False, True):
        window.set_always_on_top(enabled)
        QApplication.processEvents()
        assert window.isVisible() and window.windowState() == previous_state
        assert window.normalGeometry() == original
        if state == "maximized":
            assert window.geometry() == window.screen().availableGeometry()
    window.showNormal()
    QApplication.processEvents()
    assert window.geometry() == original and window.is_always_on_top()
    window.showMinimized()
    window.showNormal()
    QApplication.processEvents()
    assert window.geometry() == original and window.is_always_on_top()


def test_native_pin_updates_system_topmost_and_reenables_frame(native_control_window, qtbot):
    """读取真实 HWND 的扩展样式，确保系统置顶生效且自绘边框仍支持缩放与贴靠。"""
    window = native_control_window
    for enabled in (True, False):
        qtbot.mouseClick(window.title_bar.pin, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: window._native_frame is not None)
        frame = window._native_frame
        assert bool(frame.get_style(frame.hwnd, -20) & 0x00000008) == enabled  # WS_EX_TOPMOST
        assert frame.get_style(frame.hwnd, -16) & 0x00040000  # WS_THICKFRAME
        move_native_cursor(window.title_bar.maximize)
        assert send_native_mouse(window, 0x0084) == 9  # HTMAXBUTTON


@pytest.mark.parametrize("initial_theme", ["light", "dark"])
def test_theme_switch_preserves_hover_and_press(control_window, qtbot, initial_theme):
    """光标和按键保持不动时切换主题，背景随主题更新而交互状态不丢失。"""
    window = control_window
    window.manager.set_theme(initial_theme)
    button = window.title_bar.maximize
    send_qt_hover(button, True)
    other = "dark" if initial_theme == "light" else "light"
    window.manager.set_theme(other)
    assert button_background(button) == window.manager.theme.color("window-control-bg-hovered")
    qtbot.mousePress(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
    window.manager.set_theme(initial_theme)
    assert button.isDown()
    assert button_background(button) == window.manager.theme.color("window-control-bg-pressed")
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-5, -5))


@pytest.fixture
def native_control_window(control_window, qtbot):
    """原生消息用例要求真实 HWND，默认离屏运行时明确跳过系统专属路径。"""
    if QApplication.platformName() != "windows":
        pytest.skip("最大化非客户区消息需要真实 Windows 平台窗口")
    qtbot.waitUntil(control_window.isActiveWindow)
    return control_window


def send_native_mouse(window, message, hit=0):
    """向实际 HWND 发送鼠标消息，经 Qt 的 nativeEvent 入口核验完整桥接。

    坐标取自 Win32 的物理屏幕位置；非客户区消息使用屏幕坐标，客户区消息
    先做 ScreenToClient。两个分量按有符号 16 位打包，保留负坐标副屏语义。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ScreenToClient.restype = wintypes.BOOL
    point = wintypes.POINT()
    assert user32.GetCursorPos(ctypes.byref(point))
    hwnd = int(window.winId())
    if message in (0x0200, 0x0201, 0x0202):
        assert user32.ScreenToClient(hwnd, ctypes.byref(point))
    position = (point.x & 0xFFFF) | ((point.y & 0xFFFF) << 16)
    return user32.SendMessageW(hwnd, message, hit, position)


def move_native_cursor(widget, point=None):
    """使用 Qt 逻辑坐标定位实际系统光标，使原生命中和高 DPI 坐标转换都参与验收。"""
    # 最大化后的布局请求可能尚未处理；先落定按钮位置，再计算目标屏幕坐标。
    QApplication.processEvents()
    QCursor.setPos(widget.mapToGlobal(point if point is not None else widget.rect().center()))
    QApplication.processEvents()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("role", ["minimize", "close"])
def test_native_standard_window_button_feedback(native_control_window, qtbot, theme, role):
    """常规窗口按钮也使用真实系统光标和客户区按压，覆盖高 DPI 下的悬浮与取消。"""
    window = native_control_window
    window.manager.set_theme(theme)
    button = getattr(window.title_bar, role)
    hover = QColor("#C42B1C") if role == "close" else window.manager.theme.color("window-control-bg-hovered")
    pressed = QColor("#A82318") if role == "close" else window.manager.theme.color("window-control-bg-pressed")
    move_native_cursor(button)
    qtbot.waitUntil(lambda: button_background(button) == hover)
    qtbot.mousePress(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
    assert button.isDown() and button_background(button) == pressed
    move_native_cursor(window, QPoint(100, 120))
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-5, -5))
    move_native_cursor(window, QPoint(120, 140))
    qtbot.waitUntil(lambda: not button.isDown())
    qtbot.waitUntil(lambda: button_background(button) == window.manager.theme.color("main-window-titlebar-bg"))
    assert window.isVisible() and not window.isMinimized()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_native_maximize_hover_press_drag_and_activation(native_control_window, qtbot, theme, compact):
    """覆盖原生悬浮、移出取消、移回继续、孤立松开及最大化／还原各触发一次。"""
    window = native_control_window
    window.manager.set_theme(theme)
    window.manager.set_option("compact", compact)
    button = window.title_bar.maximize
    clicks = []
    button.clicked.connect(lambda: clicks.append(window.isMaximized()))
    normal = window.manager.theme.color("main-window-titlebar-bg")
    hover = window.manager.theme.color("window-control-bg-hovered")
    pressed = window.manager.theme.color("window-control-bg-pressed")
    move_native_cursor(button)
    assert send_native_mouse(window, 0x0084) == 9  # HTMAXBUTTON，保留系统贴靠入口。
    send_native_mouse(window, 0x00A0, 9)
    assert button_background(button) == hover

    # 直接移出整窗仍应收到系统离开通知，不依赖 Qt 的普通 Leave 事件。
    move_native_cursor(window, QPoint(-5, 50))
    qtbot.waitUntil(lambda: button_background(button) == normal)
    move_native_cursor(button)
    send_native_mouse(window, 0x00A0, 9)
    send_native_mouse(window, 0x00A1, 9)
    assert QWidget.mouseGrabber() is button
    assert button.isDown() and button_background(button) == pressed

    move_native_cursor(window, QPoint(100, 120))
    send_native_mouse(window, 0x0200, 1)
    assert not button.isDown() and button_background(button) == normal, (
        QCursor.pos(), button.mapFromGlobal(QCursor.pos()), button.geometry(),
        button._native_pressed, button._hovered, window.isActiveWindow(),
    )
    move_native_cursor(button)
    send_native_mouse(window, 0x0200, 1)
    assert button.isDown() and button_background(button) == pressed
    move_native_cursor(window, QPoint(-5, 50))
    send_native_mouse(window, 0x0200, 1)
    send_native_mouse(window, 0x0202)
    assert not button.isDown() and QWidget.mouseGrabber() is None
    assert not window.isMaximized() and not clicks

    # 没有起始按下的非客户区松开不能激活按钮；一次完整按压只触发一次 QAction 回调。
    move_native_cursor(button)
    send_native_mouse(window, 0x00A2, 9)
    assert not window.isMaximized() and not clicks
    send_native_mouse(window, 0x00A1, 9)
    send_native_mouse(window, 0x0202)
    qtbot.waitUntil(window.isMaximized)
    assert len(clicks) == 1 and not button.isDown()
    assert button.toolTip() == "还原窗口"
    move_native_cursor(button)
    send_native_mouse(window, 0x00A1, 9)
    assert button.isDown()
    send_native_mouse(window, 0x00A2, 9)
    qtbot.waitUntil(lambda: not window.isMaximized())
    assert len(clicks) == 2 and not button.isDown()
    assert button.toolTip() == "最大化窗口"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("cancel", ["cancel_mode", "capture", "deactivate", "disable", "hide"])
def test_native_press_cancels_without_stale_feedback(native_control_window, qtbot, theme, cancel):
    """取消、抓取被接管、失焦、禁用与隐藏都释放按压，后续松开不能误触发。"""
    window = native_control_window
    window.manager.set_theme(theme)
    button = window.title_bar.maximize
    clicks = []
    button.clicked.connect(lambda: clicks.append(True))
    move_native_cursor(button)
    send_native_mouse(window, 0x00A1, 9)
    assert button.isDown()
    if cancel == "cancel_mode":
        send_native_mouse(window, 0x001F)
    elif cancel == "capture":
        editor = window.manager.documents["editor"]["widget"]
        editor.grabMouse()
        editor.releaseMouse()
    elif cancel == "deactivate":
        peer = QLineEdit("焦点切换验收")
        qtbot.addWidget(peer)
        peer.show()
        peer.activateWindow()
        move_native_cursor(peer)
        qtbot.waitUntil(lambda: not window.isActiveWindow())
    elif cancel == "disable":
        button.setEnabled(False)
    else:
        button.hide()
    assert not button.isDown() and not button._native_pressed
    assert QWidget.mouseGrabber() is not button
    # 取消后光标若仍位于按钮上，系统可以重新建立正常悬浮；移出后不得残留底色。
    move_native_cursor(window, QPoint(150, 160))
    qtbot.waitUntil(lambda: not button._hovered)
    send_native_mouse(window, 0x00A2, 9)
    assert not clicks and not window.isMaximized()
