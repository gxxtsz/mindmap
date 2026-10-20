"""可嵌入任意 Qt Widgets 程序的原生停靠系统公共接口。"""

from .host import DockHost
from .icons import IconProvider, IconRegistry
from .manager import DockManager
from .models import DockPosition, DockTarget, DocumentSpec, ToolWindowSpec, ViewMode
from .storage import JsonLayoutStore, LayoutStore
from .theme import Theme, ThemeTokens

__version__ = "0.1.0"
__all__ = ["DockHost", "DockManager", "DockPosition", "DockTarget", "ToolWindowSpec", "ViewMode",
           "DocumentSpec", "Theme", "ThemeTokens", "IconProvider", "IconRegistry", "JsonLayoutStore", "LayoutStore"]
