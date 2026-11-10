"""文本插件的轻量清单；仅激活时导入 Qt 编辑器实现。"""

from workbench_api import PluginManifest


def activate(context):
    from .editor import TextPlugin
    return TextPlugin(context)


manifest = PluginManifest("text", "文本编辑", "0.1.0", activate)
