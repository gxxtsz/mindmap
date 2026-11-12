"""Qt 界面扩展声明；业务组件只接触公开桥，不访问宿主窗口实现。"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from PySide6.QtGui import QAction, QColor, QIcon
from PySide6.QtWidgets import QMenu, QWidget

from .contracts import Disposable


class MenuLocation(StrEnum):
    """主菜单贡献位置。标识与显示文字无关，分区标题和顺序由宿主决定。

    FILE_EXPORT 对应「文件」下的「导出当前页」子菜单。其他位置接收所属菜单的扩展动作，
    例如 EDIT 的扩展动作排在宿主的标准编辑命令之后。
    """

    FILE = "file"
    FILE_EXPORT = "file.export"
    EDIT = "edit"
    VIEW = "view"
    SETTINGS = "settings"


@dataclass(frozen=True)
class CommandSpec:
    """声明可复用的命令。menu 使用 MenuLocation，空字符串表示无需菜单入口。

    enabled 在活动上下文变化时重新求值，工作台将结果同步到菜单和快捷键共用的动作。
    """

    id: str
    title: str
    callback: Callable
    shortcut: str = ""
    icon: str = ""
    menu: MenuLocation | str = ""
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
    """菜单打开时按当前文档填充。menu 使用 MenuLocation 指定宿主分区。

    populate 仅收到本次贡献的临时菜单，成功后宿主才合入其中的动作。临时子菜单使用
    menu.addMenu(标题) 创建，随主菜单释放。复用的文档动作仍由所属视图管理。
    """

    menu: MenuLocation | str
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
    def compact(self) -> bool:
        """当前主题的紧凑模式，供插件调整控件尺寸和间距。"""
        ...

    @property
    def restoring(self) -> bool: ...

    def register_panel(self, spec: PanelSpec) -> QAction | None:
        """登记面板并绑定插件生命周期。有快捷键时返回可复用的面板动作。"""
        ...

    def panel_visible(self, panel_id: str) -> bool:
        """读取面板在布局中的显隐偏好。暂停期间保留该值，未知标识抛出 KeyError。"""
        ...

    def activate_panel(self, panel_id: str, *, focus: bool = True) -> bool:
        """显示并激活面板，成功返回 True。focus=False 时保留当前输入焦点。

        面板暂停或不可用时返回 False，未知标识抛出 KeyError。
        """
        ...

    def set_panels_suspended(self, panel_ids: Iterable[str], suspended: bool) -> None:
        """批量暂停或恢复面板，保留显隐、位置和尺寸等布局偏好。

        暂停期间面板暂时隐藏且动作禁用。全部标识通过校验后才修改，未知标识抛出 KeyError。
        在活动视图订阅回调内调用时，面板的布局在本轮通知结束后统一刷新。
        """
        ...

    def theme_color(self, token: str) -> QColor:
        """根据当前主题解析颜色令牌。切换主题后可在主题订阅回调中重新查询。"""
        ...

    def theme_icon(self, name: str) -> QIcon:
        """按图标标识返回当前主题的 Qt 图标，供插件菜单和控件复用。"""
        ...

    def register_status(self, spec: StatusItemSpec) -> Disposable: ...

    def contribute_menu(self, contribution: MenuContribution) -> Disposable:
        """登记动态菜单贡献并返回注销句柄。未知位置在注册时抛出 ValueError。"""
        ...

    def subscribe_active(self, callback: Callable) -> Disposable: ...

    def subscribe_theme(self, callback: Callable) -> Disposable: ...

    def subscribe_layout(self, callback: Callable) -> Disposable: ...

    def show_message(self, message: str) -> None: ...

    def refresh(self) -> None: ...

    def action(self, identifier: str) -> QAction: ...

    def open_directory(self, view_id: str | None = None) -> bool: ...

    def remember_path(self, path) -> None: ...

    def current_layout(self) -> dict: ...
