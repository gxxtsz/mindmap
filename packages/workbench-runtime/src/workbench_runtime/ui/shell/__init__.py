"""工作台的窗口外壳、应用声明和桌面启动流程。"""

from .cli import run_desktop
from .specs import ActionSpec, AppSpec
from .window import WorkbenchWindow

__all__ = ["AppSpec", "ActionSpec", "WorkbenchWindow", "run_desktop"]
