"""实体写时复制、差量回滚及运行时父级/资源索引，不建立整图快照。"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field

from .errors import MindMapError
from .history import ChangeSet
from .models import MindMapDocument


@dataclass
class DocumentState:
    """内部文档及增量索引；写入登记在当前日志中，索引不进入持久化文档。"""

    document: MindMapDocument
    journal: TransactionJournal | None = field(default=None, init=False)

    def __post_init__(self):
        self.reset(self.document)

    def reset(self, document):
        """已完整校验的新文档建立一次索引，仅在装载或替换文档时调用。"""
        self.document = document
        self.parents = document.parents()
        self.asset_counts = Counter(node.image_id for node in document.nodes.values() if node.image_id)
        self.unused_assets = set(document.assets) - self.asset_counts.keys()

    def edit_node(self, key):
        """首次写入复制单个节点；原实体保持不变，供回滚及历史比较使用。"""
        if self.journal is None:
            raise MindMapError("节点修改必须在事务中执行")
        if key not in self.journal.nodes:
            self.journal.nodes[key] = self.document.nodes[key]
            self.document.nodes[key] = deepcopy(self.document.nodes[key])
        return self.document.nodes[key]

    def put_node(self, node):
        """登记新增实体，调用方传入由编辑器独立拥有的节点。"""
        self.journal.nodes.setdefault(node.id, self.document.nodes.get(node.id))
        self.document.nodes[node.id] = node
        for child in node.children:
            self.set_parent(child, node.id)

    def delete_node(self, key):
        """登记删除前值及父索引，祖先/后代由上层结构操作统一处理。"""
        self.journal.nodes.setdefault(key, self.document.nodes[key])
        del self.document.nodes[key]
        self.set_parent(key, None)

    def set_parent(self, key, parent):
        """立即维护父索引供嵌套操作读取，回滚时一并恢复。"""
        self.journal.parents.setdefault(key, self.parents.get(key))
        if parent is None:
            self.parents.pop(key, None)
        else:
            self.parents[key] = parent

    def set_asset(self, key, asset):
        """登记不可变资源的新增或删除，None 表示删除。"""
        self.journal.assets.setdefault(key, self.document.assets.get(key))
        if asset is None:
            self.document.assets.pop(key, None)
        else:
            self.document.assets[key] = asset

    def set_setting(self, name, value):
        """替换文档级字段的独立副本，只保存该字段的原值，保持外部输入隔离。"""
        self.journal.settings.setdefault(name, getattr(self.document, name))
        setattr(self.document, name, deepcopy(value))

    def update_indexes(self, changes, *, forward=True, parents=True):
        """按已验证变化集更新索引；历史重放先删除旧边再添加新边，避免换父次序问题。

        普通提交的父引用已即时维护，因此可关闭 parents。引用计数仅在成功提交
        或历史重放后改变，失败事务不必扫描资源或恢复整张计数表。
        """
        index = 1 if forward else 0
        nodes = changes.collections.get("nodes", {})
        if parents:
            for key, pair in nodes.items():
                old = pair[1 - index]
                if old is not None:
                    for child in old.children:
                        if self.parents.get(child) == key:
                            self.parents.pop(child)
                if pair[index] is None:
                    self.parents.pop(key, None)
            for key, pair in nodes.items():
                node = pair[index]
                if node is not None:
                    for child in node.children:
                        self.parents[child] = key
        for pair in nodes.values():
            old, new = pair[1 - index], pair[index]
            if old is not None and old.image_id:
                self.asset_counts[old.image_id] -= 1
                if not self.asset_counts[old.image_id]:
                    del self.asset_counts[old.image_id]
            if new is not None and new.image_id:
                self.asset_counts[new.image_id] += 1
        for key in changes.collections.get("assets", {}):
            if key in self.document.assets and not self.asset_counts.get(key):
                self.unused_assets.add(key)
            else:
                self.unused_assets.discard(key)


class TransactionJournal:
    """保存首次写入前值，未变化实体既不复制也不扫描。"""

    def __init__(self, state):
        self.state = state
        self.nodes, self.assets, self.settings, self.parents = {}, {}, {}, {}

    def changes(self):
        """仅比较登记实体；历史拥有独立前后值，后续编辑不能污染历史。"""
        document = self.state.document
        collections = {}
        for name in ("nodes", "assets"):
            current = getattr(document, name)
            delta = {
                key: (deepcopy(old), deepcopy(current.get(key)))
                for key, old in getattr(self, name).items() if old != current.get(key)
            }
            if delta:
                collections[name] = delta
        settings = {
            key: (deepcopy(old), deepcopy(getattr(document, key)))
            for key, old in self.settings.items() if old != getattr(document, key)
        }
        return ChangeSet(collections, settings)

    def rollback(self):
        """原实体未被修改，直接恢复实体及父索引，无需再次深拷贝整图。"""
        document = self.state.document
        for name in ("nodes", "assets"):
            target = getattr(document, name)
            for key, old in getattr(self, name).items():
                if old is None:
                    target.pop(key, None)
                else:
                    target[key] = old
        for key, old in self.settings.items():
            setattr(document, key, old)
        for key, old in self.parents.items():
            if old is None:
                self.state.parents.pop(key, None)
            else:
                self.state.parents[key] = old
