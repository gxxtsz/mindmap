"""通过公开宿主协议连接的大纲、属性与设置面板；图形接口由宿主在 Qt GUI 线程调用。"""

from .contracts import PanelHost
from .favorites import SubmapFavoritesPanel
from .inspector import NodePropertiesPanel
from .inspiration import InspirationPanel
from .outline import OutlineModel, OutlinePanel
from .panels import MindMapPanels
from .settings import GlobalSettingsPanel

__all__ = ["PanelHost", "NodePropertiesPanel", "OutlineModel", "OutlinePanel", "MindMapPanels",
           "GlobalSettingsPanel", "SubmapFavoritesPanel", "InspirationPanel"]
