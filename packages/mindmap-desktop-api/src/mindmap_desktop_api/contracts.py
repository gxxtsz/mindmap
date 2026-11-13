"""导图主体与桌面基础设施之间的应用、文档、视图及操作结果协议。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .qt import CommandService, UiService

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
        """保留原始异常供调用方处理；reported 表示导图应用已写入带业务来源的日志。"""
        return cls(OperationStatus.FAILED, str(message), error=message if isinstance(message, Exception) else None,
                   reported=reported)


class SavePolicy(StrEnum):
    """声明文档的保存能力和关闭策略。

    自动保存的时机和快照由导图应用管理，关闭时由宿主协调最后一次保存。
    手动保存文档仅在最后一个关联标签关闭且有修改时提示。只读文档可以
    参与最近文件、标签复制和会话恢复，宿主会跳过其保存和关闭时的保存检查。
    """

    AUTOMATIC = "automatic"
    MANUAL = "manual"
    READ_ONLY = "read_only"


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
        宿主不会调用只读文档的保存方法。导图应用仍实现此接口，供直接调用时返回取消结果。
        """
        ...


class EditorView(Protocol):
    """独立标签协议，widget 的实际 QWidget 类型在 Qt 注册边界校验。

    edit_actions 提供视图支持的标准编辑动作，以 undo、redo、cut、copy、paste、
    select_all 为键。宿主负责菜单摆放和快捷键路由，导图应用可按视图能力省略相应的键。
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

    def capture_state(self) -> dict:
        """在 GUI 线程取得可序列化的视图快照，供运行期间和退出时保存。

        返回页面、光标或视口等可重建的视图状态。调用方也可读取隐藏标签的快照，
        读取期间保留未提交的编辑和视图所属文档。
        """
        ...

    def restore_state(self, state: dict) -> None:
        """恢复支持的视图版本；未知版本回退默认值，不改变共享文档内容。"""
        ...

    def duplicate(self) -> OperationResult: ...

    def dispose(self) -> None: ...


class MindMapApplication(Protocol):
    """桌面直接持有的唯一导图应用。由产品工厂构造，不提供编辑器类型注册。

    页面扩展只能通过 NodePageContext 操作所属节点，不能取得本接口。
    打开和新建成功后由应用登记完整视图；失败时不得留下半初始化标签。
    """

    def new_document(self, path: Path | None = None) -> OperationResult:
        """新建导图；路径为空时由应用发起保存位置选择。"""
        ...

    def open_document(self, path: Path, view_id: str | None = None,
                      state: dict | None = None) -> OperationResult:
        """打开或恢复导图，复用同一路径的共享文档，分别保留各标签的视图状态。"""
        ...

    def dispose(self) -> None:
        """停止导图的后台任务和定时器，窗口随后释放上下文所拥有的界面资源。"""
        ...


class DocumentService(Protocol):
    """导图应用可调用的文档服务；注册项自动绑定调用导图应用的生命周期。"""

    @property
    def active(self) -> EditorView | None: ...

    def ordered(self) -> list[EditorView]: ...

    def find_path(self, path: str | Path) -> EditorView | None: ...

    def activate_view(self, view_id: str) -> None:
        """激活已登记的视图及其所在分屏，复用标签和控件，并更新编辑焦点。

        活动视图发生变化时沿用宿主的通知。标识对应的视图尚未打开时抛出 ValueError。
        """
        ...

    def register_view(self, view: EditorView, *, after: str | None = None,
                      remember: bool = True) -> Disposable:
        """成功后把视图放入活动分屏。after 指向已有标签时，插在该标签之后。

        remember 控制是否加入最近文件。返回注销句柄，用于应用退出或创建失败时清理。
        """
        ...

    def sync(self, view: EditorView) -> None: ...

    def view_state_changed(self, view_id: str) -> None:
        """通知宿主视图的可恢复状态发生变化，例如切页、平移或缩放。

        仅对本导图应用已登记的视图生效，启动恢复期间延后处理。连续通知由宿主合并，
        到期时读取最新 capture_state，与标签和分屏布局一并保存。
        """
        ...

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
    """结构化日志服务；业务解释归导图应用，平台仅持有文本并负责展示。"""

    def __call__(self, message: str, *, level: str = "info", source: str = "应用",
                 source_path: str = "", title: str | None = None, detail: str = "") -> None:
        """message 用于完整复制；title/detail 为可选展示摘要，不影响错误级别。"""
        ...


@dataclass
class ApplicationContext:
    """导图应用独享的桌面服务集合，不提供运行时主窗口的私有属性。

    ui 是公开的 Qt 桥，documents 是文档服务，settings/state 保存导图偏好和本机状态。
    own 将信号断开、定时器停止等清理动作纳入初始化回滚及正常关闭流程。
    """

    commands: CommandService
    ui: UiService
    documents: DocumentService
    settings: SettingsService
    state: StateService
    log: LogService
    options: dict = field(default_factory=dict)
    _owned: list[Disposable] = field(default_factory=list)

    def own(self, resource: Disposable | Callable[[], None]) -> Disposable:
        """登记需要回收的资源或回调，返回可提前幂等释放的句柄。"""
        item = resource if isinstance(resource, Disposable) else Disposable(resource)
        self._owned.append(item)
        return item

    def dispose(self) -> None:
        """逆序清理全部资源；单个清理异常不阻止其他订阅释放。"""
        for item in reversed(self._owned):
            try:
                item.dispose()
            except Exception as error:
                self.log(f"导图应用资源释放失败：{error}", level="error")
        self._owned.clear()
