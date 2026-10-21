"""纯状态模型：稳定标识、位置、显示模式与可序列化布局。"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Callable

from PySide6.QtGui import QAction

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget


class DockPosition(StrEnum):
    """六个标准工具窗口位置；位置与侧栏按钮所在的物理边分开管理。"""

    LEFT_TOP = "left_top"
    LEFT_BOTTOM = "left_bottom"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    RIGHT_TOP = "right_top"
    RIGHT_BOTTOM = "right_bottom"


POSITION_LABELS = {
    DockPosition.LEFT_TOP: "左上",
    DockPosition.LEFT_BOTTOM: "左下",
    DockPosition.BOTTOM_LEFT: "底部左",
    DockPosition.BOTTOM_RIGHT: "底部右",
    DockPosition.RIGHT_TOP: "右上",
    DockPosition.RIGHT_BOTTOM: "右下",
}


class ViewMode(StrEnum):
    """模式不包含可见性：隐藏后必须仍记住原模式及返回位置。"""

    DOCK_PINNED = "dock_pinned"
    DOCK_UNPINNED = "dock_unpinned"
    UNDOCK = "undock"
    FLOAT = "float"
    WINDOW = "window"


MODE_LABELS = {
    ViewMode.DOCK_PINNED: "停靠固定",
    ViewMode.DOCK_UNPINNED: "停靠取消固定",
    ViewMode.UNDOCK: "取消停靠",
    ViewMode.FLOAT: "浮动",
    ViewMode.WINDOW: "窗口",
}


@dataclass(frozen=True)
class DockTarget:
    """移动目标；editor 表示编辑器标签区，index=-1 表示追加到末尾。

    host_id 必须属于同一个 DockManager；拖放时只在确认合法目标后提交状态，
    避免取消拖动造成面板从原容器消失。
    """

    position: DockPosition | str = DockPosition.LEFT_TOP
    host_id: str = "main"
    index: int = -1
    group_id: str = ""

    def __post_init__(self):
        if self.position != "editor":
            object.__setattr__(self, "position", DockPosition(self.position))


@dataclass
class ToolWindowSpec:
    """工具窗口注册接口。

    factory 延迟创建实际 QWidget，整个注册生命周期只调用一次。
    id 用于布局持久化，应独立于显示标题且保持稳定；图标为语义名称或 QIcon。
    header_actions 按顺序在标题栏展示，菜单动作单击即弹出；动作由调用方持有，
    必须在工具窗口注销前保持有效。框架不复制、不持久化动作，也不改变其父对象。
    """

    id: str
    title: str
    factory: Callable[[], "QWidget"]
    icon: str = "panel"
    position: DockPosition = DockPosition.LEFT_TOP
    shortcut: str = ""
    mode: ViewMode = ViewMode.DOCK_PINNED
    available: bool = True
    notification: bool = False
    header_actions: tuple[QAction, ...] = ()

    def __post_init__(self):
        if not self.id or not self.title or self.id.startswith("tool:"):
            raise ValueError("工具窗口的标识和标题不能为空")
        self.header_actions = tuple(self.header_actions)
        if any(not isinstance(action, QAction) for action in self.header_actions):
            raise TypeError("工具窗口标题栏操作必须全部为 QAction")
        self.position = DockPosition(self.position)
        self.mode = ViewMode(self.mode)


@dataclass
class DocumentSpec:
    """中央工作区文档接口，内容可以是表格、画布、表单或任何 QWidget。

    factory 在注册时创建一次；can_close 在用户请求关闭或退出前调用，返回 False
    可以取消操作。modified=True 且未提供关闭决策时默认阻止关闭，保护业务数据。
    on_closed 在页面从标签区移除后调用；普通关闭保留实例，dispose=True 才销毁。
    """

    id: str
    title: str
    factory: Callable[[], "QWidget"]
    icon: str = "document"
    can_close: Callable[[], bool] | None = None
    on_closed: Callable[[], None] | None = None
    modified: bool = False

    def __post_init__(self):
        if not self.id or self.id.startswith("tool:") or not self.title:
            raise ValueError("文档标识和标题不能为空，且标识不能使用保留前缀 tool:")


@dataclass
class ToolState:
    """布局状态不持有 QWidget；控件归属由管理器在 Qt 主线程内统一变更。"""

    id: str
    position: str
    host_id: str = "main"
    mode: str = ViewMode.DOCK_PINNED
    visible: bool = False
    available: bool = True
    order: int = 0
    geometry: list[int] = field(default_factory=lambda: [180, 160, 620, 420])
    last_position: str = DockPosition.LEFT_TOP
    grouped_tabs: bool = False
    editor_group: str = ""


@dataclass
class LayoutSettings:
    """全局外观和布局选项；每个独立编辑器框架使用相同设置。"""

    show_bars: bool = True
    compact: bool = False
    widescreen: bool = False
    left_side_by_side: bool = False
    right_side_by_side: bool = False
    always_header_icons: bool = False


def default_region_sizes() -> dict[str, dict[str, int]]:
    """返回一个宿主的初始区域尺寸，单位为 Qt 逻辑像素。

    每次调用均创建独立字典，停靠占位区与覆盖工具窗互不共享可变对象。
    尺寸属于宿主而非工具；工具切换、迁移及隐藏都不能重置目标区域的尺寸。
    """
    return {"dock": {"left": 285, "right": 285, "bottom": 260},
            "overlay": {"left": 300, "right": 300, "bottom": 300}}
