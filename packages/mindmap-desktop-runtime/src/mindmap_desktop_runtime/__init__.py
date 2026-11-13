"""导图桌面基础设施；产品工厂直接注入唯一的导图应用。"""

from .config import DesktopConfig
from .ui.shell import run_desktop
from .window import DesktopRuntimeWindow

__all__ = ["DesktopConfig", "DesktopRuntimeWindow", "run_desktop"]
