"""文件附件的名称与文档内资源引用。"""

import ntpath
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

from .errors import MindMapError

BINARY_MEDIA_TYPE = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class FileAttachment:
    """保留文件名以供显示和打开，内容由 SHA-256 标识定位。"""

    name: str
    asset_id: str

    def __post_init__(self):
        if (not isinstance(self.name, str) or not self.name or self.name.endswith((".", " "))
                or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in self.name)):
            raise MindMapError("附件名称必须是有效的文件名")
        try:
            self.name.encode("utf-8")
        except UnicodeError as error:
            raise MindMapError("附件名称包含无效的 Unicode 字符") from error
        reserved = ntpath.isreserved(self.name) if hasattr(ntpath, "isreserved") else (
            PureWindowsPath(self.name).is_reserved())
        if reserved:
            raise MindMapError("附件名称使用了系统保留名称")
        if not isinstance(self.asset_id, str) or not re.fullmatch(r"[a-f0-9]{64}", self.asset_id):
            raise MindMapError("附件资源标识无效")

    @classmethod
    def from_data(cls, data: object) -> "FileAttachment":
        """读取节点记录中的附件并校验字段。"""
        if not isinstance(data, dict) or set(data) != {"name", "asset_id"}:
            raise MindMapError("附件记录必须包含文件名和资源标识")
        return cls(data["name"], data["asset_id"])
