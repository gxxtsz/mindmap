"""业务应用的公开配置与动作声明，不包含任何特定行业逻辑。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mindmap_docking import Theme


@dataclass(frozen=True)
class ActionSpec:
    """一个菜单、工具栏和快捷键共享的业务动作。

    id 在应用内保持唯一，不能覆盖停靠系统动作；callback 为无参业务回调。
    toolbar=True 时在标题工具栏显示，menu 为业务自定义的顶级菜单名称。
    """

    id: str
    title: str
    callback: Callable[[], None]
    icon: str = ""
    shortcut: str = ""
    menu: str = "文件"
    toolbar: bool = False
    checkable: bool = False


@dataclass(frozen=True)
class AppSpec:
    """工作台外壳配置；所有品牌、动作和默认外观均由业务应用声明。

    application_id 用于隔离持久化目录，title/subtitle 仅负责显示。
    native_title_bar=True 使用平台系统标题栏；默认集成标题栏支持 Windows 缩放。
    全局主题默认关闭，嵌入应用可以与独立工作台同时使用不同的局部主题。
    """

    application_id: str
    title: str
    subtitle: str = "工作空间"
    icon: str = "mindmap"
    version: str = "0.1.0"
    status_text: str = "就绪"
    theme: Theme = field(default_factory=Theme)
    storage_dir: str | Path | None = None
    actions: tuple[ActionSpec, ...] = ()
    native_title_bar: bool = False
    global_theme: bool = False

    def __post_init__(self):
        if not self.title or not self.application_id:
            raise ValueError("应用名称和应用标识不能为空")
        ids = [action.id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("应用动作标识重复")
