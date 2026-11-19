"""接收 Windows 全局窗口快捷键，原生接收窗口独立于主窗口的可见性和句柄。"""

import ctypes
import logging
import sys
from collections.abc import Callable
from ctypes import wintypes

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QWindow
from PySide6.QtWidgets import QApplication, QWidget

_LOG = logging.getLogger(__name__)
_WM_HOTKEY = 0x0312
_MOD_NOREPEAT = 0x4000
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_ERROR_HOTKEY_NOT_REGISTERED = 1419
_MODIFIERS = {
    Qt.KeyboardModifier.AltModifier: 0x0001,
    Qt.KeyboardModifier.ControlModifier: 0x0002,
    Qt.KeyboardModifier.ShiftModifier: 0x0004,
}
_SUPPORTED_MODIFIERS = sum(modifier.value for modifier in _MODIFIERS)


def _windows_keys(sequence: QKeySequence) -> tuple[int, int] | None:
    """将单组 Qt 按键转换为 Windows 修饰键与虚拟键。"""
    if sequence.isEmpty():
        return None
    if sequence.count() != 1:
        raise ValueError("请输入一组组合键。")
    combination = sequence[0]
    modifiers = combination.keyboardModifiers()
    if modifiers.value & ~_SUPPORTED_MODIFIERS:
        raise ValueError("可选修饰键为 Ctrl、Alt 和 Shift。")
    key = combination.key().value
    if key == Qt.Key.Key_F12 or (key == Qt.Key.Key_F4 and modifiers == Qt.KeyboardModifier.AltModifier):
        raise ValueError("该组合键由 Windows 保留，请改用其他组合键。")
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
        key = 0x70 + key - Qt.Key.Key_F1
    elif not (Qt.Key.Key_A <= key <= Qt.Key.Key_Z or Qt.Key.Key_0 <= key <= Qt.Key.Key_9):
        raise ValueError("请使用字母、数字或功能键。")
    return sum(flag for modifier, flag in _MODIFIERS.items() if modifiers & modifier), key


def _load_user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for name, arguments, result in (
        ("RegisterHotKey", [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        ("UnregisterHotKey", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
        ("IsWindowEnabled", [wintypes.HWND], wintypes.BOOL),
        ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        ("GetLastActivePopup", [wintypes.HWND], wintypes.HWND),
    ):
        function = getattr(user32, name)
        function.argtypes = arguments
        function.restype = result
    return user32


class WindowsVisibilityShortcut(QWindow):
    """使用从未显示的 HWND 接收消息，包括 Windows 原生对话框的消息循环。"""

    activated = Signal()

    @classmethod
    def create_for_platform(cls):
        if sys.platform == "win32" and QApplication.platformName() == "windows":
            return cls()
        return None

    def __init__(self):
        super().__init__()
        self._user32 = _load_user32()
        self._keys = None
        self._registration_id = 0
        self._next_id = 1

    def replace(self, sequence: QKeySequence, persist: Callable[[], None] | None = None) -> None:
        """先注册候选组合并保存偏好，成功后才切换消息分发并释放旧组合。"""
        keys = _windows_keys(sequence)
        new_id = 0
        if keys is not None and keys != self._keys:
            if self._next_id == self._registration_id:
                self._next_id = self._next_id % 0xBFFF + 1
            new_id = self._next_id
            self._next_id = new_id % 0xBFFF + 1
            modifiers, key = keys
            if not self._user32.RegisterHotKey(int(self.winId()), new_id, modifiers | _MOD_NOREPEAT, key):
                error = ctypes.get_last_error()
                if error == _ERROR_HOTKEY_ALREADY_REGISTERED:
                    raise ValueError("该组合键已被系统或其他应用占用。")
                raise OSError(f"Windows 无法注册该组合键（错误 {error}）。")
        try:
            if persist is not None:
                persist()
        except BaseException:
            self._unregister(new_id)
            raise
        if keys != self._keys:
            old_id = self._registration_id
            self._keys, self._registration_id = keys, new_id
            self._unregister(old_id)

    def _unregister(self, registration_id: int) -> None:
        if registration_id and not self._user32.UnregisterHotKey(int(self.winId()), registration_id):
            error = ctypes.get_last_error()
            if error != _ERROR_HOTKEY_NOT_REGISTERED:
                _LOG.warning("Windows 无法释放窗口快捷键（错误 %d）", error)

    def nativeEvent(self, event_type, message):
        if self._registration_id and bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_HOTKEY and msg.wParam == self._registration_id:
                # 在处理快捷键消息时激活窗口，使用本次用户输入授予的前台权限。
                self.activated.emit()
                return True, 0
        return False, 0

    def activate_window(self, window: QWidget) -> None:
        if not self._user32.SetForegroundWindow(int(window.winId())):
            window.activateWindow()

    def activate_native_dialog(self, owner: QWidget) -> bool:
        """在原生文件选择等对话框禁用主窗口时，优先唤回其可见弹窗。"""
        handle = owner.windowHandle()
        if handle is None:
            return False
        hwnd = int(handle.winId())
        if self._user32.IsWindowEnabled(hwnd):
            return False
        popup = self._user32.GetLastActivePopup(hwnd)
        if popup and popup != hwnd and self._user32.IsWindowVisible(popup):
            self._user32.SetForegroundWindow(popup)
        return True

    def release(self) -> None:
        registration_id = self._registration_id
        self._keys, self._registration_id = None, 0
        self._unregister(registration_id)
        self.destroy()
