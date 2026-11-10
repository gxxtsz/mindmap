"""可独立安装的通用工作台；业务编辑器通过插件入口动态接入。"""

from .config import WorkbenchConfig
from .ui.shell import run_desktop
from .window import WorkbenchRuntimeWindow

__all__ = ["WorkbenchConfig", "WorkbenchRuntimeWindow", "run_desktop"]
