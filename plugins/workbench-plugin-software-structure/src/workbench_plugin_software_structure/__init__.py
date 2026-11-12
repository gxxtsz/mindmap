"""软件结构可视化的轻量入口，业务模块在插件启用时加载。"""

from workbench_api import Disposable, PluginContext, PluginManifest


def activate(context: PluginContext) -> Disposable:
    """通过宿主公开接口登记模板下载、文档提供者、状态栏及两种侧栏。

    下载命令由插件上下文托管，文件菜单和各软件结构视图共用该动作。
    状态栏按活动视图切换画布，返回的注销句柄在移除控件前断开订阅并释放画布引用。
    """
    from workbench_api.qt import CommandSpec, MenuLocation, StatusItemSpec

    from .dependency_filter_panel import register_dependency_filter_panel
    from .module_api_panel import register_module_api_panel
    from .status_controls import SoftwareStructureStatusControls
    from .template import DOWNLOAD_TEMPLATE_COMMAND_ID, download_template
    from .viewer import SoftwareStructureView, register_document_provider

    download = context.commands.register(CommandSpec(
        id=DOWNLOAD_TEMPLATE_COMMAND_ID,
        title="下载软件结构模板…",
        callback=lambda: download_template(context.ui),
        icon="export-text",
        menu=MenuLocation.FILE,
    ))
    download.setToolTip("下载软件结构模板（含 AI 生成说明）")
    provider = register_document_provider(context)
    controls = SoftwareStructureStatusControls(context.ui)
    registrations = (
        provider,
        register_dependency_filter_panel(context),
        register_module_api_panel(context),
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
