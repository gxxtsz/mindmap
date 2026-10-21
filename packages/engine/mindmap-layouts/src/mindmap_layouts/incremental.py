"""正式画布的增量树几何：维护子树摘要，按需解析坐标和视口。

持久化映射与同级摘要树允许布局版本共享未变化数据。连接线路由和惰性结果由
incremental_result 组合；单独测量本模块不包含事务、排版或 Qt 绘制耗时。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass
from math import isfinite

from mindmap_core import MindMapDocument, MindMapError, Point, Rect

from .persistent import PersistentMap


@dataclass(frozen=True, slots=True)
class _Summary:
    total: float
    reach: float
    count: int
    left: _Summary | None = None
    right: _Summary | None = None


def _combine(left, right):
    return _Summary(left.total + right.total, max(left.reach, right.reach),
                    left.count + right.count, left, right)


class _ChildIndex:
    """线段树同时维护子树高度之和与最大横向范围，单点更新及前缀查询为对数复杂度。"""

    def __init__(self, keys, metrics, gap):
        self.keys = tuple(keys)
        self.gap = gap
        self.capacity = 1 << max(0, (len(keys) - 1).bit_length())
        nodes = [_Summary(0.0, 0.0, 0)] * (2 * self.capacity)
        for index, key in enumerate(keys):
            reach, span, count = metrics[key]
            nodes[self.capacity + index] = _Summary(span + gap, reach, count)
        for index in range(self.capacity - 1, 0, -1):
            nodes[index] = _combine(nodes[2 * index], nodes[2 * index + 1])
        self.root = nodes[1]

    @property
    def total(self):
        return self.root.total - self.gap if self.keys else 0.0

    def update(self, slot, metric):
        def replace(node, low, high):
            if high - low == 1:
                return _Summary(metric[1] + self.gap, metric[0], metric[2])
            middle = (low + high) // 2
            if slot < middle:
                return _combine(replace(node.left, low, middle), node.right)
            return _combine(node.left, replace(node.right, middle, high))
        result = copy(self)
        result.root = replace(self.root, 0, self.capacity)
        return result

    def prefix(self, slot):
        """返回指定同级项之前的累计高度，包含各项之后的间距。"""
        node, low, high, total = self.root, 0, self.capacity, 0.0
        while high - low > 1:
            middle = (low + high) // 2
            if slot < middle:
                node, high = node.left, middle
            else:
                total += node.left.total
                node, low = node.right, middle
        if slot > low:
            total += node.total
        return total

    def rows(self, low, high):
        """按高度区间遍历相交的子树，整段屏外同级项直接跳过。"""
        pending = [(self.root, 1, 0.0)]
        while pending:
            node, index, start = pending.pop()
            length = node.total
            if not length or start >= high or start + length <= low:
                continue
            if index >= self.capacity:
                slot = index - self.capacity
                if slot < len(self.keys):
                    yield self.keys[slot], start
            else:
                pending.append((node.right, 2 * index + 1, start + node.left.total))
                pending.append((node.left, 2 * index, start))


class IncrementalTreeGeometry:
    """拥有独立紧凑结构的增量几何原型，支持四方向、紧凑排列、展开和子树移动。

    构造时完整校验并读取文档，随后只保存独立子列表、尺寸和展开标记，不持有
    输入节点引用，也不修改输入文档。尺寸必须覆盖全部节点，包括当前折叠节点。
    调用方负责把业务提交转换为对应的局部命令；主题及布局配置变化应重新构造。

    子树高度与横向范围向祖先传播，未受影响的子树不重新计算内部坐标。坐标
    通过父链及同级前缀和按需解析，query 只枚举与视口相交的分支。all_rectangles
    是测试和完整导出使用的线性展开接口，不能在每次交互刷新时调用。
    """

    def __init__(self, document: MindMapDocument, measurements: Mapping[str, Point], *, _validated=False):
        if not _validated:
            document.validate()
        self.root = document.root_id
        self.direction = document.layout.direction
        self.down = self.direction == "down"
        self.gap_x, self.gap_y = (32.0, 10.0) if document.layout.compact else (64.0, 22.0)
        self.children = {key: tuple(node.children) for key, node in document.nodes.items()}
        self.expanded = {key: node.expanded for key, node in document.nodes.items()}
        self.hints = {key: node.direction for key, node in document.nodes.items()}
        self.parents = document.parents()
        self.sizes = {}
        for key in self.children:
            try:
                size = measurements[key]
            except KeyError as exc:
                raise MindMapError(f"增量几何缺少节点尺寸：{key}") from exc
            self.sizes[key] = self._size(size)
        self.metrics = {}
        self.groups = {}
        self.locations = {}
        self.root_sides = {}
        self.root_order = {}
        for key in reversed(document.descendants(self.root)):
            self._rebuild(key)
            self.metrics[key] = self._metric(key)
        self.last_updated_nodes = 0
        self.last_visited_nodes = 0
        # 冷构造结束后切换持久化映射；之后的布局快照仅共享不可变树，不复制整图。
        for name in self._maps:
            setattr(self, name, PersistentMap(getattr(self, name)))

    _maps = ("children", "expanded", "hints", "parents", "sizes", "metrics", "groups", "locations",
             "root_sides", "root_order")

    def snapshot(self):
        """取得稳定的几何版本；后续提交不会改变旧布局、拖动起点或正在进行的试算。"""
        result = copy(self)
        for name in self._maps:
            setattr(result, name, getattr(self, name).fork())
        return result

    @property
    def visible_count(self):
        """当前展开状态下的节点总数，统计已包含在根摘要中，不遍历节点。"""
        return self.metrics[self.root][2]

    def sync(self, document: MindMapDocument, measurements: Mapping[str, Point], node_ids) -> None:
        """同步已提交并通过引擎校验的差量，支持新增、删除、换父、折叠及尺寸变化。

        node_ids 必须覆盖本次所有变化实体；布局方向、紧凑模式或根标识改变时应
        重新构造服务。未变化节点不读取，旧版本由持久化映射保留，试算和动画可
        持有 snapshot。新增/删除真实涉及多少实体，就维护相应数量的记录。
        """
        dirty = set(node_ids)
        removed = {key for key in dirty if key not in document.nodes}
        changed, old_edges = set(), {}
        for key in dirty - removed:
            node = document.nodes[key]
            children = tuple(node.children)
            size = self._size(measurements[key])
            previous = self.children.get(key)
            if previous != children:
                old_edges[key] = previous or ()
                self.children[key] = children
                changed.add(key)
            if self.sizes.get(key) != size:
                self.sizes[key] = size
                changed.add(key)
            if self.expanded.get(key) != node.expanded:
                self.expanded[key] = node.expanded
                changed.add(key)
            if key not in self.metrics:
                self.metrics[key] = (*size, 1)
            if self.hints.get(key) != node.direction or key not in self.hints:
                self.hints[key] = node.direction
                if key in self.root_sides and key in self.children[self.root]:
                    old_edges.setdefault(self.root, self.children[self.root])
                    changed.add(self.root)
        for key in removed:
            old_edges[key] = self.children.get(key, ())
        for key, children in old_edges.items():
            for child in children:
                if self.parents.get(child) == key:
                    self.parents.pop(child, None)
        for key in old_edges.keys() - removed:
            for child in self.children[key]:
                self.parents[child] = key
        for key in old_edges.keys() - removed:
            self._rebuild(key)
        for key in removed:
            for name in self._maps:
                getattr(self, name).pop(key, None)
        # 先处理变化集合的后代，避免大批新增深链按父到子反复传播。
        pending, seen, ordered = [(key, False) for key in changed], set(), []
        while pending:
            key, ready = pending.pop()
            if ready:
                ordered.append(key)
            elif key not in seen:
                seen.add(key)
                pending.append((key, True))
                pending.extend((child, False) for child in self.children[key] if child in changed)
        self.last_updated_nodes = 0
        for key in ordered:
            self._propagate(key)

    def side(self, key):
        """按父链读取当前有效方向，整棵分支换边无需改写每个后代。"""
        while key != self.root and self.parents[key] != self.root:
            key = self.parents[key]
        return self.root_sides[key] if key != self.root else "right"

    def order(self, key):
        """单个节点的先序排序键，用于少量选中节点排序，避免生成全图顺序表。"""
        path = []
        while key != self.root:
            parent, _, slot = self.locations[key]
            path.append(self.root_order[key] if parent == self.root else slot)
            key = parent
        return tuple(reversed(path))

    def layout_order(self, key):
        """原全量布局的节点插入顺序键，供重叠落区保持同距离下的稳定优先级。"""
        path = []
        while key != self.root:
            parent, side, slot = self.locations[key]
            for group_side, group in self.groups[parent].items():
                if group_side == side:
                    break
                slot += len(group.keys)
            path.append(slot)
            key = parent
        path.reverse()
        result = []
        for slot in path[:-1]:
            result.extend((1, -slot))
        if path:
            result.extend((0, path[-1]))
        return tuple(result)

    def nearest(self, key, dx, dy):
        """按原键盘规则寻找方向上的最近节点，查询区域逐级扩大，允许跨越屏外大间隙。"""
        current = self.rectangle(key).center
        bounds = self.bounds
        radius = 64.0
        maximum = max(abs(bounds.x - current[0]), abs(bounds.right - current[0]),
                      abs(bounds.y - current[1]), abs(bounds.bottom - current[1])) * 2 + 1
        best = None
        while True:
            if dx:
                area = Rect(current[0] + 1 if dx > 0 else current[0] - radius,
                            current[1] - radius / 2, radius - 1, radius)
            else:
                area = Rect(current[0] - radius / 2,
                            current[1] + 1 if dy > 0 else current[1] - radius,
                            radius, radius - 1)
            for candidate, rect in self.query(area).items():
                x, y = rect.center[0] - current[0], rect.center[1] - current[1]
                along = x * dx + y * dy
                if along > 1:
                    score = along + abs(y if dx else x) * 2
                    if best is None or (score, candidate) < best:
                        best = score, candidate
            if (best is not None and best[0] < radius) or radius >= maximum:
                return best[1] if best else None
            radius = min(maximum, radius * 2)

    @property
    def bounds(self):
        """由根的左右摘要直接求保守内容边界；保留路径描边余量及导出留白。"""
        root = self._root_rect()
        left, right, top, bottom = root.x, root.right, root.y, root.bottom
        if self.expanded[self.root]:
            for side, group in self.groups[self.root].items():
                if side == "left":
                    left = min(left, root.x - self.gap_x - group.root.reach)
                else:
                    right = max(right, root.right + self.gap_x + group.root.reach)
                top, bottom = min(top, -group.total / 2), max(bottom, group.total / 2)
        bounds = self._transform(Rect(left, top, right - left, bottom - top)).padded(43.6)
        if not all(isfinite(value) for value in (bounds.x, bounds.y, bounds.width, bounds.height)):
            raise MindMapError("布局范围超出有限数值范围")
        return bounds

    def _size(self, size):
        if len(size) != 2 or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and isfinite(value) and value > 0 for value in size
        ):
            raise MindMapError("节点尺寸必须为正有限数值")
        return (size[1], size[0]) if self.down else tuple(size)

    def _rebuild(self, key):
        """结构插入/移除时重建两个直接父级的同级索引，不遍历被移动子树。"""
        groups = {}
        counts = {"left": 0, "right": 0}
        for index, child in enumerate(self.children[key]):
            side = "right"
            if key == self.root:
                side = self.direction
                if side == "both":
                    side = self.hints[child] or min(counts, key=counts.get)
                    counts[side] += 1
                if side == "down":
                    side = "right"
            groups.setdefault(side, []).append(child)
            if key == self.root:
                self.root_sides[child] = side
                self.root_order[child] = index
        indexed = {}
        for side, keys in groups.items():
            group = _ChildIndex(keys, self.metrics, self.gap_y)
            indexed[side] = group
            for slot, child in enumerate(keys):
                self.locations[child] = key, side, slot
        self.groups[key] = indexed

    def _metric(self, key):
        width, height = self.sizes[key]
        if not self.expanded[key] or not self.children[key]:
            return width, height, 1
        groups = self.groups[key].values()
        return (
            width + self.gap_x + max(group.root.reach for group in groups),
            max(height, max(group.total for group in self.groups[key].values())),
            1 + sum(group.root.count for group in self.groups[key].values()),
        )

    def _propagate(self, key):
        while True:
            self.last_updated_nodes += 1
            metric = self._metric(key)
            if self.metrics[key] == metric:
                break
            self.metrics[key] = metric
            if key == self.root:
                break
            parent, side, slot = self.locations[key]
            groups = self.groups[parent].copy()
            groups[side] = groups[side].update(slot, metric)
            self.groups[parent] = groups
            key = parent

    def set_expanded(self, key: str, expanded: bool) -> None:
        """修改一个节点的可见子树；仅更新该节点和摘要发生变化的祖先。

        这是局部几何命令，接受严格布尔值；不修改业务文档、撤销记录或资源。
        递归展开须由调用方逐项提交，耗时随实际改变的节点数增长。
        """
        if key not in self.children or not isinstance(expanded, bool):
            raise MindMapError("展开参数无效")
        self.last_updated_nodes = 0
        if self.expanded[key] != expanded:
            self.expanded[key] = expanded
            self._propagate(key)

    def resize(self, key: str, size: Point) -> None:
        """更新已测量尺寸并局部传播；尺寸仍由 Qt 排版层提供，本模块不测量文字。"""
        if key not in self.sizes:
            raise MindMapError("节点不存在")
        size = self._size(size)
        self.last_updated_nodes = 0
        if self.sizes[key] != size:
            self.sizes[key] = size
            self._propagate(key)

    def move(self, key: str, parent: str, index: int | None = None) -> None:
        """在移除源节点后的目标同级列表中插入一棵子树，目标父节点同时展开。

        仅维护源/目标直接父级索引和祖先摘要；循环检查沿目标父链进行。当前原型
        的同级插入需要重建直接父级数组，宽同级列表的成本为父级度数之和。
        大批结构编辑应进一步使用可插入的顺序统计树，不能宣称此接口恒定耗时。
        """
        if key == self.root or key not in self.parents or parent not in self.children:
            raise MindMapError("移动节点或目标无效")
        current = parent
        while True:
            if current == key:
                raise MindMapError("不能移动到自身或后代节点")
            if current == self.root:
                break
            current = self.parents[current]
        old = self.parents[key]
        length = len(self.children[parent]) - (old == parent)
        if index is None:
            index = length
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= length:
            raise MindMapError("插入位置无效")
        siblings = list(self.children[old])
        siblings.remove(key)
        self.children[old] = tuple(siblings)
        siblings = list(self.children[parent])
        siblings.insert(index, key)
        self.children[parent] = tuple(siblings)
        self.parents[key] = parent
        self.expanded[parent] = True
        self._rebuild(old)
        if old != parent:
            self._rebuild(parent)
        self.last_updated_nodes = 0
        self._propagate(old)
        if old != parent:
            self._propagate(parent)

    def _root_rect(self):
        width, height = self.sizes[self.root]
        return Rect(-width / 2, -height / 2, width, height)

    def _child_rect(self, parent, child, side, prefix=None):
        parent_id, group_side, slot = self.locations[child]
        group = self.groups[parent_id][group_side]
        prefix = group.prefix(slot) if prefix is None else prefix
        width, height = self.sizes[child]
        top = parent.center[1] - group.total / 2 + prefix
        x = parent.x - self.gap_x - width if side == "left" else parent.right + self.gap_x
        return Rect(x, top + (self.metrics[child][1] - height) / 2, width, height)

    def _transform(self, rect):
        return Rect(rect.y, rect.x, rect.height, rect.width) if self.down else rect

    def rectangle(self, key: str) -> Rect:
        """按祖先链解析一个可见节点的世界坐标；折叠后代明确报错。

        查询成本为路径上各同级索引的对数之和，不展开整张导图。返回不可变矩形，
        可作为命中、选中定位和按需创建图元的输入。
        """
        if key not in self.children:
            raise MindMapError("节点不存在")
        path = []
        current = key
        while current != self.root:
            path.append(current)
            current = self.parents[current]
            if not self.expanded[current]:
                raise MindMapError("节点当前不可见")
        rect, side = self._root_rect(), "right"
        for child in reversed(path):
            if self.parents[child] == self.root:
                side = self.root_sides[child]
            rect = self._child_rect(rect, child, side)
        return self._transform(rect)

    def all_rectangles(self) -> dict[str, Rect]:
        """完整展开当前可见几何，供一致性验证/导出使用；此接口明确是线性复杂度。"""
        result = {}
        pending = [(self.root, self._root_rect(), "right")]
        while pending:
            key, rect, side = pending.pop()
            result[key] = self._transform(rect)
            if not self.expanded[key]:
                continue
            for group_side, group in self.groups[key].items():
                direction = group_side if key == self.root else side
                prefix = 0.0
                for child in group.keys:
                    pending.append((child, self._child_rect(rect, child, direction, prefix), direction))
                    prefix += self.metrics[child][1] + self.gap_y
        return result

    def query(self, viewport: Rect) -> dict[str, Rect]:
        """返回与视口相交的可见节点，利用子树包围盒及同级高度索引跳过屏外分支。

        输入使用最终方向的世界坐标，适用于平移/缩放后换算得到的逻辑视口。
        当前只查询节点矩形；连接线越过视口及箭头描边的覆盖需渲染层另行处理。
        """
        viewport = self._transform(viewport)
        pending = [(self.root, self._root_rect(), "right")]
        result = {}
        self.last_visited_nodes = 0
        self.visited_parent_rects = {}
        while pending:
            key, rect, side = pending.pop()
            self.last_visited_nodes += 1
            reach, span, _ = self.metrics[key]
            subtree = Rect(rect.right - reach if side == "left" else rect.x,
                           rect.center[1] - span / 2, reach, span)
            if key != self.root and not subtree.intersects(viewport):
                continue
            self.visited_parent_rects[key] = self._transform(rect)
            if rect.intersects(viewport):
                result[key] = self._transform(rect)
            if not self.expanded[key]:
                continue
            for group_side, group in self.groups[key].items():
                direction = group_side if key == self.root else side
                origin = rect.center[1] - group.total / 2
                for child, prefix in group.rows(viewport.y - origin, viewport.bottom - origin):
                    pending.append((child, self._child_rect(rect, child, direction, prefix), direction))
        return result
