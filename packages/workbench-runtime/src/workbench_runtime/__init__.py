"""可独立安装的通用工作台；业务编辑器通过插件入口动态接入。"""

from .config import WorkbenchConfig
from .window import WorkbenchRuntimeWindow

__all__ = ["WorkbenchConfig", "WorkbenchRuntimeWindow"]
