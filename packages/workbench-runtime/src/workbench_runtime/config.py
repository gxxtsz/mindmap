"""宿主产品配置；运行时只消费声明，不决定具体产品的名称和历史目录。"""

import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths


@dataclass(frozen=True)
class WorkbenchConfig:
    """构造宿主时传入的稳定产品标识与显示信息。

    application_id 参与布局归属和默认配置目录计算，不应随显示名称变化。
    title、icon 只控制界面展示。具有历史目录的产品应通过窗口的 storage_dir
    显式注入原路径；默认路径仅供没有历史数据的新宿主使用。
    """

    application_id: str = "workbench"
    title: str = "工作台"
    icon: str = "document"

    def __post_init__(self):
        """应用标识必须是单个目录名，防止默认位置跳出系统配置目录。"""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", self.application_id) or not self.title.strip():
            raise ValueError("应用标识必须是有效目录名，显示名称不能为空")

    def default_storage_dir(self) -> Path:
        """返回平台配置位置，不创建目录；实际写入仍由状态存储负责。"""
        root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation)
        return Path(root) / self.application_id
