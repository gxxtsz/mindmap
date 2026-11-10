"""Qt 界面扩展声明；业务组件只接触公开桥，不访问宿主窗口实现。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QWidget

from .contracts import Disposable


@dataclass(frozen=True)
class CommandSpec:
    """命令可加入菜单或快捷键；enabled 每次活动上下文变化时重新求值。"""

    id: str
    title: str
    callback: Callable
    shortcut: str = ""
    icon: str = ""
    menu: str = ""
    enabled: Callable[[], bool] = lambda: True


@dataclass(frozen=True)
class PanelSpec:
    """插件拥有内容控件，宿主管理停靠容器；稳定标识同时用于布局持久化。"""

    id: str
    title: str
    factory: Callable[[], QWidget]
    icon: str = "panel"
    position: str = "left_top"
    shortcut: str = ""
    header_actions: tuple[QAction, ...] = ()
    default_visible: bool = False


@dataclass(frozen=True)
class MenuContribution:
    """菜单打开时填充，按当前文档提供动作；构造时不保留其他视图的绑定。"""

    menu: str
    populate: Callable[[QMenu], None]


@dataclass(frozen=True)
class StatusItemSpec:
    """状态栏扩展；visible 可根据活动编辑器类型隐藏插件专属控件。"""

    id: str
    widget: QWidget
    visible: Callable[[], bool] = lambda: True


class CommandService(Protocol):
    def register(self, spec: CommandSpec) -> QAction:
        """注册命令并由插件上下文拥有；返回动作供插件设置勾选状态或按钮复用。"""
        ...


class UiService(Protocol):
    """插件使用的 Qt 服务协议；回调订阅随插件回滚和退出自动断开。"""

    @property
    def parent(self) -> QWidget:
        """对话框父对象，不允许通过它读取宿主内部状态。"""
        ...

    @property
    def docking(self):
        """现有停靠组件的公开接口，可用于面板暂停、恢复、定位及分屏。"""
        ...

    @property
    def restoring(self) -> bool: ...

    def register_panel(self, spec: PanelSpec) -> QAction | None: ...

    def register_status(self, spec: StatusItemSpec) -> Disposable: ...

    def contribute_menu(self, contribution: MenuContribution) -> Disposable: ...

    def subscribe_active(self, callback: Callable) -> Disposable: ...

    def subscribe_theme(self, callback: Callable) -> Disposable: ...

    def subscribe_layout(self, callback: Callable) -> Disposable: ...

    def show_message(self, message: str) -> None: ...

    def refresh(self) -> None: ...

    def action(self, identifier: str) -> QAction: ...

    def open_directory(self, view_id: str | None = None) -> bool: ...

    def remember_path(self, path) -> None: ...

    def current_layout(self) -> dict: ...
