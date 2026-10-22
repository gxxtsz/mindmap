"""工作台的最近文件与退出会话记录；文件内容、停靠布局分别由原组件持有。"""

from __future__ import annotations

import os
import re
from pathlib import Path

from mindmap_docking import JsonLayoutStore

RECENT_FILE_LIMIT = 20


def path_key(path: str | Path) -> str:
    """比较真实绝对路径；Windows 忽略大小写，解析软链接后避免同一文件重复编辑。"""
    return os.path.normcase(str(Path(path).resolve()))


def _document_path(value: object) -> str:
    """只接受原生导图的绝对路径，不要求文件当前存在，允许保留暂时不可用的历史。"""
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("导图路径必须是非空字符串")
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != ".mindmap":
        raise ValueError("导图记录必须使用 .mindmap 文件的绝对路径")
    return str(path.resolve())


class DocumentState:
    """原子保存最近文件和最近一次正常退出的文件集合。

    最近文件每次更新立即落盘，但始终携带上一次成功提交的退出快照，不能把
    启动恢复期间的半成品写成新会话。新退出快照由窗口通过关闭检查后显式提交。
    I/O 或格式错误交给窗口显示中文提示，文件加载不依赖此记录成功保存。
    """

    def __init__(self, path: str | Path):
        self.store = JsonLayoutStore(path)
        self.recent_files: list[str] = []
        self.open_documents: list[dict[str, str]] = []
        self.active_id = ""

    def load(self) -> None:
        """先完整校验再替换内存状态；不存在的配置表示首次使用，不产生错误。"""
        payload = self.store.load()
        if payload is None:
            return
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise ValueError("不支持的导图记录版本")
        recent, documents = payload.get("recent_files"), payload.get("open_documents")
        active = payload.get("active_id", "")
        if not isinstance(recent, list) or not isinstance(documents, list) or not isinstance(active, str):
            raise ValueError("最近文件或退出会话格式无效")
        normalized_recent, normalized_documents = [], []
        recent_keys, document_keys, identifiers = set(), set(), set()
        for value in recent:
            path = _document_path(value)
            key = path_key(path)
            if key not in recent_keys:
                recent_keys.add(key)
                normalized_recent.append(path)
        for item in documents:
            if not isinstance(item, dict):
                raise ValueError("退出会话条目必须是对象")
            identifier = item.get("id")
            if not isinstance(identifier, str) or re.fullmatch(r"mindmap\.[0-9a-f]{32}", identifier) is None:
                raise ValueError("退出会话标识无效")
            path = _document_path(item.get("path"))
            key = path_key(path)
            if identifier in identifiers or key in document_keys:
                raise ValueError("退出会话包含重复的文件或标识")
            identifiers.add(identifier)
            document_keys.add(key)
            normalized_documents.append({"id": identifier, "path": path})
        self.recent_files = normalized_recent[:RECENT_FILE_LIMIT]
        self.open_documents = normalized_documents
        self.active_id = active if active in identifiers else ""

    def _save(self) -> None:
        """复用现有 JSON 存储的同目录临时文件与原子替换，写入失败保留旧文件。"""
        self.store.save({
            "schema_version": 1,
            "recent_files": self.recent_files,
            "open_documents": self.open_documents,
            "active_id": self.active_id,
        })

    def remember(self, path: str | Path) -> None:
        """成功新建、打开或另存为后置顶；普通激活、自动保存及恢复不调用此接口。"""
        value = _document_path(str(Path(path).resolve()))
        key = path_key(value)
        self.recent_files = [value, *(item for item in self.recent_files if path_key(item) != key)][
            :RECENT_FILE_LIMIT
        ]
        self._save()

    def clear_recent(self) -> None:
        """只清空历史列表，保留退出快照及当前运行中的导图。"""
        self.recent_files = []
        self._save()

    def save_session(self, documents: list[dict[str, str]], active_id: str) -> None:
        """提交已通过关闭检查的退出快照，列表顺序来自实际分屏树中的标签顺序。"""
        self.open_documents = [dict(item) for item in documents]
        self.active_id = active_id if any(item["id"] == active_id for item in documents) else ""
        self._save()
