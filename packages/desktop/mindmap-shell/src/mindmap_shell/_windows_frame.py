"""为集成标题栏接入 Windows 系统阴影，不在 Qt 内容区预留投影边距。

仅在真实 Windows 平台窗口上加载系统库；离屏测试和其他平台保持原有窗口行为。
自绘标题栏及缩放命中仍由外壳负责，DWM 只负责窗口外部的合成效果。
参考：https://learn.microsoft.com/windows/win32/dwm/customframe
"""

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtWidgets import QApplication


class _Margins(ctypes.Structure):
    """DWM 的四边扩展量，顺序与系统 MARGINS 结构一致，单位为物理像素。"""

    _fields_ = [(name, ctypes.c_int) for name in ("left", "right", "top", "bottom")]


class _MonitorInfo(ctypes.Structure):
    """读取当前显示器工作区，避免最大化后的内容被系统缩放边框或任务栏遮挡。"""

    _fields_ = [
        ("size", wintypes.DWORD), ("monitor", wintypes.RECT),
        ("work", wintypes.RECT), ("flags", wintypes.DWORD),
    ]


class _TrackMouseEvent(ctypes.Structure):
    """跟踪非客户区离开通知；HWND 使用指针宽度，避免 64 位系统句柄被截断。"""

    _fields_ = [
        ("size", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwnd", wintypes.HWND), ("hover_time", wintypes.DWORD),
    ]


def _track_nonclient_mouse_leave(hwnd: int) -> bool:
    """订阅一次 WM_NCMOUSELEAVE，鼠标进入非客户区后由调用方重新订阅。

    鼠标消息不依赖 DWM 是否支持系统边框，因此跟踪与合成适配分别初始化。
    这里只监听离开，不截获悬停消息，保留 Windows 最大化按钮的贴靠菜单。
    离屏平台没有有效 HWND，不调用系统接口；失败时由后续移动消息再次尝试。
    """
    if sys.platform != "win32" or QApplication.platformName() != "windows":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TrackMouseEvent)]
    user32.TrackMouseEvent.restype = wintypes.BOOL
    # TME_LEAVE | TME_NONCLIENT：直接移出整个窗口也必须撤销按钮悬浮底色。
    tracking = _TrackMouseEvent(ctypes.sizeof(_TrackMouseEvent), 0x00000012, hwnd, 0)
    return bool(user32.TrackMouseEvent(ctypes.byref(tracking)))


