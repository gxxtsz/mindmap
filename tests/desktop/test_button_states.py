"""按钮状态回归：自动焦点、键盘焦点与真实鼠标悬停须有不同的视觉反馈。"""

import pytest
from mindmap_shell import AppSpec, WorkbenchWindow
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QApplication, QLineEdit


@pytest.fixture
def button_window(qtbot, tmp_path):
    """使用独立配置创建完整外壳，保留 Qt 启动时自动选择焦点的真实行为。"""
    window = WorkbenchWindow(AppSpec("button-state-test", "按钮状态验收", storage_dir=tmp_path))
    window.manager.add_document("editor", "测试文档", QLineEdit("编辑区"))
    qtbot.addWidget(window)
    window.show()
    window.activateWindow()
    QApplication.processEvents()
    yield window
    window.close()


def background_color(button, x):
    """读取完整窗口中图标左侧的背景，避免透明按钮单独截图丢失父级底色。"""
    window = button.window()
    image = window.grab().toImage()
    point = button.mapTo(window, QPoint(x, button.height() // 2))
    ratio = image.devicePixelRatio()
    return image.pixelColor(int((point.x() + 0.5) * ratio), int((point.y() + 0.5) * ratio))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_automatic_focus_does_not_look_like_hover(button_window, qtbot, theme, compact):
    """未经点击的更多按钮可能被 Qt 自动聚焦，此时仍须保持侧栏的普通底色。"""
    window = button_window
    window.manager.set_theme(theme)
    window.manager.set_option("compact", compact)
    button = window.host.left_rail.more
    editor = window.manager.documents["editor"]["widget"]
    qtbot.mouseMove(editor, QPoint(20, 10))
    editor.setFocus()
    button.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
    QApplication.processEvents()
    assert button.hasFocus()
    assert not button.underMouse()
    assert not button.isDown() and not button.isChecked()
    assert background_color(button, 7) == window.manager.theme.color("main-window-bg")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_keyboard_focus_uses_outline_and_still_opens_menu(button_window, qtbot, theme):
    """键盘访问保留可见描边和空格激活能力，焦点自身不能填充悬浮底色。"""
    window = button_window
    window.manager.set_theme(theme)
    editor = window.manager.documents["editor"]["widget"]
    button = window.host.left_rail.more
    qtbot.mouseMove(editor, QPoint(20, 10))
    editor.setFocus()
    button.setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    assert button.hasFocus()
    assert background_color(button, 7) == window.manager.theme.color("main-window-bg")
    image = button.grab().toImage()
    accent = window.manager.theme.color("control-brand-border")
    assert any(image.pixelColor(x, y) == accent
               for y in range(image.height()) for x in range(image.width()))

    opened = []

    def close_menu():
        """在菜单自身的事件循环中核验弹出，再用 Escape 模拟键盘取消。"""
        popup = QApplication.activePopupWidget()
        assert popup is not None
        opened.append(True)
        qtbot.keyClick(popup, Qt.Key.Key_Escape)

    QTimer.singleShot(0, close_menu)
    qtbot.keyClick(button, Qt.Key.Key_Space)
    assert opened == [True]
    assert not button.isDown()
    assert background_color(button, 7) == window.manager.theme.color("main-window-bg")


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("source", ["more", "gear"])
def test_hover_and_menu_cancel_leave_no_background(button_window, qtbot, theme, source):
    """真实移入、移出以及菜单打开后取消均应恢复普通背景，覆盖截图中的两个入口。"""
    window = button_window
    window.manager.set_theme(theme)
    button = window.host.left_rail.more if source == "more" else window.title_bar.options
    editor = window.manager.documents["editor"]["widget"]
    x = 7 if source == "more" else 3
    editor.setFocus()
    qtbot.mouseMove(button, button.rect().center())
    qtbot.waitUntil(button.underMouse)
    assert background_color(button, x) == window.manager.theme.color("toolbar-bg-hovered")
    qtbot.mouseMove(editor, QPoint(20, 10))
    qtbot.waitUntil(lambda: not button.underMouse())
    assert background_color(button, x) == window.manager.theme.color("main-window-bg")

    def cancel_menu():
        """先把鼠标移到菜单与按钮外，再关闭菜单，覆盖弹窗期间移走鼠标的路径。"""
        popup = QApplication.activePopupWidget()
        assert popup is not None
        qtbot.mouseMove(editor, QPoint(20, 10))
        popup.close()

    QTimer.singleShot(0, cancel_menu)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert not button.underMouse()
    assert not button.isDown()
    assert background_color(button, x) == window.manager.theme.color("main-window-bg")
