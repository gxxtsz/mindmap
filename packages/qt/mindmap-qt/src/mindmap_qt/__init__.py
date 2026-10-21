"""PySide6 思维导图组件；QApplication 和应用偏好由宿主负责。"""

from .files import MindMapFileController
from .panels import MindMapPanels
from .ui_style import UiColors
from .widget import MindMapWidget
from .workspace import MindMapWorkspace

__all__ = ["MindMapWidget", "MindMapWorkspace", "MindMapPanels", "MindMapFileController", "UiColors"]
