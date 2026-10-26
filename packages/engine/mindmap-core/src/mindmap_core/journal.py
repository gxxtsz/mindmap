"""实体写时复制、差量回滚及运行时父级/资源索引，不建立整图快照。"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field

from .errors import MindMapError
from .history import ChangeSet, NodeMove, NodePlacement
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
        self.root_pages = {document.root_id: None, **{p.root_id: p.id for p in document.iter_pages()}}
        self.root_pages.update(dict.fromkeys(document.inspiration.root_ids, document.inspiration.id))
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

    def page_id(self, node_id):
        """只沿当前节点的页内父链查询归属，不扫描其他页面或建立整图快照。"""
        while node_id in self.parents:
            node_id = self.parents[node_id]
        if node_id not in self.root_pages:
            raise MindMapError("节点不属于有效页面")
        return self.root_pages[node_id]

    def page(self, node_id):
        """返回节点所属页面的内部配置，调用方只能通过日志接口修改。"""
        page_id = self.page_id(node_id)
        return self.document.page_record(page_id)

    def set_page(self, key, page, *, kind=None):
        """以写时复制方式替换页面记录，同时即时维护根索引供嵌套事务查询。"""
        # 新页面由调用方指定类别；更新和删除从原表推导，绝不把详情页写进子导图表。
        kind = kind or self.document.page_kind(key)
        name = "detail_pages" if kind == "detail" else "submaps"
        pages = getattr(self.document, name)
        old = pages.get(key)
        getattr(self.journal, name).setdefault(key, old)
        if old is not None:
            self.root_pages.pop(old.root_id, None)
        if page is None:
            pages.pop(key, None)
        else:
            if page.root_id in self.root_pages:
                raise MindMapError("页面根节点重复")
            pages[key] = deepcopy(page)
            self.root_pages[page.root_id] = key

    def update_indexes(self, changes, *, forward=True, parents=True):
        """按已验证变化集更新索引；历史重放先删除旧边再添加新边，避免换父次序问题。

        普通提交的父引用已即时维护，因此可关闭 parents。引用计数仅在成功提交
        或历史重放后改变，失败事务不必扫描资源或恢复整张计数表。
        """
        index = 1 if forward else 0
        if "inspiration" in changes.settings:
            old, new = changes.settings["inspiration"][1 - index], changes.settings["inspiration"][index]
            for root in old.root_ids:
                if self.root_pages.get(root) == old.id:
                    self.root_pages.pop(root)
            self.root_pages.update(dict.fromkeys(new.root_ids, new.id))
        nodes = changes.collections.get("nodes", {})
        pages = {**changes.collections.get("submaps", {}), **changes.collections.get("detail_pages", {})}
        for pair in pages.values():
            old = pair[1 - index]
            if old is not None:
                self.root_pages.pop(old.root_id, None)
        for pair in pages.values():
            page = pair[index]
            if page is not None:
                self.root_pages[page.root_id] = page.id
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
        self.nodes, self.assets, self.submaps, self.detail_pages = {}, {}, {}, {}
        self.settings, self.parents = {}, {}
        self.page_id = None
        self.has_origin = False

    def changes(self):
        """仅比较登记实体；历史拥有独立前后值，后续编辑不能污染历史。"""
        document = self.state.document
        collections = {}
        for name in ("nodes", "assets", "submaps", "detail_pages"):
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
        return ChangeSet(collections, settings, self.page_id, self._moves())

    def _moves(self) -> tuple[NodeMove, ...]:
        """从登记过父引用的节点生成净移动快照，不从兄弟顺序变化猜测拖动对象。

        parents 保留首次改动前的父标识，包括同父节点排序；未登记的兄弟及后代
        不能成为移动对象。新建、删除、页面根及往返后位置不变的节点均被排除。
        名称从写时复制前值和正式结果读取，只扫描相关父级的子列表及页根分支；
        缓存索引和方向，批量移动不会为每个节点重复枚举同一父级或整张节点表。
        """
        if not self.parents and "inspiration" not in self.settings:
            return ()
        document = self.state.document
        positions, sides = {}, {}
        old_area = self.settings.get("inspiration", document.inspiration)
        root_pages = self.state.root_pages.copy()
        for root in document.inspiration.root_ids:
            root_pages.pop(root, None)
        root_pages.update(dict.fromkeys(old_area.root_ids, old_area.id))
        for table in (self.submaps, self.detail_pages):
            for page in table.values():
                if page is not None:
                    root_pages[page.root_id] = page.id

        def node(key, before):
            current = document.nodes.get(key)
            return self.nodes.get(key, current) if before else current

        def parent(key, before):
            current = self.state.parents.get(key)
            return self.parents.get(key, current) if before else current

        def direction(key, before):
            # 沿该端父链确定所属页面及一级分支，不遍历分支后代。
            branch, root = key, key
            while (ancestor := parent(root, before)) is not None:
                branch, root = root, ancestor
            page_id = (root_pages if before else self.state.root_pages)[root]
            if page_id is None:
                layout = self.settings.get("layout", document.layout) if before else document.layout
            elif page_id == document.inspiration.id:
                layout = old_area.layout if before else document.inspiration.layout
            else:
                # 转回普通后代时来源页已经移除，必须先读事务前值，不能提前
                # 求值当前文档的 page_record；普通配置修改仍复用同一条路径。
                if before and page_id in self.submaps:
                    page = self.submaps[page_id]
                elif before and page_id in self.detail_pages:
                    page = self.detail_pages[page_id]
                else:
                    page = document.page_record(page_id)
                layout = page.layout
            if layout.direction != "both":
                return layout.direction
            if key == root:
                return "right"
            cache_key = before, root
            if cache_key not in sides:
                counts, resolved = {"left": 0, "right": 0}, {}
                for child in node(root, before).children:
                    side = node(child, before).direction or min(counts, key=counts.get)
                    counts[side] += 1
                    resolved[child] = side
                sides[cache_key] = resolved
            return sides[cache_key][branch]

        def placement(key, parent_id, before):
            root = key
            while (ancestor := parent(root, before)) is not None:
                root = ancestor
            page_id = (root_pages if before else self.state.root_pages)[root]
            if parent_id is None:
                area = old_area if before else document.inspiration
                return NodePlacement(node(key, before).text, None, "灵感区", area.root_ids.index(key) + 1,
                                     direction(key, before), page_id, tuple(area.positions[key]))
            owner = node(parent_id, before)
            cache_key = before, parent_id
            if cache_key not in positions:
                positions[cache_key] = {child: index for index, child in enumerate(owner.children, 1)}
            return NodePlacement(node(key, before).text, parent_id, owner.text,
                                 positions[cache_key][key], direction(key, before), page_id)

        moves = []
        candidates = dict(self.parents)
        if "inspiration" in self.settings:
            for key in set(old_area.root_ids) & set(document.inspiration.root_ids):
                if old_area.positions[key] != document.inspiration.positions[key]:
                    candidates.setdefault(key, self.state.parents.get(key))
        for key, old_parent in candidates.items():
            new_parent = parent(key, False)
            if node(key, True) is None or node(key, False) is None:
                continue
            # 包装根可能因保留独立内容而变成普通子节点。页面根没有普通父级，
            # 不属于拖动位置记录，更不能按 parent=None 把它当作灵感区游离根。
            if ((key in root_pages and root_pages[key] != old_area.id)
                    or (key in self.state.root_pages and self.state.root_pages[key] != document.inspiration.id)):
                continue
            old = placement(key, old_parent, True)
            new = placement(key, new_parent, False)
            if (old.parent_id, old.index, old.direction, old.page_id, old.position) != (
                    new.parent_id, new.index, new.direction, new.page_id, new.position):
                moves.append(NodeMove(key, old, new))
        return tuple(moves)

    def rollback(self):
        """原实体未被修改，直接恢复实体及父索引，无需再次深拷贝整图。"""
        document = self.state.document
        for name in ("submaps", "detail_pages"):
            for key, old in getattr(self, name).items():
                current = getattr(document, name).get(key)
                if current is not None:
                    self.state.root_pages.pop(current.root_id, None)
                if old is not None:
                    self.state.root_pages[old.root_id] = old.id
        for name in ("nodes", "assets", "submaps", "detail_pages"):
            target = getattr(document, name)
            for key, old in getattr(self, name).items():
                if old is None:
                    target.pop(key, None)
                else:
                    target[key] = old
        for key, old in self.settings.items():
            setattr(document, key, old)
            if key == "inspiration":
                self.state.root_pages = {root: page for root, page in self.state.root_pages.items()
                                         if page != old.id}
                self.state.root_pages.update(dict.fromkeys(old.root_ids, old.id))
        for key, old in self.parents.items():
            if old is None:
                self.state.parents.pop(key, None)
            else:
                self.state.parents[key] = old
