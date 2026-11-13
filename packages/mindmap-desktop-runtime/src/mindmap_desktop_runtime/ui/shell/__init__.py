"""导图桌面的窗口外壳、应用声明和桌面启动流程。"""

from .cli import run_desktop
from .specs import ActionSpec, AppSpec
from .window import DesktopWindow

__all__ = ["AppSpec", "ActionSpec", "DesktopWindow", "run_desktop"]
