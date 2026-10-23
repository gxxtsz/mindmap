"""共享变化集及有界差异历史；不依赖编辑引擎、布局或文件格式。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields
from typing import Any

from .errors import MindMapError
from .models import MindMapDocument, new_id

_COLLECTIONS = ("nodes", "assets", "submaps")


@dataclass
class ChangeSet:
    """实体与文档字段的前后值；同一份变化集驱动撤销、重做和事件摘要。"""

    collections: dict[str, dict[str, tuple[Any, Any]]] = field(default_factory=dict)
    settings: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    # 操作所属页面随内容历史保存；界面据此跨页展示撤销结果，导航自身不产生历史。
    page_id: str | None = None

    @classmethod
    def between(cls, before: MindMapDocument, after: MindMapDocument) -> ChangeSet:
        """比较两个独立状态，只长期保留变化实体，自动包含新增的文档级字段。"""
        collections = {}
        for name in _COLLECTIONS:
            old, current = getattr(before, name), getattr(after, name)
            delta = {
                key: (deepcopy(old.get(key)), deepcopy(current.get(key)))
                for key in old.keys() | current.keys()
                if old.get(key) != current.get(key)
            }
            if delta:
                collections[name] = delta
        settings = {
            item.name: (deepcopy(getattr(before, item.name)), deepcopy(getattr(after, item.name)))
            for item in fields(after)
            if item.name not in _COLLECTIONS and getattr(before, item.name) != getattr(after, item.name)
        }
        return cls(collections, settings)

    @property
    def empty(self) -> bool:
        return not self.collections and not self.settings

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self.collections.get("nodes", {}))

    def apply(self, document: MindMapDocument, *, forward: bool) -> None:
        """重放前后值的独立副本，保证后续编辑不会污染历史保存的数据。"""
        index = 1 if forward else 0
        for name, delta in self.collections.items():
            target = getattr(document, name)
            for key, values in delta.items():
                if values[index] is None:
                    target.pop(key, None)
                else:
                    target[key] = deepcopy(values[index])
        for name, values in self.settings.items():
            setattr(document, name, deepcopy(values[index]))


@dataclass
class HistoryEntry:
    """一条原子提交及前后状态标识；状态标识用于准确跟踪保存位置。"""

    label: str
    delta: ChangeSet
    before_token: str
    after_token: str


class HistoryManager:
    """管理撤销、重做、容量与保存状态；通知时机由调用方控制。"""

    def __init__(self, limit: int):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise MindMapError("历史容量至少为 1")
        self.limit = limit
        self._undo: list[HistoryEntry] = []
        self._redo: list[HistoryEntry] = []
        self._state = self._saved = new_id()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def is_dirty(self) -> bool:
        return self._state != self._saved

    @property
    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    def record(self, delta: ChangeSet, label: str) -> None:
        """提交非空差异；从撤销位置编辑时立即丢弃原重做分支。"""
        if delta.empty:
            return
        entry = HistoryEntry(label, delta, self._state, new_id())
        self._undo.append(entry)
        del self._undo[: -self.limit]
        self._redo.clear()
        self._state = entry.after_token

    def undo(self, document: MindMapDocument) -> HistoryEntry:
        """调用方确认可撤销且通过操作钩子后，回放前值并移动历史游标。"""
        entry = self._undo.pop()
        entry.delta.apply(document, forward=False)
        self._redo.append(entry)
        self._state = entry.before_token
        return entry

    def redo(self, document: MindMapDocument) -> HistoryEntry:
        """回放后值；历史对象不向调用方暴露可修改的文档实例。"""
        entry = self._redo.pop()
        entry.delta.apply(document, forward=True)
        self._undo.append(entry)
        self._state = entry.after_token
        return entry

    def mark_saved(self, token=None) -> None:
        """持久化成功后记录对应状态；省略令牌时记录当前游标，后台保存传入捕获的令牌。"""
        self._saved = self._state if token is None else token

    def reset(self) -> None:
        """替换文档时建立全新历史分支和保存位置。"""
        self._undo.clear()
        self._redo.clear()
        self._state = self._saved = new_id()
