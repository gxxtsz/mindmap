"""在 Qt 的桌面坐标中记录主窗口位置，并按当前显示器工作区恢复。"""

from collections.abc import Iterable

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication, QWidget

_WINDOW_STATES = {
    "normal": Qt.WindowState.WindowNoState,
    "maximized": Qt.WindowState.WindowMaximized,
    "fullscreen": Qt.WindowState.WindowFullScreen,
}


def capture_window_placement(window: QWidget) -> dict:
    """在 GUI 线程记录当前显示器、普通窗口矩形及显示状态。

    最大化或全屏时仍记录还原后的尺寸。保存时忽略最小化标志，保留 Qt 同时
    记录的最大化或全屏状态。矩形均采用 Qt 的桌面坐标。
    """
    screen = window.screen()
    normal = window.normalGeometry()
    if not normal.isValid():
        normal = window.geometry()
    mode = "fullscreen" if window.isFullScreen() else "maximized" if window.isMaximized() else "normal"
    return {
        "mode": mode,
        "normal_geometry": list(normal.getRect()),
        "screen_name": screen.name() if screen is not None else "",
        "work_area": list(screen.availableGeometry().getRect()) if screen is not None else None,
    }


def _read_rect(values: object) -> QRect | None:
    """解析已保存的矩形，拒绝异常尺寸以及无法传给 Qt 的坐标。"""
    if (not isinstance(values, list) or len(values) != 4
            or any(type(value) is not int or abs(value) > 1_000_000 for value in values)
            or min(values[2:]) <= 0):
        return None
    return QRect(*values)


def _overlapping_screen(rect: QRect, screens: Iterable[QScreen]) -> QScreen | None:
    """逐块比较实际屏幕矩形，桌面外接矩形中的空隙不会参与匹配。"""
    def overlap(screen: QScreen) -> int:
        intersection = screen.geometry().intersected(rect)
        return intersection.width() * intersection.height()

    screen = max(screens, key=overlap, default=None)
    return screen if screen is not None and overlap(screen) > 0 else None


def restore_window_placement(
    window: QWidget, saved: object = None, legacy_geometry: object = None,
) -> None:
    """在首次显示前设置主窗口的位置、普通尺寸与显示状态。

    优先匹配保存的显示器名称，同名时用工作区坐标区分。显示器移动后保留窗口
    相对工作区的位置，显示器缺失时在主屏居中。旧会话只有窗口矩形，按与当前
    显示器的交集选择屏幕，矩形恰好覆盖工作区或整屏时推断对应的显示状态。
    位置和大小限制在当前工作区，确保旧坐标或屏幕缩放变化后仍能找到窗口。
    """
    primary = QApplication.primaryScreen()
    if primary is None:
        return
    screens = QApplication.screens()
    record = saved if isinstance(saved, dict) else {}
    normal = _read_rect(record.get("normal_geometry"))
    old_work = _read_rect(record.get("work_area"))
    name, mode = record.get("screen_name"), record.get("mode")
    if (normal is not None and old_work is not None and isinstance(name, str)
            and isinstance(mode, str) and mode in _WINDOW_STATES):
        matches = [screen for screen in screens if not name or screen.name() == name]
        screen = _overlapping_screen(old_work, matches) or (matches[0] if matches else None)
        if screen is not None and old_work.intersects(normal):
            normal.translate(screen.availableGeometry().topLeft() - old_work.topLeft())
        else:
            screen = screen or primary
            normal.moveCenter(screen.availableGeometry().center())
    else:
        normal = _read_rect(legacy_geometry)
        screen = _overlapping_screen(normal, screens) if normal is not None else None
        mode = "normal"
        if screen is not None and normal == screen.availableGeometry():
            mode = "maximized"
        elif screen is not None and normal == screen.geometry():
            mode = "fullscreen"
        if screen is None or mode != "normal":
            screen = screen or primary
            normal = QRect(QPoint(), window.size())
            normal.moveCenter(screen.availableGeometry().center())

    work = screen.availableGeometry()
    if not work.isValid():
        return
    available = work.size()
    minimum = window.minimumSize()
    # 工作区容不下最小窗口时，以启动尺寸为上限保留布局空间，
    # 下方仍把窗口左上角放入工作区，避免窗口整体落到屏幕外。
    maximum = available if available.expandedTo(minimum) == available else window.size()
    size = normal.size().boundedTo(maximum).expandedTo(minimum).boundedTo(window.maximumSize())
    x = max(work.left(), min(normal.x(), work.right() - size.width() + 1))
    y = max(work.top(), min(normal.y(), work.bottom() - size.height() + 1))
    if window.isMaximized() or window.isFullScreen() or window.isMinimized():
        window.setWindowState(Qt.WindowState.WindowNoState)
    # 先把普通矩形放入目标屏幕，再请求最大化或全屏，让 Qt 在该屏完成窗口状态切换。
    window.setGeometry(QRect(QPoint(x, y), size))
    window.setWindowState(_WINDOW_STATES[mode])
