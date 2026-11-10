"""思维导图插件轻量入口；禁用时不会导入引擎、画布或创建字体资源。"""

from workbench_api import PluginManifest


def activate(context):
    from .controller import MindMapController
    return MindMapController(context)


manifest = PluginManifest("mindmap", "思维导图", "0.1.0", activate)
