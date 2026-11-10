"""与具体编辑器无关的插件、文档、视图及操作结果协议。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .qt import CommandService, UiService

API_VERSION = 2


def path_key(path: str | Path) -> str:
    """将文件路径转为资源比较键；解析链接，并遵守系统的大小写规则。"""
    return os.path.normcase(str(Path(path).resolve()))


class OperationStatus(StrEnum):
    """取消不属于失败；调用方据此决定是否显示错误和中止批量操作。"""

    SUCCESS = "success"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class OperationResult:
    """打开、保存和复制的显式结果；布尔转换仅表示成功，错误保留中文说明。"""

    status: OperationStatus = OperationStatus.SUCCESS
    message: str = ""
    view_id: str = ""
    error: Exception | None = None
    reported: bool = False

    def __bool__(self) -> bool:
        return self.status == OperationStatus.SUCCESS

    @classmethod
    def cancelled(cls, message=""):
        return cls(OperationStatus.CANCELLED, message)

    @classmethod
    def failed(cls, message, *, reported=False):
        """保留原始异常供调用方处理；reported 表示插件已写入带业务来源的日志。"""
        return cls(OperationStatus.FAILED, str(message), error=message if isinstance(message, Exception) else None,
                   reported=reported)


class SavePolicy(StrEnum):
    """平台按策略组织关闭；自动保存的具体时机和快照由插件管理。"""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


class Disposable:
    """幂等注销句柄；先清空回调再执行，避免释放过程中的信号引起重入。"""

    def __init__(self, callback: Callable[[], None] = lambda: None):
        self._callback = callback

    def dispose(self) -> None:
        callback, self._callback = self._callback, lambda: None
        callback()


class DocumentHandle(Protocol):
    """一份共享文档；路径只有保存成功后才能改绑，视图不能拥有它的 Qt 生命周期。"""

    id: str
    save_policy: SavePolicy

    @property
    def path(self) -> Path | None: ...

    @property
    def is_dirty(self) -> bool: ...

    @property
    def resource_uri(self) -> str:
        """磁盘文档使用 file URI；未保存文档使用稳定的 untitled URI。"""
        ...

    def save(self, *, save_as: bool = False, reason: str = "explicit") -> OperationResult:
        """提交所有关联视图并保存。reason 为 explicit、batch 或 close，批量及关闭不重复记录成功日志。

        失败不得清除修改状态；reason 只影响展示，不改变保存及输入检查的正确性。
        """
        ...


class EditorView(Protocol):
    """独立标签协议，widget 的实际 QWidget 类型在 Qt 注册边界校验。

    edit_actions 提供视图支持的标准编辑动作，以 undo、redo、cut、copy、paste、
    select_all 为键。宿主负责菜单摆放和快捷键路由，插件可按视图能力省略相应的键。
    """

    id: str
    document: DocumentHandle
    widget: Any
    edit_actions: dict[str, Any]

    @property
    def title(self) -> str: ...

    def prepare_input(self) -> bool:
        """提交完整输入；输入法候选仍在进行时返回 False，不强制结束组合。"""
        ...

    def capture_state(self) -> dict: ...

    def restore_state(self, state: dict) -> None:
        """恢复支持的视图版本；未知版本回退默认值，不改变共享文档内容。"""
        ...

    def duplicate(self) -> OperationResult: ...

    def dispose(self) -> None: ...


@dataclass(frozen=True)
class EditorProvider:
    """编辑器提供者通过 context.documents.register_view 提交成功创建的视图。

    open_document(path, view_id, state) 用于普通打开和恢复；view_id 为 None 表示普通打开。
    创建失败或取消时不得注册半初始化视图。扩展名包含小数点并使用小写。
    """

    id: str
    title: str
    extensions: tuple[str, ...]
    new_document: Callable[[Path | None], OperationResult]
    open_document: Callable[[Path, str | None, dict | None], OperationResult]
    icon: str = "document"


@dataclass(frozen=True)
class PluginManifest:
    """可独立读取的插件清单；activate 才导入业务实现，禁用插件不会创建业务对象。"""

    id: str
    name: str
    version: str
    activate: Callable[[PluginContext], Plugin]
    api_version: int = API_VERSION
    requires: tuple[str, ...] = ()


class Plugin(Protocol):
    def dispose(self) -> None:
        """插件结束时停止私有后台任务；平台随后逆序清理注册项和订阅。"""
        ...


class EditorRegistryService(Protocol):
    def register(self, provider: EditorProvider) -> Disposable:
        """注册提供者并返回注销句柄，标识及扩展名冲突应在注册前拒绝。"""
        ...


class DocumentService(Protocol):
    """插件可调用的文档服务；注册项自动绑定调用插件的生命周期。"""

    @property
    def active(self) -> EditorView | None: ...

    def ordered(self) -> list[EditorView]: ...

    def find_path(self, path: str | Path) -> EditorView | None: ...

    def activate_view(self, view_id: str) -> None:
        """激活已登记的视图及其所在分屏，复用标签和控件，并更新编辑焦点。

        活动视图发生变化时沿用宿主的通知。标识对应的视图尚未打开时抛出 ValueError。
        """
        ...

    def register_view(self, provider_id: str, view: EditorView, *, after: str | None = None,
                      remember: bool = True) -> Disposable: ...

    def sync(self, view: EditorView) -> None: ...

    def remove_view(self, view_id: str) -> None: ...


class SettingsService(Protocol):
    def value(self, key: str, default: Any = None) -> Any: ...

    def setValue(self, key: str, value: Any) -> None: ...

    def sync(self) -> None: ...


class StateService(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...

    def flush(self) -> None: ...


class LogService(Protocol):
    """结构化日志服务；业务解释归插件，平台仅持有文本并负责展示。"""

    def __call__(self, message: str, *, level: str = "info", source: str = "应用",
                 source_path: str = "", title: str | None = None, detail: str = "") -> None:
        """message 用于完整复制；title/detail 为可选展示摘要，不影响错误级别。"""
        ...


@dataclass
class PluginContext:
    """按插件隔离的服务集合，不提供运行时主窗口的私有属性。

    ui 是公开的 Qt 桥，documents 是文档服务，settings/state 是隔离存储。
    own 将信号断开、定时器停止等清理动作纳入激活回滚及正常关闭流程。
    """

    plugin_id: str
    editors: EditorRegistryService
    commands: CommandService
    ui: UiService
    documents: DocumentService
    settings: SettingsService
    state: StateService
    log: LogService
    options: dict = field(default_factory=dict)
    _owned: list[Disposable] = field(default_factory=list)

    def own(self, resource: Disposable | Callable[[], None]) -> Disposable:
        item = resource if isinstance(resource, Disposable) else Disposable(resource)
        self._owned.append(item)
        return item

    def dispose(self) -> None:
        """逆序清理全部资源；单个清理异常不阻止其他订阅释放。"""
        for item in reversed(self._owned):
            try:
                item.dispose()
            except Exception as error:
                self.log(f"插件资源释放失败：{error}", level="error")
        self._owned.clear()
