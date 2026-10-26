"""验证闲置后显示恢复请求能触发真实绘制，并保留窗口和编辑状态。"""

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest
from mindmap_shell import AppSpec, WorkbenchWindow
from mindmap_shell._windows_frame import _MonitorInfo, _WindowsFrame
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget


class PaintProbe(QWidget):
    """使用不透明子控件计数实际绘制，避免 grab 隐式重绘掩盖恢复逻辑缺失。"""

    def __init__(self):
        super().__init__()
        self.paints = 0
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.editor = QLineEdit("尚未提交的草稿", self)
        self.editor.setGeometry(20, 20, 240, 32)

    def paintEvent(self, event):
        """固定颜色便于验证整窗重绘后依旧能够产出内容。"""
        self.paints += 1
        with QPainter(self) as painter:
            painter.fillRect(self.rect(), QColor("#72b394"))


@pytest.fixture
def recovery_window(qtbot, tmp_path):
    """全部使用独立配置，测试恢复时不读写用户导图。"""
    window = WorkbenchWindow(AppSpec("display-recovery-test", "显示恢复验收", storage_dir=tmp_path))
    probe = PaintProbe()
    window.manager.add_document("editor", "测试文档", probe)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: probe.paints > 0)
    # 初始显示可能安排额外恢复，落定后再计数，防止错误归因于启动绘制。
    qtbot.waitUntil(lambda: not window._display_recovery_timer.isActive())
    QCoreApplication.sendPostedEvents(None, QEvent.Type.UpdateRequest)
    window._test_probe = probe
    yield window
    window.close()


def test_recovery_repaints_opaque_content_and_keeps_editor_state(recovery_window, qtbot):
    """多次通知合并后仍绘制真实子控件，草稿、光标、焦点、句柄和窗口尺寸不变。"""
    window = recovery_window
    probe = window._test_probe
    probe.editor.setFocus()
    probe.editor.setSelection(2, 4)
    qtbot.waitUntil(probe.editor.hasFocus)
    original = (window.geometry(), window.windowState(), window.winId(), probe.editor.text(),
                probe.editor.cursorPosition(), probe.editor.selectedText())
    # 模拟绘图缓存内容失效。直接写入 Qt 自身的像素缓冲，不触发 QWidget 绘制；
    # 之后须由恢复请求引发 paintEvent，而不能借助窗口截图间接重画。
    store = window.backingStore()
    with QPainter(store.paintDevice()) as painter:
        painter.fillRect(0, 0, store.size().width(), store.size().height(), Qt.GlobalColor.black)
    paints = probe.paints
    for _ in range(3):
        window._request_display_recovery("显示设备变化")
    qtbot.waitUntil(lambda: probe.paints > paints)
    assert not window._display_recovery_timer.isActive()
    assert probe.editor.hasFocus()
    assert (window.geometry(), window.windowState(), window.winId(), probe.editor.text(),
            probe.editor.cursorPosition(), probe.editor.selectedText()) == original
    assert probe.grab().toImage().pixelColor(5, 60) == QColor("#72b394")


@pytest.mark.parametrize("state", ["hidden", "minimized", "closing"])
def test_recovery_does_not_show_or_repaint_inactive_window(recovery_window, qtbot, state):
    """系统通知及已排队请求都不能使隐藏窗口抢先显示或干扰关闭流程。"""
    window = recovery_window
    window._request_display_recovery("桌面合成器变化")
    if state == "hidden":
        window.hide()
    elif state == "minimized":
        window.showMinimized()
    else:
        window.manager.shutdown()
    QApplication.processEvents()
    paints = window._test_probe.paints
    window._request_display_recovery("系统从节能状态恢复")
    assert not window._display_recovery_timer.isActive()
    QApplication.processEvents()
    assert window._test_probe.paints == paints
    if state == "hidden":
        assert window.isHidden()
    elif state == "minimized":
        assert window.isMinimized()


class PaintEvents(QObject):
    """观察现有输入控件的绘图事件；不改变控件本身或调用截图。"""

    def __init__(self, widget):
        super().__init__(widget)
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


@pytest.mark.parametrize("message,wparam", [(0x031E, 0), (0x007E, 32), (0x0218, 6),
                                          (0x0218, 7), (0x0218, 18)])
