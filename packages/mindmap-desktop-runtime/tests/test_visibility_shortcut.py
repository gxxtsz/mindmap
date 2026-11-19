"""从设置菜单到系统注册和窗口状态的回归测试。"""

import ctypes
from ctypes import wintypes

import pytest
from mindmap_desktop_runtime import DesktopConfig, DesktopRuntimeWindow
from mindmap_desktop_runtime.ui.shell import _windows_hotkey
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
)

SETTING = "window/visibility_shortcut"
TITLE = "显示/隐藏窗口快捷键"
PRIMARY = "Ctrl+Alt+F10"
SECONDARY = "Ctrl+Alt+F11"
SAVE = QDialogButtonBox.StandardButton.Save
CANCEL = QDialogButtonBox.StandardButton.Cancel


class HotkeyRegistry:
    """记录实际 Win32 参数，保留系统重复注册和释放语义。"""

    def __init__(self):
        self.bindings = {}
        self.occupied = set()
        self.foreground = []

    def RegisterHotKey(self, hwnd, identifier, modifiers, key):
        assert modifiers & 0x4000
        chord = modifiers & ~0x4000, key
        if chord in self.occupied or chord in self.bindings.values():
            return False
        self.bindings[hwnd, identifier] = chord
        return True

    def UnregisterHotKey(self, hwnd, identifier):
        del self.bindings[hwnd, identifier]
        return True

    def SetForegroundWindow(self, hwnd):
        self.foreground.append(hwnd)
        # 让 QWidget.activateWindow 在 Qt 离屏平台接管焦点。
        return False

    def IsWindowEnabled(self, hwnd):
        return True

    def notify(self, window, identifier=None):
        if identifier is None:
            _, identifier = next(iter(self.bindings))
        msg = wintypes.MSG(message=0x0312, wParam=identifier)
        return window._visibility_shortcut.nativeEvent(b"windows_generic_MSG", ctypes.addressof(msg))[0]


@pytest.fixture
def desktop(qtbot, tmp_path, application_factory, monkeypatch):
    registry = HotkeyRegistry()
    monkeypatch.setattr(_windows_hotkey, "_load_user32", lambda: registry)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1409, raising=False)
    monkeypatch.setattr(_windows_hotkey.WindowsVisibilityShortcut, "create_for_platform",
                        classmethod(lambda cls: cls()))

    def build(config=None):
        window = DesktopRuntimeWindow(
            config=config, storage_dir=tmp_path, restore=False, application_factory=application_factory,
        )
        qtbot.addWidget(window, before_close_func=lambda widget: widget.dispose())
        window.show()
        return window

    return build, registry


@pytest.fixture
def available_tray(monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)


def open_editor(window, qtbot):
    menu = window.main_menu()
    settings = next(action.menu() for action in menu.actions() if action.text() == "设置")
    next(action for action in settings.actions() if action.text() == TITLE + "…").trigger()
    menu.deleteLater()
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is not None)
    dialog = QApplication.activeModalWidget()
    return dialog, dialog.findChild(QKeySequenceEdit), dialog.findChild(QDialogButtonBox)


def click(box, button, qtbot):
    qtbot.mouseClick(box.button(button), Qt.MouseButton.LeftButton)


def save_shortcut(window, qtbot, value):
    _, field, box = open_editor(window, qtbot)
    field.setKeySequence(QKeySequence(value))
    click(box, SAVE, qtbot)
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is None)


def has_error(dialog, text):
    return any(label.isVisible() and text in label.text() for label in dialog.findChildren(QLabel))


