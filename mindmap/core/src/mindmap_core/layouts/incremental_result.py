"""增量树布局服务与稳定的按需结果；仅在显式遍历时展开全部节点/连接线。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace

from mindmap_core import BranchPath, BranchStyle, Curve, LayoutResult, MindMapError, Rect, get_branch_style

from .connectors import route_children
from .incremental import IncrementalTreeGeometry
from .persistent import PersistentMap


def _translated(branch, x, y):
    def point(value):
        return value[0] + x, value[1] + y
    if isinstance(branch, Curve):
        return replace(branch, points=tuple(map(point, branch.points)))
    return replace(branch, start=point(branch.start), end=point(branch.end),
                   commands=tuple(replace(command, points=tuple(map(point, command.points)))
                                  for command in branch.commands),
                   arrow=tuple(map(point, branch.arrow)) if branch.arrow else None)


def _bounds(branch):
    if isinstance(branch, BranchPath):
        return branch.bounds
    xs, ys = zip(*branch.points, strict=True)
    return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)).padded(2)


class _NodeRects(Mapping):
    def __init__(self, geometry):
        self.geometry = geometry

    def __len__(self):
        return self.geometry.visible_count

    def __getitem__(self, key):
        try:
            return self.geometry.rectangle(key)
        except (KeyError, MindMapError) as exc:
            raise KeyError(key) from exc

    def __iter__(self):
        # 保持原 TreeLayout 的插入顺序，重叠落区等依赖稳定顺序的调用仍得到相同结果。
        geometry = self.geometry
        yield geometry.root
        pending = [geometry.root]
        while pending:
            key = pending.pop()
            if geometry.expanded[key]:
                for group in geometry.groups[key].values():
                    for child in group.keys:
                        yield child
                        pending.append(child)


class _Branches(Mapping):
    def __init__(self, result):
        self.result = result

    def __len__(self):
        return len(self.result.nodes) - 1

    def __iter__(self):
        return (key for key in self.result.nodes if key != self.result.geometry.root)

    def __getitem__(self, key):
        result = self.result
        if key == result.geometry.root or key not in result.nodes:
            raise KeyError(key)
        parent = result.geometry.parents[key]
        branches, origin = result.parent_branches(parent)
        return _translated(branches[key], *origin)


class _Routing:
    """独立路由上下文，不持有结果映射；旧布局释放时不产生结果与分支映射的引用环。"""

    def __init__(self, geometry, styles, palette, accent, route_cache, extrema, color_rows):
        self.geometry, self.styles = geometry, styles
        self.palette, self.accent = palette, accent
        self._route_cache, self._parent_routes = route_cache, {}
        self._extrema = extrema
        self._color_rows = color_rows
        self.nodes = _NodeRects(geometry)
        self.bounds = geometry.bounds

    def color(self, key):
        geometry = self.geometry
        while key != geometry.root and geometry.parents[key] != geometry.root:
            key = geometry.parents[key]
        return (self.palette[geometry.root_order[key] % len(self.palette)]
                if key != geometry.root else self.accent)

    def parent_branches(self, parent, keys=None):
        """缓存父级局部路径；子树仅平移时复用路径，世界原点由调用方单独设置。"""
        geometry = self.geometry
        if keys is None and parent in self._parent_routes:
            return self._parent_routes[parent]
        rect = geometry.rectangle(parent)
        logical = Rect(0, 0, *geometry.sizes[parent])
        records = []
        direction = geometry.side(parent)
        selected = set(keys) if keys is not None else None
        for group_side, group in geometry.groups[parent].items():
            side = group_side if parent == geometry.root else direction
            children = (group.keys if selected is None else
                        sorted((key for key in selected if geometry.locations[key][1] == group_side),
                               key=lambda key: geometry.locations[key][2]))
            for child in children:
                child_rect = geometry._transform(geometry._child_rect(logical, child, side))
                local = child_rect
                records.append((child, local, "down" if geometry.down else side,
                                self.color(child) if parent == geometry.root else self.color(parent)))
        kind = self.styles[parent]
        request = rect.width, rect.height, kind, tuple(records)
        cached = self._route_cache.get(parent)
        if cached is not None and cached[0] == request:
            branches = cached[1]
            self._route_cache.move_to_end(parent)
        else:
            branches = route_children(Rect(0, 0, rect.width, rect.height), records, kind)
            self._route_cache[parent] = request, branches
            self._route_cache.move_to_end(parent)
            if len(self._route_cache) > 2048:
                self._route_cache.popitem(last=False)
        result = branches, (rect.x, rect.y)
        if keys is None:
            self._parent_routes[parent] = result
        return result

    def viewport(self, viewport: Rect, pinned=()):
        """取得屏内节点和相交连接线；返回局部路径及原点，避免平移时重建 Qt 路径。

        大量同级的正交连接线仅查询附近出口并补充各颜色的两端，保留完整共享主干。
        曲线必须保留所有可能穿过视口的边，不以节点可见性代替连线可见性。
        """
        geometry = self.geometry
        nodes = geometry.query(viewport)
        parents = dict(geometry.visited_parent_rects)
        for key in pinned:
            rect = self.nodes.get(key)
            if rect is not None:
                nodes[key] = rect
                parent = geometry.parents.get(key)
                if parent is not None:
                    parents[parent] = geometry.rectangle(parent)
        branches = {}
        logical_view = geometry._transform(viewport)
        for parent, rect in parents.items():
            if not geometry.expanded[parent] or not geometry.children[parent]:
                continue
            # 父子连线只占直接相邻两列；深层子树的范围不应使根级连线反复参与路由。
            logical = geometry._transform(rect)
            side = geometry.side(parent)
            if parent != geometry.root:
                if side == "right" and (logical.right > logical_view.right or
                                         logical.right + geometry.gap_x < logical_view.x):
                    continue
                if side == "left" and (logical.x < logical_view.x or
                                        logical.x - geometry.gap_x > logical_view.right):
                    continue
            keys = None
            if len(geometry.children[parent]) > 128 and self.styles[parent] in (
                BranchStyle.ROUND, BranchStyle.ELBOW, BranchStyle.ARROW
            ):
                keys = set(self._extrema if parent == geometry.root else ())
                for group_side, group in geometry.groups[parent].items():
                    keys.update((group.keys[0], group.keys[-1]))
                    origin = logical.center[1] - group.total / 2
                    rows = list(group.rows(logical_view.y - origin, logical_view.bottom - origin))
                    keys.update(key for key, _ in rows)
                    if rows:
                        low = geometry.locations[rows[0][0]][2]
                        high = geometry.locations[rows[-1][0]][2]
                        if parent == geometry.root:
                            # 每种颜色的下一条边可能拥有跨入视口的主干，即使出口仍在屏外。
                            for (side, _), slots in self._color_rows.items():
                                if side != group_side:
                                    continue
                                for slot in (bisect_left(slots, low) - 1, bisect_right(slots, high)):
                                    if 0 <= slot < len(slots):
                                        keys.add(group.keys[slots[slot]])
                        else:
                            for slot in (low - 1, high + 1):
                                if 0 <= slot < len(group.keys):
                                    keys.add(group.keys[slot])
            paths, origin = self.parent_branches(parent, keys)
            for key, branch in paths.items():
                box = _bounds(branch)
                box = Rect(box.x + origin[0], box.y + origin[1], box.width, box.height)
                if box.intersects(viewport):
                    branches[key] = branch, origin
        return nodes, branches


class IncrementalLayoutResult(LayoutResult):
    """冻结在某次提交的按需布局；映射可以独立持有，后续更新不改变其几何。

    画布通过 viewport 只实例化屏内节点及相交连接线。小图动画或完整导出仍可
    显式遍历全部映射，成本随实际输出增长。路由上下文与结果单向持有，避免旧
    版本形成引用环后等待全局垃圾回收，造成交互期间的周期性停顿。
    """

    def __init__(self, geometry, styles, palette, accent, route_cache, extrema, color_rows):
        self._routing = _Routing(geometry, styles, palette, accent, route_cache, extrema, color_rows)
        self.geometry = geometry
        super().__init__(nodes=self._routing.nodes, branches=_Branches(self._routing), bounds=geometry.bounds)

    def viewport(self, viewport, pinned=()):
        """返回视口节点及局部路径/原点，不构造全量几何字典。"""
        return self._routing.viewport(viewport, pinned)


class IncrementalTreeLayout:
    """正式内置树布局的增量服务；仅接受引擎已提交的文档及对应完整差量。"""

    def __init__(self, document, measurements):
        self.geometry = IncrementalTreeGeometry(document, measurements, _validated=True)
        self.styles = PersistentMap({key: get_branch_style(node.style) for key, node in document.nodes.items()})
        self.palette, self.accent = tuple(document.theme.palette), document.theme.accent
        self._route_cache = OrderedDict()
        self._style_cache = {}
        self._update_extrema()

    def _update_extrema(self):
        groups = {}
        geometry = self.geometry
        for child in geometry.children[geometry.root]:
            pair = geometry.root_sides[child], geometry.root_order[child] % len(self.palette)
            if pair not in groups:
                groups[pair] = [child, child]
            else:
                groups[pair][1] = child
        self._extrema = tuple(key for pair in groups.values() for key in pair)
        color_rows = {}
        for side, group in geometry.groups[geometry.root].items():
            for slot, child in enumerate(group.keys):
                color_rows.setdefault((side, geometry.root_order[child] % len(self.palette)), []).append(slot)
        self._color_rows = {key: tuple(slots) for key, slots in color_rows.items()}

    def update(self, document, measurements, node_ids):
        """同步布局和样式差量；方向/紧凑配置及聚焦范围的重建由宿主统一决定。"""
        node_ids = tuple(node_ids)
        geometry = self.geometry
        old_parents = geometry.parents.fork()
        invalidate = set()
        for key in node_ids:
            node = document.nodes.get(key)
            if (node is None or self.styles.get(key) != get_branch_style(node.style)
                    or geometry.children.get(key) != tuple(node.children)):
                current = key
                while current is not None and current not in invalidate:
                    invalidate.add(current)
                    current = old_parents.get(current)
        root_changed = tuple(document.root.children) != geometry.children[geometry.root]
        root_changed = root_changed or any(
            key in geometry.root_sides and key in document.nodes
            and geometry.hints.get(key) != document.nodes[key].direction for key in node_ids
        )
        geometry.sync(document, measurements, node_ids)
        for key in tuple(invalidate):
            current = geometry.parents.get(key)
            while current is not None and current not in invalidate:
                invalidate.add(current)
                current = geometry.parents.get(current)
        for key in invalidate:
            self._style_cache.pop(key, None)
        for key in node_ids:
            if key in document.nodes:
                style = get_branch_style(document.nodes[key].style)
                if self.styles.get(key) != style or key not in self.styles:
                    self.styles[key] = style
            else:
                self.styles.pop(key, None)
        palette = tuple(document.theme.palette)
        if root_changed or palette != self.palette:
            self.palette = palette
            self._update_extrema()
        self.accent = document.theme.accent

    def style_kinds(self, node_ids, scope="subtree"):
        """统计完整逻辑子树的样式，包含折叠后代；缓存仅沿结构/样式变化的祖先失效。"""
        result = set()
        geometry = self.geometry
        for key in node_ids:
            if scope == "level":
                result.add(self.styles[key])
                continue
            pending = [(key, False)]
            while pending:
                current, ready = pending.pop()
                if current in self._style_cache:
                    continue
                if ready:
                    kinds = {self.styles[current]}
                    for child in geometry.children[current]:
                        kinds.update(self._style_cache[child])
                    self._style_cache[current] = frozenset(kinds)
                else:
                    pending.append((current, True))
                    pending.extend((child, False) for child in geometry.children[current]
                                   if child not in self._style_cache)
            result.update(self._style_cache[key])
        return result

    def result(self):
        """常数成本冻结本次布局，节点、索引和路径缓存按需复用，旧结果保持稳定。"""
        return IncrementalLayoutResult(self.geometry.snapshot(), self.styles.fork(), self.palette,
                                       self.accent, self._route_cache, self._extrema, self._color_rows)

    def preview_measurements(self, document, measurements, node_ids):
        """在独立几何版本中试算节点尺寸，不改变正式布局、文档或已有结果。

        document 必须仍是本服务对应的已提交文档，只允许 measurements 中给定
        node_ids 的尺寸发生变化。原地文字编辑据此实时调整节点和连接线；持久化
        索引仅沿受影响祖先更新，不能为每次按键复制文档或重新扫描整张导图。
        """
        geometry = self.geometry.snapshot()
        geometry.sync(document, measurements, node_ids)
        return IncrementalLayoutResult(geometry, self.styles.fork(), self.palette, self.accent,
                                       self._route_cache, self._extrema, self._color_rows)
