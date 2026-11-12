"""软件结构可视化的轻量入口，业务模块在插件启用时加载。"""

from workbench_api import Disposable, PluginContext, PluginManifest


def activate(context: PluginContext) -> Disposable:
    """通过宿主公开接口登记软件结构文档，返回幂等的注销句柄。

    同一份文件由一个文档提供者加载，模块依赖图使用文档中的共享数据。
    宿主管理注册项和标签的生命周期，插件无需访问工作台窗口的实现。
    """
    from .viewer import register_document_provider

    return register_document_provider(context)


manifest = PluginManifest("software-structure", "软件结构可视化", "0.1.0", activate)