def test_native_display_messages_schedule_real_repaint(recovery_window, qtbot, message, wparam):
    """真实 HWND 收到合成、显示及恢复通知后，输入控件必须实际收到重绘事件。"""
    if QApplication.platformName() != "windows":
        pytest.skip("原生显示通知需要真实 Windows 窗口")
    window = recovery_window
    counter = PaintEvents(window._test_probe.editor)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    # SendMessage 只影响这个隔离测试窗口，不广播，不改变系统显示或电源设置。
    user32.SendMessageW(int(window.winId()), message, wparam, 0)
    qtbot.waitUntil(lambda: counter.count > 0)
    assert window.isVisible() and not window.isMinimized()


def test_unrelated_native_notification_does_not_request_recovery(recovery_window, monkeypatch):
    """挂起通知并不是恢复，普通原生消息也不应变成持续重绘来源。"""
    if QApplication.platformName() != "windows":
        pytest.skip("原生电源通知需要真实 Windows 窗口")
    window = recovery_window
    requests = []
    monkeypatch.setattr(window, "_request_display_recovery", requests.append)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW(int(window.winId()), 0x0218, 4, 0)  # PBT_APMSUSPEND
    assert not window._display_recovery_timer.isActive()
    assert requests == []


def test_failed_frame_extension_keeps_fallback_outline_available(caplog):
    """系统接受边框颜色但拒绝扩展时，不能隐藏唯一可用的自绘轮廓，并保留错误码。"""
    frame = object.__new__(_WindowsFrame)
    frame.hwnd = 1
    frame.native_border = False
    # 仅模拟合成接口的失败返回值，不将测试句柄传入真实 Windows 系统库。
    frame.dwm = SimpleNamespace(
        DwmSetWindowAttribute=lambda *args: 0,
        DwmExtendFrameIntoClientArea=lambda *args: -2147467259,
    )
    frame.refresh(maximized=False, fullscreen=False, dark=False, border_color=0x123456)
    assert not frame.native_border
    assert "恢复系统边框扩展失败" in caplog.text and "0x80004005" in caplog.text


@pytest.mark.parametrize("proposed,expected", [
    ((-10, -10, 3850, 2110), (0, 0, 3840, 2088)),
    ((-3850, -10, 10, 2110), (-3840, 0, 0, 2088)),
])
def test_maximized_client_uses_destination_monitor_after_minimize(proposed, expected):
    """模拟恢复时HWND仍指向左屏的旧位置，裁剪必须跟随消息中的目标矩形。

    两块屏幕在物理坐标中左右相邻，左屏使用负坐标。旧实现查询HWND所在屏幕，
    恢复到右屏时会把客户区右边界裁为0；同一用例也验证左屏仍能正确保留负坐标。
    """
    frame = object.__new__(_WindowsFrame)
    frame.hwnd = 1
    monitors = {1: (-3840, 0, 0, 2088), 2: (0, 0, 3840, 2088)}

    def monitor_from_rect(address, flags):
        rect = ctypes.cast(address, ctypes.POINTER(wintypes.RECT)).contents
        return 1 if rect.left + rect.right < 0 else 2

    def get_monitor_info(monitor, address):
        info = ctypes.cast(address, ctypes.POINTER(_MonitorInfo)).contents
        info.work = wintypes.RECT(*monitors[monitor])
        return True

    frame.user32 = SimpleNamespace(
        IsZoomed=lambda hwnd: True,
        MonitorFromWindow=lambda hwnd, flags: 1,
        MonitorFromRect=monitor_from_rect,
        GetMonitorInfoW=get_monitor_info,
    )
    client = wintypes.RECT(*proposed)
    frame.constrain_maximized_client(ctypes.addressof(client))
    assert (client.left, client.top, client.right, client.bottom) == expected
    assert client.right > client.left and client.bottom > client.top


def test_invalid_monitor_work_area_does_not_zero_client(caplog):
    """显示器拓扑切换时即使系统临时返回不相交的工作区，也不提交空客户区。"""
    frame = object.__new__(_WindowsFrame)
    frame.hwnd = 1

    def get_monitor_info(monitor, address):
        info = ctypes.cast(address, ctypes.POINTER(_MonitorInfo)).contents
        info.work = wintypes.RECT(-3840, 0, 0, 2088)
        return True

    frame.user32 = SimpleNamespace(IsZoomed=lambda hwnd: True, MonitorFromRect=lambda *args: 1,
                                   GetMonitorInfoW=get_monitor_info)
    client = wintypes.RECT(0, 0, 3840, 2088)
    frame.constrain_maximized_client(ctypes.addressof(client))
    assert (client.left, client.top, client.right, client.bottom) == (0, 0, 3840, 2088)
    assert "没有有效交集" in caplog.text
