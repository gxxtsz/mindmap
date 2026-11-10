"""思维导图项目桌面外壳的稳定公开入口。"""

from .cli import run_desktop
from .specs import ActionSpec, AppSpec
from .window import WorkbenchWindow

__version__ = "0.1.0"
__all__ = ["AppSpec", "ActionSpec", "WorkbenchWindow", "run_desktop"]
