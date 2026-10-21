"""按钮状态回归：自动焦点、键盘焦点与真实鼠标悬停须有不同的视觉反馈。"""

import pytest
from mindmap_docking import DockPosition, ToolWindowSpec
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
    # 分数缩放的离屏窗口初次显示时可能留有悬停标记；明确进入再离开按钮，
    # 用真实鼠标事件建立一致的测试起点，不改变 Qt 自动选择的键盘焦点。
    more = window.host.left_rail.more
    qtbot.mouseMove(more, more.rect().center())
    qtbot.mouseMove(window.manager.documents["editor"]["widget"], QPoint(20, 10))
    QApplication.processEvents()
    yield window
    window.close()


@pytest.fixture
def tool_button_window(button_window):
    """在真实桌面外壳中注册两个可同时打开的工具，保持主题、焦点与背景继承一致。"""
    for tool_id, position in (("alpha", DockPosition.LEFT_TOP), ("beta", DockPosition.LEFT_BOTTOM)):
        button_window.manager.register_tool_window(ToolWindowSpec(
            tool_id, "测试工具 " + tool_id, lambda: QLineEdit("工具内容"), position=position,
        ))
    button_window.manager.refresh()
    QApplication.processEvents()
    return button_window


def background_color(button, x, y=None):
    """读取完整窗口中的按钮像素，默认取图标左侧背景，避免透明截图丢失父级底色。"""
    window = button.window()
    image = window.grab().toImage()
    point = button.mapTo(window, QPoint(x, button.height() // 2 if y is None else y))
    ratio = image.devicePixelRatio()
    return image.pixelColor(int((point.x() + 0.5) * ratio), int((point.y() + 0.5) * ratio))


def assert_icon_color(button, color):
    """从真实图标区域确认前景色，覆盖已打开工具的活动与非活动着色路径。"""
    image = button.grab().toImage()
    ratio = image.devicePixelRatio()
    size = 16 if button.manager.theme.compact else 20
    left, top = (button.width() - size) // 2, (button.height() - size) // 2
    assert any(image.pixelColor(int((x + 0.5) * ratio), int((y + 0.5) * ratio)) == color
               for y in range(top, top + size) for x in range(left, left + size))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_tool_buttons_follow_panel_focus_and_open_state(tool_button_window, qtbot, theme, compact):
    """真实焦点在两个面板和编辑器之间移动时，蓝底白图标与柔和打开态及时切换。"""
    window = tool_button_window
    manager = window.manager
    editor = manager.documents["editor"]["widget"]
    manager.set_theme(theme)
    manager.set_option("compact", compact)
    QApplication.processEvents()
    qtbot.mouseMove(editor, editor.rect().center())
    alpha = window.host.left_rail._buttons["alpha"]
    beta = window.host.left_rail._buttons["beta"]

    manager.activate("alpha")
    qtbot.waitUntil(lambda: manager.active_id == "alpha")
    assert alpha.isChecked()
    assert background_color(alpha, 7) == manager.theme.color("ToolWindow.Button.selectedBackground")
    assert_icon_color(alpha, manager.theme.color("ToolWindow.Button.selectedForeground"))

    manager.activate("beta")
    qtbot.waitUntil(lambda: manager.active_id == "beta")
    assert alpha.isChecked() and beta.isChecked()
    assert background_color(beta, 7) == manager.theme.color("ToolWindow.Button.selectedBackground")
    assert_icon_color(beta, manager.theme.color("ToolWindow.Button.selectedForeground"))
    qtbot.mouseMove(alpha, alpha.rect().center())
    qtbot.waitUntil(alpha.underMouse)
    assert background_color(alpha, 7) == manager.theme.color("ToolWindow.Button.inactiveBackground")
    assert_icon_color(alpha, manager.theme.color("ToolWindow.Button.inactiveForeground"))

    # 编辑器获得焦点必须发布活动项清空通知，侧栏才能立即刷新，而非等下一次悬停。
    changes = []
    manager.active_changed.connect(changes.append)
    editor.setFocus()
    qtbot.waitUntil(lambda: manager.active_id == "")
    assert changes == [""]
    assert background_color(beta, 7) == manager.theme.color("ToolWindow.Button.inactiveBackground")
    assert_icon_color(beta, manager.theme.color("ToolWindow.Button.inactiveForeground"))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_stripe_focus_ring_is_separate_from_tool_fill(tool_button_window, qtbot, theme, compact):
    """核验外侧两像素焦点圈、填充间隙及焦点离开后的撤销，活动蓝底可与描边共存。"""
    window = tool_button_window
    manager = window.manager
    editor = manager.documents["editor"]["widget"]
    manager.set_theme(theme)
    manager.set_option("compact", compact)
    manager.activate("alpha")
    button = window.host.left_rail._buttons["alpha"]
    QApplication.processEvents()
    qtbot.mouseMove(editor, QPoint(20, 10))
    editor.setFocus()
    qtbot.waitUntil(lambda: manager.active_id == "")
    button.setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    assert button.hasFocus() and button.isChecked()

    for active in (False, True):
        if active:
            manager.mark_active("alpha")
        accent = manager.theme.color("control-brand-border")
        center = button.width() // 2
        assert all(background_color(button, center, y) == accent for y in (0, 1))
        gap = range(2, 3 if compact else 4)
        assert all(background_color(button, center, y) == manager.theme.color("main-window-bg") for y in gap)
        fill_key = "selectedBackground" if active else "inactiveBackground"
        assert background_color(button, 7) == manager.theme.color("ToolWindow.Button." + fill_key)

    editor.setFocus()
    qtbot.waitUntil(lambda: not button.hasFocus() and manager.active_id == "")
    assert background_color(button, button.width() // 2, 1) == manager.theme.color("main-window-bg")
    assert background_color(button, 7) == manager.theme.color("ToolWindow.Button.inactiveBackground")


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
@pytest.mark.parametrize("source", ["more", "menu"])
def test_hover_and_menu_cancel_leave_no_background(button_window, qtbot, theme, source):
    """侧栏更多和左上角主菜单在悬停及取消弹窗后均恢复普通背景。"""
    window = button_window
    window.manager.set_theme(theme)
    button = window.host.left_rail.more if source == "more" else window.title_bar.menu
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
