"""可替换的布局存储边界；默认实现使用版本化 JSON 和原子文件替换。"""

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol


class LayoutStore(Protocol):
    """存储只传递 JSON 数据；内容校验和布局提交仍由 DockManager 负责。"""

    def load(self) -> dict | None:
        """无配置返回 None，读取失败或损坏时抛出 OSError/ValueError。"""
        ...

    def save(self, payload: dict) -> None:
        """提交完整快照；实现必须保证失败时旧数据仍可读。"""
        ...


class JsonLayoutStore:
    """应用独享路径的 JSON 存储；不使用 pickle，不加载模块，不执行配置内容。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        if self.path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("布局文件超过 8 MiB 限制")
        payload = json.loads(self.path.read_text("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("布局文件根节点必须是对象")
        return payload

    def save(self, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # 临时文件必须与目标处于同一目录，os.replace 才能在同一文件系统中原子提交。
            # Windows 下先关闭临时句柄再替换，避免共享访问规则造成 PermissionError。
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".layout-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
