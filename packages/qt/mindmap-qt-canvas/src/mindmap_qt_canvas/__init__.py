"""可独立嵌入的 Qt 思维导图交互画布与文件控制器；图形接口由宿主在 Qt GUI 线程调用。"""

from .files import MindMapFileController
from .widget import MindMapWidget

__all__ = ["MindMapWidget", "MindMapFileController"]
