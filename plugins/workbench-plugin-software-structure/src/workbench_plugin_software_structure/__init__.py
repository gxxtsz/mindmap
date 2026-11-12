"""软件结构可视化的轻量入口，业务模块在插件启用时加载。"""

from workbench_api import Disposable, PluginContext, PluginManifest


def activate(context: PluginContext) -> Disposable:
    """通过宿主公开接口登记文档提供者和窗口状态栏控件。

    状态栏按活动视图切换画布。注册项由插件上下文接管，以便激活失败时回滚，
    返回的注销句柄会在移除控件之前断开订阅并释放画布引用。
    """
    from workbench_api.qt import StatusItemSpec

    from .status_controls import SoftwareStructureStatusControls
    from .viewer import SoftwareStructureView, register_document_provider

    provider = register_document_provider(context)
    controls = SoftwareStructureStatusControls(context.ui)
    registrations = (
        provider,
        context.ui.register_status(StatusItemSpec(
            "software-structure.view", controls,
            lambda: isinstance(context.documents.active, SoftwareStructureView),
        )),
        context.own(lambda: controls.bind_view(None)),
        context.ui.subscribe_active(controls.bind_view),
        context.ui.subscribe_theme(controls.apply_theme),
    )
    controls.apply_theme()
    controls.bind_view(context.documents.active)

    def dispose() -> None:
        for registration in reversed(registrations):
            registration.dispose()

    return Disposable(dispose)


manifest = PluginManifest("software-structure", "软件结构可视化", "0.1.0", activate)