@pytest.mark.parametrize(("key", "modifiers", "shortcut", "registered"), [
    (Qt.Key.Key_F10, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier, PRIMARY, (3, 0x79)),
    (Qt.Key.Key_F3, Qt.KeyboardModifier.NoModifier, "F3", (0, 0x72)),
    (Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "A", (0, ord("A"))),
    (Qt.Key.Key_1, Qt.KeyboardModifier.NoModifier, "1", (0, ord("1"))),
    (Qt.Key.Key_A, Qt.KeyboardModifier.ShiftModifier, "Shift+A", (4, ord("A"))),
])
def test_shortcut_capture_cancel_clear_restart_and_release(desktop, qtbot, key, modifiers, shortcut, registered):
    build, registry = desktop
    window = build()
    assert not registry.bindings and window.documents.active is None
    _, field, box = open_editor(window, qtbot)
    assert field.keySequence().isEmpty() and field.isClearButtonEnabled()
    qtbot.keyClick(field, key, modifiers)
    assert field.keySequence() == QKeySequence(shortcut)
    click(box, SAVE, qtbot)
    assert window.settings.value(SETTING) == shortcut
    assert list(registry.bindings.values()) == [registered]

    _, field, box = open_editor(window, qtbot)
    assert field.keySequence() == QKeySequence(shortcut)
    field.setKeySequence(QKeySequence(SECONDARY))
    click(box, CANCEL, qtbot)
    assert window.settings.store.load()["values"][""][SETTING] == shortcut
    assert list(registry.bindings.values()) == [registered]
    assert window.close() and not registry.bindings

    reopened = build()
    assert list(registry.bindings.values()) == [registered]
    _, field, box = open_editor(reopened, qtbot)
    field.clear()
    click(box, SAVE, qtbot)
    assert reopened.settings.value(SETTING) == "" and not registry.bindings
    assert reopened.close()
    assert build().settings.value(SETTING) == "" and not registry.bindings


@pytest.mark.parametrize(("candidate", "error"), [
    ("Meta+A", "修饰键"),
    ("F12", "Windows 保留"),
    ("Ctrl+Alt+F12", "Windows 保留"),
    ("Ctrl+S", "「保存」"),
    ("Ctrl+0", "「适应窗口」"),
    ("F2", "「编辑文字」"),
    ("F3", "其他应用占用"),
    (SECONDARY, "其他应用占用"),
])
def test_invalid_or_conflicting_shortcut_keeps_old_registration(desktop, qtbot, candidate, error):
    build, registry = desktop
    window = build(DesktopConfig(reserved_shortcuts=(("适应窗口", "Ctrl+0"), ("编辑文字", "F2"))))
    save_shortcut(window, qtbot, PRIMARY)
    original = registry.bindings.copy()
    registry.occupied.update(((3, 0x7A), (0, 0x72)))
    dialog, field, box = open_editor(window, qtbot)
    field.setKeySequence(QKeySequence(candidate))
    click(box, SAVE, qtbot)
    assert dialog.isVisible() and has_error(dialog, error)
    assert registry.bindings == original
    assert window.settings.value(SETTING) == PRIMARY
    click(box, CANCEL, qtbot)


def test_write_failure_releases_candidate_and_retry_commits(desktop, qtbot, monkeypatch):
    build, registry = desktop
    window = build()
    save_shortcut(window, qtbot, PRIMARY)
    original = registry.bindings.copy()
    _, old_id = next(iter(original))
    store = window.settings.store
    dialog, field, box = open_editor(window, qtbot)
    field.setKeySequence(QKeySequence(SECONDARY))

    def reject(payload):
        assert set(registry.bindings.values()) == {(3, 0x79), (3, 0x7A)}
        assert window.settings.value(SETTING) == PRIMARY
        assert payload["values"][""][SETTING] == SECONDARY
        raise OSError("模拟写入失败")

    with monkeypatch.context() as patch:
        patch.setattr(store, "save", reject)
        click(box, SAVE, qtbot)
    assert dialog.isVisible() and has_error(dialog, "模拟写入失败")
    assert store.load()["values"][""][SETTING] == PRIMARY and registry.bindings == original
    click(box, SAVE, qtbot)
    assert list(registry.bindings.values()) == [(3, 0x7A)]
    assert window.settings.value(SETTING) == SECONDARY
    assert not registry.notify(window, old_id)


