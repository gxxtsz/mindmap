"""工作台稳定扩展边界；导入核心协议不创建 Qt 应用或加载业务插件。"""

from .contracts import (
    API_VERSION,
    Disposable,
    DocumentHandle,
    EditorProvider,
    EditorView,
    OperationResult,
    OperationStatus,
    Plugin,
    PluginContext,
    PluginManifest,
    SavePolicy,
    path_key,
)

__all__ = [
    "API_VERSION", "Disposable", "DocumentHandle", "EditorProvider", "EditorView",
    "OperationResult", "OperationStatus", "Plugin", "PluginContext", "PluginManifest",
    "SavePolicy", "path_key",
]
