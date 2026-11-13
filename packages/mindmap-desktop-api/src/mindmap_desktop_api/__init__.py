"""导图应用与桌面基础设施的内部协议；节点扩展使用独立的页面协议。"""

from .contracts import (
    ApplicationContext,
    Disposable,
    DocumentHandle,
    EditorView,
    MindMapApplication,
    OperationResult,
    OperationStatus,
    SavePolicy,
    path_key,
)

__all__ = [
    "ApplicationContext", "Disposable", "DocumentHandle", "EditorView", "MindMapApplication",
    "OperationResult", "OperationStatus", "SavePolicy", "path_key",
]
