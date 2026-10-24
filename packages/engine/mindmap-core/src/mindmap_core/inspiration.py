"""灵感区结构编辑；只改变归属和根坐标，保留全部节点、页面与资源标识。"""

from copy import deepcopy
from math import isfinite

from .errors import MindMapError
from .models import Node


def _point(value):
    """将调用方坐标规范为独立浮点元组，拒绝布尔、溢出和非有限数。"""
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)):
        raise MindMapError("灵感区坐标必须为两个有限数值")
    try:
        result = tuple(float(v) for v in value)
    except OverflowError as exc:
        raise MindMapError("灵感区坐标超出有效范围") from exc
    if not all(isfinite(v) for v in result):
        raise MindMapError("灵感区坐标必须为两个有限数值")
    return result


class InspirationEditorMixin:
    """供文档编辑器组合的事务内操作，不自行提交历史或发送事件。"""

    def _edit_inspiration(self):
        area = deepcopy(self.document.inspiration)
        self._state.set_setting("inspiration", area)
        return self.document.inspiration

    def _unlink_branches(self, roots):
        """移除普通父边或游离根登记，不删除任何实体及附属页面。"""
        area = self._edit_inspiration() if any(
            key in self.document.inspiration.root_ids for key in roots) else None
        for key in roots:
            parent = self._state.parents.get(key)
            if parent is not None:
                self.edit(parent).children.remove(key)
            elif area is not None and key in area.root_ids:
                area.root_ids.remove(key)
                area.positions.pop(key)
                self._state.root_pages.pop(key, None)
            else:
                raise MindMapError("主导图、子导图及详情页的根节点不能移动或删除")
            self._state.set_parent(key, None)

    def add_inspiration(self, text="新想法", *, position):
        """新增有序游离根；坐标及文本由最外层提交统一校验。"""
        position = _point(position)
        # 分支样式使用区域的标准新建根配置，与普通新文档保持一致。
        from .branch_styles import BranchStyle

        node = Node(text, style={"branch": {"kind": BranchStyle.ROUND.value}})
        self._state.put_node(node)
        area = self._edit_inspiration()
        area.root_ids.append(node.id)
        area.positions[node.id] = deepcopy(position)
        self._state.root_pages[node.id] = area.id
        return node.id

    def set_inspiration_positions(self, positions):
        """替换指定游离根的坐标，拒绝对子节点或失效标识写位置。"""
        if not isinstance(positions, dict) or not positions.keys() <= self.document.inspiration.positions.keys():
            raise MindMapError("只能调整有效游离根节点的位置")
        area = self._edit_inspiration()
        area.positions.update({key: _point(point) for key, point in positions.items()})

    def move_to_inspiration(self, node_ids, *, positions):
        """将同页普通分支拆成游离树，原顺序和所有附属内容保持不变。"""
        roots = self.roots(node_ids)
        if not roots:
            return []
        pages = {self._state.page_id(key) for key in roots}
        if len(pages) != 1 or self.document.page_kind(next(iter(pages))) == "detail":
            raise MindMapError("只能移动同一主导图、子导图或灵感区中的分支")
        if not isinstance(positions, dict) or set(positions) != set(roots):
            raise MindMapError("请为每条移动分支提供唯一的游离坐标")
        old_order = list(self.document.inspiration.root_ids)
        self._unlink_branches(roots)
        area = self._edit_inspiration()
        # 原有游离根仅换位置时不改变根序；新拆出的分支按原树序追加。
        area.root_ids.extend(roots)
        ordered = [key for key in old_order if key in area.root_ids]
        area.root_ids = ordered + [key for key in area.root_ids if key not in ordered]
        area.positions.update({key: _point(point) for key, point in positions.items()})
        for key in roots:
            self._state.root_pages[key] = area.id
        return roots

    def attach_inspirations(self, node_ids, parent_id):
        """游离分支原地迁入主导图或子导图，禁止跨附属页面形成归属环。"""
        roots = self.roots(node_ids)
        if not roots:
            return
        parent = self.node(parent_id)
        target = self._state.page_id(parent_id)
        if self.document.page_kind(target) not in ("main", "submap"):
            raise MindMapError("请选择主导图或子导图中的父主题")
        if any(self._state.page_id(key) != self.document.inspiration.id for key in roots):
            raise MindMapError("加入导图的来源必须属于灵感区")
        self.require_plain_parent(parent)
        if any(parent_id in self.document.owned_descendants(key) for key in roots):
            raise MindMapError("不能移动到自身、后代或所属附属页面中")
        self._unlink_branches(roots)
        parent = self.edit(parent_id)
        parent.children.extend(roots)
        parent.expanded = True
        for key in roots:
            self._state.set_parent(key, parent_id)

    def group_inspirations(self, node_ids, text="新主题", *, position):
        """创建共同游离父主题并组合选中分支，整个过程由一次事务回滚或提交。"""
        roots = self.roots(node_ids)
        if not roots or any(self._state.page_id(key) != self.document.inspiration.id for key in roots):
            raise MindMapError("请选择灵感区中的主题")
        self._unlink_branches(roots)
        parent = self.add_inspiration(text, position=position)
        self.edit(parent).children = roots
        for key in roots:
            self._state.set_parent(key, parent)
        return parent
