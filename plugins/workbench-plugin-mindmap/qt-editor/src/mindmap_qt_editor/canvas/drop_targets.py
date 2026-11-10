"""根据拖动起点的固定几何计算节点命中、吸附和分支落点。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import floor, hypot

from mindmap_core import Rect
from PySide6.QtCore import QPointF, QRectF

ENTER_DISTANCE = 24.0
# 小图预建全部候选；大图按指针附近的固定几何生成，避免开始拖动时分配数万落点。
EAGER_TARGET_LIMIT = 500
SORT_DEPTH = 24.0
SORT_PADDING = 12.0


def _qrect(rect: Rect) -> QRectF:
    return QRectF(rect.x, rect.y, rect.width, rect.height)


def _distance(point: QPointF, rect: QRectF) -> float:
    """返回点到矩形的欧氏距离；外侧吸附圆角范围不会因对角线而扩大。"""
    return hypot(
        max(rect.left() - point.x(), 0, point.x() - rect.right()),
        max(rect.top() - point.y(), 0, point.y() - rect.bottom()),
    )


@dataclass(frozen=True)
class _DropProposal:
    """不可变的结构落点；index 是从目标父节点移除移动根之后的插入下标。"""

    roots: tuple[str, ...]
    parent_id: str
    index: int
    direction: str | None = None


@dataclass
class _Target:
    """落点的命中区域和视觉参照；基于固定节点几何，仅按视口倍率换算扩展范围。"""

    proposal: _DropProposal
    reference: str
    position: str
    region: QRectF


class _RegionIndex:
    """固定矩形的网格索引，查询结果保持原始顺序，不改变重叠落区的优先级。

    大矩形放入少量后备项，避免跨越数千网格的分支间隙占用过多内存；小矩形只
    登记其覆盖的单元。查询吸附时扩大点的搜索范围，最后仍由正式距离规则判定。
    """

    def __init__(self, entries):
        self.entries = tuple(entries)
        self.cells = defaultdict(list)
        self.overflow = []
        for index, (_, rect) in enumerate(self.entries):
            left, right = floor(rect.left() / 256), floor(rect.right() / 256)
            top, bottom = floor(rect.top() / 256), floor(rect.bottom() / 256)
            if (right - left + 1) * (bottom - top + 1) > 64:
                self.overflow.append(index)
                continue
            for x in range(left, right + 1):
                for y in range(top, bottom + 1):
                    self.cells[x, y].append(index)

    def nearby(self, point, radius=0.0):
        """查询点附近的候选矩形；半径使用场景单位，由调用方换算视口像素。"""
        indices = set(self.overflow)
        for x in range(floor((point.x() - radius) / 256), floor((point.x() + radius) / 256) + 1):
            for y in range(floor((point.y() - radius) / 256), floor((point.y() + radius) / 256) + 1):
                indices.update(self.cells.get((x, y), ()))
        return (self.entries[index] for index in sorted(indices))


class _OnDemand(dict):
    """按需派生的手势缓存，固定起点内重复查询不重新扫描相同直接父级。"""

    def __init__(self, loader):
        super().__init__()
        self.loader = loader

    def __missing__(self, key):
        value = self.loader(key)
        self[key] = value
        return value


class _MovingSubtrees:
    """通过祖先查询判断是否属于移动子树，拖动大分支不枚举全部后代。"""

    def __init__(self, roots, parents):
        self.roots, self.parents, self.cache = set(roots), parents, {}

    def __contains__(self, key):
        path, current = [], key
        while current is not None and current not in self.roots and current not in self.cache:
            path.append(current)
            current = self.parents.get(current)
        result = current in self.roots or self.cache.get(current, False)
        for current in path:
            self.cache[current] = result
        return result


class _DropResolver:
    """只读落点计算，缓存父索引、源子树和稳定几何，不读取场景中的预览图元。"""

    def __init__(self, document, layout, selection, focus_id, *, moving_roots=None):
        """冻结目标页和源根；moving_roots 供原生拖放传入已归并的跨展示树标识。

        目标文档中不存在的根只用于生成落点提案，不加入目标树或参与命中几何。
        参数均为只读借用；普通鼠标拖动仍从 selection 按树序解析移动根。
        """
        self.document = document
        self.layout = layout
        self.geometry = getattr(layout, "geometry", None)
        self._virtual = self.geometry is not None and len(layout.nodes) > 1000
        self.parents = self.geometry.parents if self._virtual else document.parents()
        selected = set(selection)
        # 显式遍历并跳过已选祖先的后代，同时保留文档先序顺序。
        roots = []
        if moving_roots is not None:
            roots = list(moving_roots)
        elif self._virtual:
            for key in selected:
                ancestor = self.parents.get(key)
                while ancestor is not None and ancestor not in selected:
                    ancestor = self.parents.get(ancestor)
                if ancestor is None:
                    roots.append(key)
            roots.sort(key=self.geometry.order)
        else:
            stack = list(reversed(document.root_ids))
            while stack:
                key = stack.pop()
                if key in selected:
                    roots.append(key)
                else:
                    stack.extend(reversed(document.nodes[key].children))
        self.roots = tuple(roots)
        self.moving = (_MovingSubtrees(roots, self.parents) if self._virtual else
                       {key for root in roots if root in document.nodes for key in document.descendants(root)})
        self.center_id = focus_id or document.root_id
        self.valid = bool(roots) and (getattr(document, "kind", None) == "inspiration"
                                     or not {document.root_id, self.center_id}.intersection(roots))
        self.down = document.layout.direction == "down"
        if self._virtual:
            self.sides = _OnDemand(lambda key: "down" if self.down else
                                   document.layout.direction if key == document.root_id
                                   and document.layout.direction != "both" else self.geometry.side(key))
            self.remaining = _OnDemand(lambda key: [child for child in document.nodes[key].children
                                                    if child not in self.moving])
            self.rows = _OnDemand(self._row)
            self.source_rects = []
        else:
            self.sides = document.branch_sides()
            self.remaining = {key: [child for child in node.children if child not in self.moving]
                              for key, node in document.nodes.items()}
            self.rows = {child: index for siblings in self.remaining.values()
                         for index, child in enumerate(siblings)}
            self.source_rects = [_qrect(rect) for key, rect in layout.nodes.items() if key in self.moving]
        self.targets: list[_Target] = []
        self.node_regions: dict[str, QRectF] = {}
        self._expanded_source = None
        self._expanded_scale = None
        self._expanded_targets = []
        self._source_index = _RegionIndex(enumerate(self.source_rects))

    def _row(self, key):
        parent = self.parents[key]
        self.rows.update((child, index) for index, child in enumerate(self.remaining[parent]))
        return self.rows[key]

    def _proposal(self, parent, index, side=None):
        if self.document.layout.direction != "both" or parent != self.document.root_id:
            side = None
        return _DropProposal(self.roots, parent, index, side)

    def rebuild(self, nodes, *, scale=None):
        """用正式起点建立稳定索引；小图预建排序候选，大图按指针位置准备附近候选。"""
        self.targets = []
        if self._virtual:
            self.node_regions = {}
            self._gap_index = None
            self._node_index = _RegionIndex(())
            return
        self.node_regions = {key: _qrect(rect) for key, rect in nodes.items() if key not in self.moving}
        self._node_index = _RegionIndex(self.node_regions.items())
        self._region_order = {key: index for index, key in enumerate(self.node_regions)}
        lazy = len(self.node_regions) > EAGER_TARGET_LIMIT
        if not lazy:
            self._append_node_targets(self.node_regions.items())
        # 同级间隙也是可操作区域，较高子树不会留下必须精准命中节点的空档。
        for parent, siblings in self.remaining.items():
            groups = {}
            for key in siblings:
                if key in self.node_regions:
                    groups.setdefault(self.sides[key], []).append(key)
            for side, group in groups.items():
                for previous, following in zip(group, group[1:]):
                    a, b = self.node_regions[previous], self.node_regions[following]
                    zone = (
                        QRectF(
                            a.right(),
                            min(a.top(), b.top()),
                            b.left() - a.right(),
                            max(a.bottom(), b.bottom()) - min(a.top(), b.top()),
                        )
                        if self.down
                        else QRectF(
                            min(a.left(), b.left()),
                            a.bottom(),
                            max(a.right(), b.right()) - min(a.left(), b.left()),
                            b.top() - a.bottom(),
                        )
                    )
                    if zone.width() > 0 and zone.height() > 0:
                        self.targets.append(
                            _Target(
                                self._proposal(parent, self.rows[following], side),
                                following,
                                "before",
                                zone,
                            )
                        )
        self._gap_index = None
        if lazy:
            self._gap_index = _RegionIndex((target, target.region) for target in self.targets)
            self.targets = []
        if scale is not None:
            self._targets_at_scale(scale)

    def _append_node_targets(self, regions):
        """按正式节点顺序生成正文和两侧排序候选，小图和大图共用同一套落区语义。"""
        for key, rect in regions:
            # 子导图入口可以作为同级排序参照，但不能出现接入普通子节点的落区。
            has_submap = bool(self.document.nodes[key].submap_id)
            if key == self.center_id or key not in self.parents:
                if has_submap:
                    continue
                sides = (
                    ("left", "right")
                    if self.document.layout.direction == "both" and key == self.document.root_id
                    else (self.sides[key],)
                )
                for side in sides:
                    zone = QRectF(rect)
                    gap = 32 if self.document.layout.compact else 64
                    if side == "left":
                        zone.setLeft(rect.left() - gap)
                        if len(sides) == 2:
                            zone.setRight(rect.center().x())
                    elif side == "down":
                        zone.setBottom(rect.bottom() + gap)
                    else:
                        zone.setRight(rect.right() + gap)
                        if len(sides) == 2:
                            zone.setLeft(rect.center().x())
                    self.targets.append(
                        _Target(self._proposal(key, len(self.remaining[key]), side), key, "inside", zone)
                    )
                continue
            parent, index = self.parents[key], self.rows[key]
            if not has_submap:
                self.targets.append(
                    _Target(self._proposal(key, len(self.remaining[key])), key, "inside", QRectF(rect))
                )
            for position in ("before", "after"):
                before = position == "before"
                zone = (
                    QRectF(rect.left() if before else rect.right(), rect.top(), 0, rect.height())
                    if self.down
                    else QRectF(rect.left(), rect.top() if before else rect.bottom(), rect.width(), 0)
                )
                self.targets.append(
                    _Target(self._proposal(parent, index + (not before), self.sides[key]), key, position, zone)
                )

    def _local_targets(self, point, scale):
        """保守覆盖吸附半径及排序扩展，按需生成附近节点落点并保留完整大间隙。

        中心主题的空侧落区可能远于吸附半径，因此总是纳入中心；最终仍使用原有
        正文、方向约束和距离判断。恢复原始节点顺序，保持重叠候选的同距离优先级。
        """
        nearby = dict(self._node_index.nearby(point, (ENTER_DISTANCE + SORT_DEPTH) / scale))
        if self.center_id in self.node_regions:
            nearby[self.center_id] = self.node_regions[self.center_id]
        self.targets = []
        self._append_node_targets(
            (key, nearby[key]) for key in sorted(nearby, key=self._region_order.__getitem__)
        )
        self.targets.extend(
            target for target, _ in self._gap_index.nearby(point, (ENTER_DISTANCE + SORT_PADDING) / scale)
        )

    def _sort_region(self, target, scale):
        """仅向节点外扩大排序落区；前后各 24 像素深，横向留 12 像素容错。

        正文之外的同级大间隙保持整段可用；小倍率下扩展区可以相交，但正文始终
        具有最高优先级，排序也必须满足位于参照节点前方或后方的方向约束。
        """
        zone = QRectF(target.region)
        depth, padding = SORT_DEPTH / scale, SORT_PADDING / scale
        if self.down:
            zone.adjust(0, -padding, 0, padding)
            if zone.width() == 0:
                if target.position == "before":
                    zone.setLeft(zone.left() - depth)
                else:
                    zone.setRight(zone.right() + depth)
        else:
            zone.adjust(-padding, 0, padding, 0)
            if zone.height() == 0:
                if target.position == "before":
                    zone.setTop(zone.top() - depth)
                else:
                    zone.setBottom(zone.bottom() + depth)
        return _Target(target.proposal, target.reference, target.position, zone)

    def allows(self, target, point):
        """排序仅允许在正文外的正确一侧，吸附和退出滞回也必须遵守该约束。"""
        if target.position == "inside":
            return True
        rect = self.node_regions.get(target.reference)
        if rect is None:
            original = self.layout.nodes.get(target.reference)
            if original is None:
                return False
            rect = _qrect(original)
        value = point.x() if self.down else point.y()
        leading, trailing = (rect.left(), rect.right()) if self.down else (rect.top(), rect.bottom())
        return value < leading if target.position == "before" else value > trailing

    def _targets_at_scale(self, scale):
        """落点集合和倍率不变时复用扩大落区，不在连续移动中创建成千临时对象。

        这里只缓存固定几何的视口换算，不试算候选文档或布局；手势起点提前建立，
        替换落点集合或改变视口倍率时自动失效。
        """
        if self._expanded_source is not self.targets or self._expanded_scale != scale:
            self._expanded_source, self._expanded_scale = self.targets, scale
            self._expanded_targets = [
                (target, self._sort_region(target, scale) if target.position != "inside" else target)
                for target in self.targets
            ]
            self._inside_targets = defaultdict(list)
            self._center_targets = []
            for target in self.targets:
                if target.position == "inside":
                    self._inside_targets[target.reference].append(target)
                if target.reference == self.center_id:
                    self._center_targets.append(target)
            self._target_index = _RegionIndex(
                (pair, pair[1].region) for pair in self._expanded_targets
            )
        return self._expanded_targets

    def resolve(self, point, scale):
        """优先正文，再选节点外的扩大排序区，最后使用 24 像素距离吸附。

        返回值的第二项表示明确进入某个操作区，可立即切换意图；只有外侧距离
        吸附才沿用退出滞回。拖动影像、短条和文字均不参与此只读命中计算。
        """
        self.blocked_submap = None
        if self._virtual:
            self._virtual_targets(point, scale)
        elif self._gap_index is not None:
            self._local_targets(point, scale)
        self._targets_at_scale(scale)
        direct = next(
            (key for key, rect in self._node_index.nearby(point) if rect.contains(point)),
            None,
        )
        if direct is not None:
            choices = self._inside_targets.get(direct)
            if choices:
                return min(choices, key=lambda target: _distance(point, target.region)), True
            if self.document.nodes[direct].submap_id:
                # 明确落在子导图入口正文时直接拒绝，禁止再吸附到附近的同级插入区。
                self.blocked_submap = direct
                return None, True
        if any(rect.contains(point) for _, rect in self._source_index.nearby(point)):
            return None, True
        # 中心主题向外延伸的一级分支落区保留，包括双向布局尚无主题的一侧。
        center = next(
            (
                target
                for target in self._center_targets
                if target.region.contains(point)
            ),
            None,
        )
        if center is not None:
            return center, True
        candidates, slots, gaps = [], [], []
        for (target, expanded), _ in self._target_index.nearby(point, ENTER_DISTANCE / scale):
            if not self.allows(target, point):
                continue
            candidates.append(expanded)
            if target.position != "inside" and expanded.region.contains(point):
                # 贴近外缘时以最近的主题为参照；大间隙作为后备，避免它覆盖附近的
                # “插到后面”提示，或把标记直接跳到遥远的下一个兄弟主题旁。
                boundary = target.region.width() == 0 if self.down else target.region.height() == 0
                (slots if boundary else gaps).append((target, expanded))
        if slots or gaps:
            _, target = min(slots or gaps, key=lambda pair: _distance(point, pair[0].region))
            return target, True
        best = min(candidates, key=lambda target: _distance(point, target.region), default=None)
        if best is not None and _distance(point, best.region) * scale <= ENTER_DISTANCE:
            return best, False
        return None, False

    def _virtual_targets(self, point, scale):
        """从冻结几何查询指针邻域及相邻同级间隙，跨屏拖动也无需重建整图索引。"""
        geometry = self.geometry
        radius = (ENTER_DISTANCE + SORT_DEPTH + SORT_PADDING) / scale
        area = Rect(point.x() - radius, point.y() - radius, radius * 2, radius * 2)
        candidates = geometry.query_with_sibling_neighbors(area, skipped_ids=self.moving)
        center = self.layout.nodes.get(self.center_id)
        if center is not None:
            candidates[self.center_id] = center
        self.source_rects = [_qrect(rect) for key, rect in candidates.items() if key in self.moving]
        self._source_index = _RegionIndex(enumerate(self.source_rects))
        keys = sorted((key for key in candidates if key not in self.moving), key=geometry.layout_order)
        self.node_regions = {key: _qrect(candidates[key]) for key in keys}
        self._node_index = _RegionIndex(self.node_regions.items())
        self.targets = []
        self._append_node_targets(self.node_regions.items())
        for parent, previous, following in geometry.adjacent_sibling_pairs(keys, skipped_ids=self.moving):
            a, b = self.node_regions[previous], self.node_regions[following]
            zone = (QRectF(a.right(), min(a.top(), b.top()), b.left() - a.right(),
                           max(a.bottom(), b.bottom()) - min(a.top(), b.top())) if self.down else
                    QRectF(min(a.left(), b.left()), a.bottom(),
                           max(a.right(), b.right()) - min(a.left(), b.left()), b.top() - a.bottom()))
            if zone.width() > 0 and zone.height() > 0:
                self.targets.append(_Target(self._proposal(parent, self.rows[following], self.sides[following]),
                                            following, "before", zone))