class _WindowsFrame:
    """绑定单个 HWND 的系统边框能力；句柄重建后必须重新创建此对象。

    所有指针和返回值显式声明，避免 64 位 Python 默认按 32 位整数截断 HWND。
    不更换 Qt 窗口标志，不创建透明外窗，因此窗口位置、内容尺寸与贴靠区域不变。
    """

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.native_border = False
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        suffix = "PtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "W"
        self.get_style = getattr(self.user32, "GetWindowLong" + suffix)
        self.get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_style.restype = ctypes.c_ssize_t
        self.set_style = getattr(self.user32, "SetWindowLong" + suffix)
        self.set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.set_style.restype = ctypes.c_ssize_t
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.IsZoomed.argtypes = [wintypes.HWND]
        self.user32.IsZoomed.restype = wintypes.BOOL
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HANDLE
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
        self.user32.GetMonitorInfoW.restype = wintypes.BOOL
        self.dwm.DwmIsCompositionEnabled.argtypes = [ctypes.POINTER(wintypes.BOOL)]
        self.dwm.DwmIsCompositionEnabled.restype = ctypes.c_long
        self.dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        self.dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(_Margins)]
        self.dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long

    def enable(self) -> bool:
        """恢复系统合成所需的窗口样式；不改变位置、尺寸、层级或键盘焦点。

        Qt 无边框模式会移除 WS_THICKFRAME；补回后，Qt 仍按
        FramelessWindowHint 将整个窗口作为客户区，所以不会出现第二条标题栏。
        不支持桌面合成时直接返回 False，调用方仍能保留自绘细边框。
        """
        enabled = wintypes.BOOL()
        if self.dwm.DwmIsCompositionEnabled(ctypes.byref(enabled)) < 0 or not enabled.value:
            return False
        style = self.get_style(self.hwnd, -16)  # GWL_STYLE
        if not style:
            raise ctypes.WinError(ctypes.get_last_error())
        # 仅补回 WS_THICKFRAME；避免引入 WS_CAPTION 的标题高度造成初始位置偏移。
        # 保留 Qt 已经设置的最小化、最大化、可见状态与完整自绘标题栏。
        if not self.set_style(self.hwnd, -16, style | 0x00040000):
            raise ctypes.WinError(ctypes.get_last_error())
        # SWP_FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER | NOACTIVATE。
        if not self.user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0037):
            raise ctypes.WinError(ctypes.get_last_error())
        return True

    def refresh(self, *, maximized: bool, fullscreen: bool, dark: bool, border_color: int) -> None:
        """同步合成外观；最大化、全屏时收起外阴影，主题切换沿用同一 HWND。

        border_color 使用 Win32 COLORREF（0x00BBGGRR），由宿主主题转换传入。
        Windows 11 统一绘制轮廓和圆角，避免 Qt 的直角描边被系统圆角裁断。
        native_border 仅在系统接受边框属性时为 True，宿主据此隐藏自绘轮廓；
        旧系统或合成失败时为 False，继续使用自绘边框，不影响窗口内容及几何。
        """
        policy = ctypes.c_int(2)  # DWMWA_NCRENDERING_POLICY / DWMNCRP_ENABLED
        self.dwm.DwmSetWindowAttribute(self.hwnd, 2, ctypes.byref(policy), ctypes.sizeof(policy))
        # 普通窗口请求系统圆角；最大化和全屏保持直角且不额外描边。
        corner = ctypes.c_int(1 if maximized or fullscreen else 2)  # DWMWCP_DONOTROUND / ROUND
        self.dwm.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        border = wintypes.DWORD(0xFFFFFFFE if maximized or fullscreen else border_color)
        result = self.dwm.DwmSetWindowAttribute(self.hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
        self.native_border = result >= 0
        appearance = wintypes.BOOL(dark)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        self.dwm.DwmSetWindowAttribute(self.hwnd, 20, ctypes.byref(appearance), ctypes.sizeof(appearance))
        margin = 0 if maximized or fullscreen else 1
        self.dwm.DwmExtendFrameIntoClientArea(self.hwnd, ctypes.byref(_Margins(*([margin] * 4))))

    def constrain_maximized_client(self, address: int) -> None:
        """在 WM_NCCALCSIZE 中将最大化客户区裁到当前显示器的可用工作区。

        address 指向 NCCALCSIZE_PARAMS；其首字段就是待计算的窗口 RECT。
        使用 Win32 的物理坐标，避免在高 DPI 或副屏上混用 Qt 逻辑像素。
        普通窗口和无法读取显示器信息的情况交给 Qt 的无边框计算处理。
        """
        if not self.user32.IsZoomed(self.hwnd):
            return
        monitor = self.user32.MonitorFromWindow(self.hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return
        client = wintypes.RECT.from_address(address)
        client.left = max(client.left, info.work.left)
        client.top = max(client.top, info.work.top)
        client.right = min(client.right, info.work.right)
        client.bottom = min(client.bottom, info.work.bottom)


def _create_windows_frame(hwnd: int) -> _WindowsFrame | None:
    """仅为真实 Windows 窗口创建系统合成适配；不可用时保留 Qt 基础外观。

    调用方必须在窗口已经取得原生句柄后调用。离屏平台产生的 winId 不是 HWND，
    即使宿主操作系统是 Windows 也不能传入系统库。系统装饰失败不应阻止应用启动。
    """
    if sys.platform != "win32" or QApplication.platformName() != "windows":
        return None
    try:
        frame = _WindowsFrame(hwnd)
        return frame if frame.enable() else None
    except (OSError, AttributeError):
        return None
