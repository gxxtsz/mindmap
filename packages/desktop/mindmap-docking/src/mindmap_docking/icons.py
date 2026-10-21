"""本项目原创矢量图标注册表；业务可注入自己的 QIcon 或 SVG 文件。"""

import hashlib
import tempfile
from pathlib import Path
from typing import Protocol

from PySide6.QtGui import QIcon
from PySide6.QtSvg import QSvgRenderer


class IconProvider(Protocol):
    """图标供应接口。颜色为当前主题的十六进制前景色，size 为逻辑像素。"""

    def get_icon(self, name: str, *, color: str, size: int) -> QIcon:
        """返回图标；找不到时返回空 QIcon，由调用方采用文字或通用图标。"""
        ...


_SVG_CACHE_DIRECTORY = None


def _svg_cache_file(data: bytes) -> Path:
    """把着色结果交给 Qt 原生 SVG 引擎，保持多 DPI 矢量重绘及可靠的销毁次序。

    缓存由进程持有到退出，避免图标比供应器活得更久时文件过早消失。文件名为
    内容摘要，源资源不被改写；TemporaryDirectory 在进程退出时清理本次临时目录。
    不在 Python 中继承 QIconEngine，避免 Qt 图标缓存销毁跨越 Python 对象生命周期。
    """
    global _SVG_CACHE_DIRECTORY
    if _SVG_CACHE_DIRECTORY is None:
        _SVG_CACHE_DIRECTORY = tempfile.TemporaryDirectory(prefix="mindmap-svg-")
    path = Path(_SVG_CACHE_DIRECTORY.name) / (hashlib.sha256(data).hexdigest() + ".svg")
    if not path.exists():
        path.write_bytes(data)
    return path


class IconRegistry:
    """默认提供随包原创 SVG；register 接受可信业务代码给出的路径或 QIcon。

    名称查找不会拼接任意相对路径，避免布局或标签文本意外成为文件读取入口。
    业务资源与内置图标同名时优先使用业务版本，不会修改磁盘上的原始文件。
    """

    def __init__(self):
        root = Path(__file__).parent / "resources" / "icons"
        self._sources = {path.stem: path for path in root.glob("*.svg")}
        self._cache = {}

    def register(self, name: str, source: str | Path | QIcon) -> None:
        """注册一个图标；SVG 在注册时检查格式，错误不会污染现有图标映射。"""
        if not name:
            raise ValueError("图标名称不能为空")
        if not isinstance(source, QIcon):
            source = Path(source)
            if not source.is_file() or not QSvgRenderer(str(source)).isValid():
                raise ValueError("图标必须是可解析的 SVG 文件或 QIcon")
        self._sources[name] = source
        self._cache.clear()

    def get_icon(self, name: str, *, color: str, size: int = 20) -> QIcon:
        source = self._sources.get(name)
        if isinstance(source, QIcon):
            return QIcon(source)
        if source is None:
            return QIcon()
        key = (name, color)
        if key not in self._cache:
            # 内置 SVG 使用标准 currentColor；替换只发生在内存，许可和源字节保持不变。
            data = source.read_bytes().replace(b"currentColor", color.encode("ascii"))
            self._cache[key] = QIcon(str(_svg_cache_file(data)))
        return QIcon(self._cache[key])