def test_occupied_saved_shortcut_can_be_retried(desktop, qtbot):
    build, registry = desktop
    window = build()
    save_shortcut(window, qtbot, PRIMARY)
    assert window.close()
    registry.occupied.add((3, 0x79))
    reopened = build()
    assert "占用" in reopened.status.currentMessage() and not registry.bindings
    _, field, box = open_editor(reopened, qtbot)
    assert field.keySequence() == QKeySequence(PRIMARY)
    registry.occupied.clear()
    click(box, SAVE, qtbot)
    assert len(registry.bindings) == 1


def test_hotkey_brings_back_window_and_modal_without_closing_documents(desktop, qtbot, available_tray):
    build, registry = desktop
    window = build()
    assert window.new_document()
    document = window.documents.active
    save_shortcut(window, qtbot, PRIMARY)
    window.showMaximized()
    auxiliary = QWidget()
    qtbot.addWidget(auxiliary)
    auxiliary.show()
    auxiliary.activateWindow()
    qtbot.waitUntil(auxiliary.isActiveWindow)

    assert registry.notify(window) and window.isVisible()
    qtbot.waitUntil(window.isActiveWindow)
    assert registry.notify(window) and window.isHidden()
    tray = window.findChild(QSystemTrayIcon)
    assert tray.isVisible()
    assert window.documents.active is document
    assert registry.notify(window) and window.isMaximized() and window.isVisible()
    assert not tray.isVisible()
    window.showMinimized()
    assert registry.notify(window) and window.isMaximized() and not window.isMinimized()

    dialog = QDialog(window)
    qtbot.addWidget(dialog)
    dialog.open()
    assert registry.notify(window) and registry.foreground[-1] == int(dialog.winId())
    assert window.isVisible() and dialog.isVisible() and window.documents.active is document
    dialog.reject()


@pytest.mark.parametrize("from_menu", [False, True])
def test_tray_icon_and_menu_restore_window(desktop, qtbot, available_tray, from_menu):
    build, registry = desktop
    window = build()
    assert window.new_document()
    document = window.documents.active
    save_shortcut(window, qtbot, "F3")
    window.showMaximized()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    assert window.findChild(QSystemTrayIcon) is None

    assert registry.notify(window) and window.isHidden()
    tray = window.findChild(QSystemTrayIcon)
    assert tray.isVisible() and tray.toolTip() == window.config.title
    assert tray.icon().cacheKey() == window.windowIcon().cacheKey()
    if from_menu:
        tray.activated.emit(QSystemTrayIcon.ActivationReason.Context)
        assert window.isHidden() and tray.isVisible()
        action = next(action for action in tray.contextMenu().actions() if action.text() == "显示窗口")
        action.trigger()
    else:
        tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert window.isVisible() and window.isMaximized() and window.documents.active is document
    assert not tray.isVisible()
    assert registry.foreground[-1] == int(window.winId())


def test_hotkey_minimizes_when_the_system_tray_is_unavailable(desktop, qtbot, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    build, registry = desktop
    window = build()
    save_shortcut(window, qtbot, "F3")
    window.showMaximized()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)

    assert registry.notify(window) and window.isMinimized() and window.isVisible()
    assert window.findChild(QSystemTrayIcon) is None
    assert registry.notify(window) and not window.isMinimized() and window.isMaximized()


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard])
def test_quit_from_tray_restores_window_for_the_save_prompt(desktop, qtbot, available_tray, monkeypatch, answer):
    build, registry = desktop
    window = build()
    assert window.new_document()
    document = window.documents.active
    document.widget.insertPlainText("未保存的修改")
    save_shortcut(window, qtbot, "F3")
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    assert registry.notify(window) and window.isHidden()
    tray = window.findChild(QSystemTrayIcon)
    assert tray.isVisible()
    prompts = []

    def confirm(view):
        assert window.isVisible() and not tray.isVisible()
        prompts.append(view)
        return answer

    monkeypatch.setattr(window, "confirm_close", confirm)
    action = next(action for action in tray.contextMenu().actions() if action.text() == "退出")
    action.trigger()
    assert prompts == [document] and not tray.isVisible()
    if answer == QMessageBox.StandardButton.Cancel:
        assert window.isVisible() and window.documents.active is document
        assert document.document.is_dirty and registry.bindings
    else:
        assert window.isHidden() and not registry.bindings
