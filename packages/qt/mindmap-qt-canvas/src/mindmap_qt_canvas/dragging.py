"""节点拖动的内部控制器：固定目标、吸附提示、放下后重排和边缘滚动。

拖动期间只读起点快照，不移动其他节点，也不展开折叠主题。松手后才独立试算、
提交一次事务，并对正式布局播放动画；动画不改写文档、历史或导出几何。
"""

from __future__ import annotations

from collections import ChainMap, defaultdict
from dataclasses import dataclass, replace
from math import ceil, floor, hypot

from mindmap_core import BranchPath, Curve, LayoutResult, MindMapDocument, MindMapEngine, Rect
from mindmap_qt_render.rendering import paint_branch
from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem

ENTER_DISTANCE = 24.0
LEAVE_DISTANCE = 36.0
ANIMATION_DURATION = 140
# 千节点实测单帧已超过 16 毫秒；大图直接展示最终布局，避免动画阻塞后续输入。
ANIMATION_NODE_LIMIT = 500
# 小图预建全部候选；大图按指针附近的固定几何生成，避免开始拖动时分配数万落点。
EAGER_TARGET_LIMIT = 500
SCROLL_MARGIN = 32.0
SCROLL_SPEED = 480.0
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


@dataclass
class _DragSnapshot:
    """只读手势起点；图元内容和命中区域在整次拖动中均以它为准。"""

    document: MindMapDocument
    visuals: dict
    layout: LayoutResult


@dataclass
class _PreparedDrop:
    """先结束鼠标捕获，再执行正式命令；保留手势起点快照用于方向与空操作判定。"""

    proposal: _DropProposal
    document: MindMapDocument
    selection: list[str]
    content_version: int
    focus_id: str | None
    start_nodes: dict[str, Rect]


@dataclass
class _Reflow:
    """提交后的纯展示状态，与拖动影像分开管理，允许正式刷新随时结束动画。"""

    layout: LayoutResult
    start_nodes: dict[str, Rect]
    parents: dict[str, str]


def _shift_branch(branch: Curve | BranchPath, parent_delta, child_delta):
    """按父子位移变形最终连线，保留注入布局及现有分支样式的路径语义。

    动画中父子矩形可以重叠或穿过彼此，不能再次调用要求合法树几何的正式路由。
    改为沿最终出线方向插值两端位移：端点跟随节点，控制点平滑过渡，箭头整体
    跟随子节点。结束时直接使用原始布局路径，避免累计误差或改写正式布局。
    """

    def shift(point, weight):
        return (
            point[0] + parent_delta[0] * (1 - weight) + child_delta[0] * weight,
            point[1] + parent_delta[1] * (1 - weight) + child_delta[1] * weight,
        )

    if isinstance(branch, Curve):
        return replace(
            branch, points=tuple(shift(point, index / 3) for index, point in enumerate(branch.points))
        )
    start, end = branch.start, branch.end
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = dx * dx + dy * dy

    def warp(point):
        weight = (
            max(0, min(1, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length))
            if length
            else 0.5
        )
        return shift(point, weight)

    return replace(
        branch,
        start=shift(start, 0),
        end=shift(end, 1),
        commands=tuple(
            replace(command, points=tuple(map(warp, command.points))) for command in branch.commands
        ),
        arrow=tuple(shift(point, 1) for point in branch.arrow) if branch.arrow else None,
    )


def _changes(document: MindMapDocument, proposal: _DropProposal) -> bool:
    """先比较结构及有效方向，再固化自动方向，避免空拖动制造历史记录。"""
    roots = set(proposal.roots)
    parent = document.nodes[proposal.parent_id]
    children = [key for key in parent.children if key not in roots]
    children[proposal.index : proposal.index] = proposal.roots
    if children != parent.children:
        return True
    if document.layout.direction == "both" and proposal.parent_id == document.root_id:
        sides = _root_sides(document)
        return any(sides[key] != proposal.direction for key in proposal.roots)
    return False


def _root_sides(document):
    """只读取一级分支的有效方向，固定自动方向时不需要遍历每棵子树。"""
    counts, result = {"left": 0, "right": 0}, {}
    for key in document.root.children:
        side = document.nodes[key].direction or min(counts, key=counts.get)
        counts[side] += 1
        result[key] = side
    return result


def _apply_move(engine: MindMapEngine, proposal: _DropProposal, document: MindMapDocument) -> bool:
    """在放下试算和正式引擎上执行相同事务，方向、排序和展开状态一起提交或回滚。

    一级分支自动方向与根的子节点顺序相关。先固定未移动一级分支的原有效方向，
    再移动并设置目标方向，避免被移动分支带动其他分支换边。现有引擎的 before/after
    会继承参照分支方向，因此移动根的最终方向必须在 move_nodes 之后设置。
    """
    if not _changes(document, proposal):
        return False
    moving = set(proposal.roots)
    siblings = [key for key in document.nodes[proposal.parent_id].children if key not in moving]
    both = document.layout.direction == "both"
    touches_root = proposal.parent_id == document.root_id or any(
        engine.parent_id(key) == document.root_id for key in proposal.roots
    )
    with engine.transaction("拖动节点"):
        if both and touches_root:
            sides = _root_sides(document)
            for key in document.root.children:
                if key not in moving and document.nodes[key].direction != sides[key]:
                    engine.update_node(key, direction=sides[key])
        if proposal.index < len(siblings):
            engine.move_nodes(proposal.roots, siblings[proposal.index], "before")
        elif siblings:
            engine.move_nodes(proposal.roots, siblings[-1], "after")
        else:
            engine.move_nodes(proposal.roots, proposal.parent_id, "inside")
        # move_nodes 在同一事务中展开目标父节点；悬停不运行此流程，因此折叠主题
        # 只在成功放下后展开。聚焦范围以外的折叠祖先仍保持原样。
        if both and proposal.parent_id == document.root_id:
            for key in proposal.roots:
                engine.update_node(key, direction=proposal.direction)
    return True


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

    def __init__(self, document, layout, selection, focus_id):
        self.document = document
        self.layout = layout
        self.geometry = getattr(layout, "geometry", None)
        self._virtual = self.geometry is not None and len(layout.nodes) > 1000
        self.parents = self.geometry.parents if self._virtual else document.parents()
        selected = set(selection)
        # 显式遍历并跳过已选祖先的后代，同时保留文档先序顺序。
        roots = []
        if self._virtual:
            for key in selected:
                ancestor = self.parents.get(key)
                while ancestor is not None and ancestor not in selected:
                    ancestor = self.parents.get(ancestor)
                if ancestor is None:
                    roots.append(key)
            roots.sort(key=self.geometry.order)
        else:
            stack = [document.root_id]
            while stack:
                key = stack.pop()
                if key in selected:
                    roots.append(key)
                else:
                    stack.extend(reversed(document.nodes[key].children))
        self.roots = tuple(roots)
        self.moving = (_MovingSubtrees(roots, self.parents) if self._virtual else
                       {key for root in roots for key in document.descendants(root)})
        self.center_id = focus_id or document.root_id
        self.valid = bool(roots) and not {document.root_id, self.center_id}.intersection(roots)
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
            if key == self.center_id:
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



class _DragLayer(QGraphicsItem):
    """只绘制源分支影像及落点提示；没有鼠标或键盘能力，不参与节点命中。"""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._bounds = QRectF()
        self._label_bounds = QRectF()
        self._label_sizes = {}
        self._label_font = QFont(controller.view.font())
        self._label_font.setPixelSize(12)
        self._label_font.setWeight(QFont.Weight.DemiBold)
        self._label_metrics = QFontMetricsF(self._label_font)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(40)

    def boundingRect(self):
        return self._bounds

    def sync(self):
        control = self.controller
        bounds = control._ghost_bounds.translated(control._offset)
        if control.target is not None:
            rect = control._base.layout.nodes.get(control.target.reference)
            if rect is not None:
                bounds = bounds.united(_qrect(rect))
            bounds = bounds.united(control._connection_hint().boundingRect())
            bounds = bounds.united(control._insertion_hint())
        self._sync_label()
        bounds = bounds.united(self._label_bounds)
        # 提示线使用屏幕固定线宽，低倍率时把整条提示线的场景留白计入重绘范围。
        padding = max(4, 12 / control.view.transform().m11())
        self.prepareGeometryChange()
        self._bounds = bounds.adjusted(-padding, -padding, padding, padding)
        self.update()

    def _sync_label(self):
        """按视口逻辑像素放置文字气泡，贴近鼠标并在视口边缘翻向，始终保持可读。

        气泡仅属于绘制图层，不是可命中的按钮；大小不随导图缩放改变。命中仍由
        固定节点和节点外间隙决定，避免气泡覆盖画面时反过来改变当前落点。
        """
        control = self.controller
        label = control._hint_text()
        if not label:
            self._label_bounds = QRectF()
            return
        if label not in self._label_sizes:
            self._label_sizes[label] = (
                ceil(self._label_metrics.horizontalAdvance(label)) + 20,
                ceil(self._label_metrics.height()) + 12,
            )
        width, height = self._label_sizes[label]
        viewport = control.view.viewport().rect()
        point = control._pointer
        left, top = point.x() + 16, point.y() + 20
        if left + width > viewport.right() - 8:
            left = point.x() - width - 16
        if top + height > viewport.bottom() - 8:
            top = point.y() - height - 16
        left = max(8, min(left, viewport.right() - width - 8))
        top = max(8, min(top, viewport.bottom() - height - 8))
        scale = control.view.transform().m11()
        origin = control.view.mapToScene(QPoint(left, top))
        self._label_bounds = QRectF(origin.x(), origin.y(), width / scale, height / scale)

    def paint(self, painter, option, widget=None):
        control = self.controller
        painter.save()
        # 吸附后降低影像浓度，避免源分支遮住正在选择的目标文字。
        painter.setOpacity(0.45 if control.target else 0.72)
        painter.translate(control._offset)
        for branch in control._ghost_branches:
            paint_branch(painter, branch)
        for key, rect in control._ghost_nodes.items():
            painter.save()
            painter.translate(rect.x, rect.y)
            control._base.visuals[key].paint(painter, control._base.document.nodes[key], controls=False)
            painter.restore()
        painter.restore()
        target = control.target
        rect = control._base.layout.nodes.get(target.reference) if target else None
        if rect is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(control._base.document.theme.accent)
        pen = QPen(color, 2)
        pen.setCosmetic(True)
        if target.position == "inside":
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(control._connection_hint())
        # 影像可能正好盖住落点正文。把固定目标绘制在影像与提示线之上，使名称和
        # 正文始终清晰；预览不绘制交互按钮，也不移动或改写实际节点图元。
        painter.save()
        painter.translate(rect.x, rect.y)
        control._base.visuals[target.reference].paint(
            painter, control._base.document.nodes[target.reference], controls=False
        )
        painter.restore()
        if target.position == "inside":
            # 接为子节点强调接收主题，整圈淡色高亮与预连接共同表达父子关系。
            pen.setStyle(Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            fill = QColor(color)
            fill.setAlpha(30)
            painter.setBrush(fill)
            painter.drawRoundedRect(_qrect(rect).adjusted(-3, -3, 3, 3), 8, 8)
        else:
            # 同级排序只标出插入位置，不叠加目标框或预连接；实心圆角短条与正文
            # 留出空隙，避免旧的长线贴边形成双层边框。全部提示均不参与命中。
            marker = control._insertion_hint()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            radius = min(marker.width(), marker.height()) / 2
            painter.drawRoundedRect(marker, radius, radius)
        painter.restore()
        # 用反向倍率绘制固定字号的文字与气泡；标签给出松手后的准确操作，不能
        # 随 10%～400% 导图缩放变成不可读的小字或遮挡大片内容的大字。
        painter.save()
        painter.translate(self._label_bounds.topLeft())
        scale = control.view.transform().m11()
        painter.scale(1 / scale, 1 / scale)
        badge = QRectF(0, 0, self._label_bounds.width() * scale, self._label_bounds.height() * scale)
        painter.setPen(QPen(color, 1))
        painter.setBrush(QColor(control._base.document.theme.background))
        painter.drawRoundedRect(badge, 7, 7)
        painter.setFont(self._label_font)
        painter.setPen(color)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, control._hint_text())
        painter.restore()


class NodeDragController(QObject):
    """画布内部的单次拖动状态机；结束和取消都幂等，所有定时器由视图拥有。"""

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.owner = view.owner
        self.ghost = None
        self.target = None
        self._base = None
        self._resolver = None
        self._reflow = None
        self._pointer = QPoint()
        self._last_scene_point = None
        self._last_scale = None
        self._offset = QPointF()
        self._display_nodes = {}
        self._content_version = 0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(ANIMATION_DURATION)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._animate)
        self._animation.finished.connect(self._settle_reflow)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._scroll)
        self._scroll_clock = QElapsedTimer()
        self._scroll_remainder = QPointF()

    @property
    def active(self):
        return self.ghost is not None

    def begin(self, origin):
        """越过系统阈值后冻结源几何与选择；只读、根节点和聚焦中心在此再次校验。"""
        if not self.owner.editable or self.owner.editor is not None or self.owner._disposed:
            return False
        # 下一次操作开始前收束上次放下动画，避免拿中间帧作为新的命中几何。
        self._settle_reflow()
        # 组件持有的正式展示文档本身就是引擎快照；正式内容改变时会先中止手势，
        # 因而可以只读复用它，保持未变化节点、视觉和图元之间的对象一致性。
        document = self.owner._document
        selection = self.owner.selected_node_ids()
        resolver = _DropResolver(document, self.owner.layout_result, selection, self.owner.focus_id)
        if not resolver.valid:
            return False
        self.owner._cancel_branch_menu()
        # 正式图元已经持有测量结果，拖动影像只需要可见节点，无需再次测量整图。
        visuals = {key: item.visual for key, item in self.owner.node_items.items()}
        self._base = _DragSnapshot(document, visuals, self.owner.layout_result)
        self._selection = selection
        self._resolver = resolver
        self._resolver.rebuild(self._base.layout.nodes, scale=self.view.transform().m11())
        self._origin = QPointF(origin)
        self._last_scene_point = self._last_scale = None
        self._ghost_nodes = {
            key: self._base.layout.nodes[key] for key in self.owner.node_items if key in resolver.moving
        }
        self._ghost_branches = [
            _shift_branch(item._branch, (item.pos().x(), item.pos().y()), (item.pos().x(), item.pos().y()))
            for key, item in self.owner.branch_items.items()
            if key in resolver.moving and resolver.parents.get(key) in resolver.moving
        ]
        self._ghost_bounds = QRectF()
        for rect in self._ghost_nodes.values():
            self._ghost_bounds = self._ghost_bounds.united(_qrect(rect))
        self._offset = QPointF()
        self.ghost = _DragLayer(self)
        self.ghost.sync()
        self.owner.scene.addItem(self.ghost)
        self._dim_sources()
        return True

    def _dim_sources(self):
        """原位置保留淡色源分支作为参照，其他主题和连线完全保持原貌。"""
        for key in self.owner.node_items:
            if key in self._resolver.moving:
                self.owner.node_items[key].setOpacity(0.18)
        for key in self.owner.branch_items:
            if key in self._resolver.moving:
                self.owner.branch_items[key].setOpacity(0.12)

    def _connection_hint(self):
        """仅为接入子节点绘制预连接线，同级排序由独立的间隙短条表达。"""
        path = QPainterPath()
        target = self.target
        if target is None or target.position != "inside":
            return path
        nodes = self._base.layout.nodes
        parent = nodes.get(target.proposal.parent_id)
        if parent is None:
            return path
        side = target.proposal.direction or self._resolver.sides[target.proposal.parent_id]
        start = QPointF(
            parent.x if side == "left" else parent.right if side == "right" else parent.center[0],
            parent.bottom if side == "down" else parent.center[1],
        )
        for key in self._resolver.roots:
            rect = self._ghost_nodes.get(key)
            if rect is None:
                continue
            end = (
                QPointF(
                    rect.right if side == "left" else rect.x if side == "right" else rect.center[0],
                    rect.y if side == "down" else rect.center[1],
                )
                + self._offset
            )
            path.moveTo(start)
            if side == "down":
                middle = (start.y() + end.y()) / 2
                path.cubicTo(QPointF(start.x(), middle), QPointF(end.x(), middle), end)
            else:
                middle = (start.x() + end.x()) / 2
                path.cubicTo(QPointF(middle, start.y()), QPointF(middle, end.y()), end)
        return path

    def _hint_text(self):
        """直接描述当前落点的结构结果，提示方向与布局排序轴一致。"""
        if self.target is None:
            return ""
        if self.target.position == "inside":
            return "＋ 作为子主题"
        if self._resolver.down:
            return "← 插到左侧" if self.target.position == "before" else "→ 插到右侧"
        return "↑ 插到前面" if self.target.position == "before" else "↓ 插到后面"

    def _insertion_hint(self):
        """返回同级插入短条的场景矩形；位置与命中解耦，不移动正式节点。

        默认使用 40×3 个视口逻辑像素，与目标边缘保留 8 像素空隙。缩小时按
        主题宽度收短；间隙不足时居中并减薄，避免压到相邻主题。双向布局只考虑
        同侧兄弟，向下布局把排序轴转为水平方向，提示短条相应转为竖向。
        """
        target = self.target
        if target is None or target.position == "inside":
            return QRectF()
        nodes = self._base.layout.nodes
        rect = nodes[target.reference]
        scale = self.view.transform().m11()
        down = self._resolver.down
        before = target.position == "before"
        thickness = 3 / scale
        clearance = 8 / scale
        siblings = self._resolver.remaining[target.proposal.parent_id]
        index = siblings.index(target.reference)
        neighbors = reversed(siblings[:index]) if before else iter(siblings[index + 1 :])
        side = self._resolver.sides[target.reference]
        neighbor = next(
            (nodes[key] for key in neighbors if key in nodes and self._resolver.sides[key] == side), None
        )
        edge = (rect.x if before else rect.right) if down else (rect.y if before else rect.bottom)
        if neighbor is not None:
            other = (
                (neighbor.right if before else neighbor.x)
                if down
                else (neighbor.bottom if before else neighbor.y)
            )
            gap = edge - other if before else other - edge
            if gap > 0:
                thickness = min(thickness, gap / 2)
                clearance = min(clearance, (gap - thickness) / 2)
        axis = edge + (-1 if before else 1) * (clearance + thickness / 2)
        span = rect.height if down else rect.width
        length = max(thickness * 2, min(40 / scale, span * 0.6))
        if down:
            return QRectF(axis - thickness / 2, rect.center[1] - length / 2, thickness, length)
        return QRectF(rect.center[0] - length / 2, axis - thickness / 2, length, thickness)

    def update(self, position):
        """只更新稳定落点、影像和滚动；悬停不试算文档、不测量、不启动布局动画。"""
        if not self.active:
            return
        point = self.view.mapToScene(position)
        scale = self.view.transform().m11()
        moved = position != self._pointer or point != self._last_scene_point or scale != self._last_scale
        self._pointer = QPoint(position)
        self._offset = point - self._origin
        inside = self.view.viewport().rect().contains(position)
        current = self.target
        if not moved:
            # 重复移动及释放使用同一命中；滚动或缩放则按新场景坐标和倍率重算。
            candidate, direct = current, False
        else:
            candidate, direct = self._resolver.resolve(point, scale) if inside else (None, True)
            blocked = getattr(self._resolver, "blocked_submap", None) if inside else None
            if blocked is not None and blocked != getattr(self, "_blocked_submap", None):
                self.owner.error_occurred.emit("此节点已有子导图，请进入子导图添加内容")
            self._blocked_submap = blocked
        # 明确进入正文或排序区时立即按当前意图切换；只有离开所有明确落区后的
        # 外侧吸附使用 36 像素退出距离。旧排序锁定不能跨进正文继续提交同级移动。
        if (
            current is not None
            and not direct
            and self._resolver.allows(current, point)
            and _distance(point, current.region) * scale <= LEAVE_DISTANCE
        ):
            candidate = current
        if candidate is not None and not _changes(self._base.document, candidate.proposal):
            candidate = None
        self.target = candidate
        self._offset = self.view.mapToScene(position) - self._origin
        self._last_scene_point = self.view.mapToScene(position)
        self._last_scale = scale
        self.ghost.sync()
        if inside and self._velocity() != QPointF():
            if not self._scroll_timer.isActive():
                self._scroll_remainder = QPointF()
                self._scroll_clock.start()
                self._scroll_timer.start()
        else:
            self._scroll_timer.stop()

    def _start_reflow(self, start_nodes):
        """正式提交和选择恢复结束后播放一次动画，命中与文档均使用最终布局。

        已存在的其他节点从原位置出发，移动子树从释放时的分支影像位置出发。
        折叠主题放下后才产生的新图元，从父节点当前位置附近出现并一起移动。
        """
        layout = self.owner.layout_result
        if len(layout.nodes) > ANIMATION_NODE_LIMIT:
            return
        parents = self.owner._document.parents()
        starts = {}
        bounds = layout.bounds
        # 按树顺序处理新显露的节点，确保其父节点起点已知，不依赖注入布局的字典顺序。
        for key in self.owner._document.descendants(self.owner._document.root_id):
            if key not in layout.nodes:
                continue
            rect = layout.nodes[key]
            origin = start_nodes.get(key)
            if origin is None:
                parent = parents.get(key)
                if parent in starts:
                    before, after = starts[parent], layout.nodes[parent]
                    origin = Rect(
                        rect.x + before.x - after.x, rect.y + before.y - after.y, rect.width, rect.height
                    )
                else:
                    origin = rect
            starts[key] = Rect(origin.x, origin.y, rect.width, rect.height)
            bounds = bounds.union(starts[key])
        self._reflow = _Reflow(layout, starts, parents)
        center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self.owner.scene.setSceneRect(_qrect(bounds))
        self.view.update_navigation_rect(center=center)
        self._animate(0.0)
        self._animation.start()

    def _animate(self, progress):
        if self._reflow is None:
            return
        reflow = self._reflow
        self._display_nodes = {
            key: Rect(
                reflow.start_nodes[key].x + (rect.x - reflow.start_nodes[key].x) * progress,
                reflow.start_nodes[key].y + (rect.y - reflow.start_nodes[key].y) * progress,
                rect.width,
                rect.height,
            )
            for key, rect in reflow.layout.nodes.items()
        }
        for key, rect in self._display_nodes.items():
            self.owner.node_items[key].setPos(rect.x, rect.y)

        def delta(key):
            before, after = self._display_nodes[key], reflow.layout.nodes[key]
            return before.x - after.x, before.y - after.y

        branches = (
            reflow.layout.branches
            if progress >= 1
            else {
                key: _shift_branch(branch, delta(reflow.parents[key]), delta(key))
                for key, branch in reflow.layout.branches.items()
            }
        )
        self.owner._set_branch_items(LayoutResult(branches=branches))

    def _settle_reflow(self):
        """动画完成或被其他操作打断时收束到正式几何，不撤销已经成功提交的移动。"""
        self._animation.stop()
        reflow, self._reflow = self._reflow, None
        if reflow is None or self.owner._disposed:
            return
        for key, rect in reflow.layout.nodes.items():
            if key in self.owner.node_items:
                self.owner.node_items[key].setPos(rect.x, rect.y)
        self.owner._set_branch_items(reflow.layout)
        center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self.owner.scene.setSceneRect(_qrect(reflow.layout.bounds))
        self.view.update_navigation_rect(center=center)
        self._display_nodes = {}

    def _velocity(self):
        """边缘速度以视口逻辑像素/秒计，Qt 已处理设备像素比，不乘显示缩放。"""
        rect = self.view.viewport().rect()
        if not rect.contains(self._pointer):
            return QPointF()

        def component(value, size):
            if value < SCROLL_MARGIN:
                return -SCROLL_SPEED * (1 - value / SCROLL_MARGIN)
            if value > size - SCROLL_MARGIN:
                return SCROLL_SPEED * (1 - (size - value) / SCROLL_MARGIN)
            return 0.0

        return QPointF(
            component(self._pointer.x(), rect.width()), component(self._pointer.y(), rect.height())
        )

    def _scroll(self):
        if not self.active:
            self._scroll_timer.stop()
            return
        # 事件队列偶尔被其他操作阻塞时限制单步时间，防止恢复后突然跳过大片内容。
        elapsed = min(self._scroll_clock.restart(), 50) / 1000
        self._scroll_remainder += self._velocity() * elapsed
        for horizontal, amount in ((True, self._scroll_remainder.x()), (False, self._scroll_remainder.y())):
            distance = int(amount)
            bar = self.view.horizontalScrollBar() if horizontal else self.view.verticalScrollBar()
            bar.setValue(bar.value() + distance)
            if horizontal:
                self._scroll_remainder.setX(amount - distance)
            else:
                self._scroll_remainder.setY(amount - distance)
        self.update(self._pointer)

    def leave(self):
        """离开视口只停止边缘滚动；鼠标捕获继续允许在外部释放并取消。"""
        self._scroll_timer.stop()
        self._pointer = QPoint(-int(SCROLL_MARGIN), -int(SCROLL_MARGIN))

    def document_changed(self):
        """记录正式内容变化并中断手势，纯保存通知不调用此方法。

        释放场景捕获可能同步触发宿主回调。版本号同时保护已经取出的落点命令，
        防止捕获释放到正式提交之间发生的文档替换或编辑被旧手势继续操作。
        """
        self._content_version += 1
        self.view.cancel_interaction()

    def finish(self, position):
        """按释放坐标再次判定并记录动画起点，配对释放鼠标捕获后才提交命令。"""
        self.update(position)
        drop = None
        if self.active and self.target is not None:
            drop = _PreparedDrop(
                self.target.proposal,
                self._base.document,
                self._selection,
                self._content_version,
                self.owner.focus_id,
                {
                    key: Rect(
                        rect.x + (self._offset.x() if key in self._resolver.moving else 0),
                        rect.y + (self._offset.y() if key in self._resolver.moving else 0),
                        rect.width,
                        rect.height,
                    )
                    for key, rect in self._base.layout.nodes.items()
                } if len(self._base.layout.nodes) <= ANIMATION_NODE_LIMIT else {},
            )
        self.cancel()
        return drop

    def commit(self, drop):
        """放下后独立校验布局，再一次提交和播放重排；钩子拒绝时保留正式状态。

        试算不挂真实引擎的操作钩子，也不展示候选文档。布局扩展拒绝结果时不会先
        污染正式文档。真实事务同步刷新画布并恢复选择，动画必须在这些回调后启动。
        """

        def valid():
            return (
                not self.owner._disposed
                and self.owner.editable
                and drop.content_version == self._content_version
                and drop.focus_id == self.owner.focus_id
            )

        if not valid() or not _changes(drop.document, drop.proposal):
            return False
        if not self.owner._can_incremental() or self.owner._incremental_layout is None:
            # 第三方布局可能拒绝合法结构，保留独立候选及原有隔离契约。
            scratch = MindMapEngine(self.owner.engine.document)
            changed_ids = []
            scratch.subscribe(lambda event: changed_ids.extend(event.node_ids))
            _apply_move(scratch, drop.proposal, drop.document)
            candidate = scratch.page_document(self.owner.current_page_id)
            _, sizes = self.owner.factory.measure(
                candidate, changed_node_ids=changed_ids,
                previous_document=drop.document, previous_visuals=self.owner._visuals,
            )
            self.owner.layout_registry.layout(candidate, sizes, focus_id=drop.focus_id)
        else:
            # 受控树布局只试算变化实体及祖先。候选在原引擎的回滚日志内执行，
            # 不建立临时引擎或整图副本，也不提前执行宿主钩子和发布历史事件。
            with self.owner.engine.preview_operation() as (candidate, touched):
                _apply_move(self.owner.engine, drop.proposal, drop.document)
                if self.owner.current_page_id is not None or candidate.submaps:
                    candidate = candidate.page_view(self.owner.current_page_id)
                changed_sizes = {}
                for key in touched:
                    node = candidate.nodes.get(key)
                    if node is not None:
                        visual = self.owner.factory.visual(node, candidate.theme, candidate,
                                                           is_root=key == candidate.root_id)
                        changed_sizes[key] = visual.width, visual.height
                geometry = self.owner._incremental_layout.geometry.snapshot()
                geometry.sync(candidate, ChainMap(changed_sizes, self.owner._measurements), touched)
                _ = geometry.bounds
        # 外部布局或捕获释放的宿主回调有可能修改文档，不能让旧命令覆盖新内容。
        if not valid():
            return False
        try:
            changed = _apply_move(self.owner.engine, drop.proposal, drop.document)
        except Exception:
            if valid():
                self.owner.select_nodes(drop.selection)
            raise
        if (
            changed
            and not self.owner._disposed
            and self.owner.editable
            and self._content_version == drop.content_version + 1
            and self.owner.focus_id == drop.focus_id
        ):
            self.owner.select_nodes(drop.selection)
            # 选择信号也可触发宿主编辑，重新检查后才允许借用当前正式图元播放动画。
            if (
                not self.owner._disposed
                and self.owner.editable
                and self._content_version == drop.content_version + 1
                and self.owner.focus_id == drop.focus_id
            ):
                self._start_reflow(drop.start_nodes)
        return changed

    def cancel(self):
        """幂等取消未提交手势；提交后动画只收束到终点，不回滚文档或折叠状态。"""
        self._settle_reflow()
        self._scroll_timer.stop()
        if not self.active:
            return
        layer, self.ghost = self.ghost, None
        self.owner.scene.removeItem(layer)
        self.target = None
        self._blocked_submap = None
        if not self.owner._disposed:
            # 拖动期间只改变源图元透明度，取消无需重建图元、重排或恢复折叠快照。
            for key in self.owner.node_items:
                if key in self._resolver.moving:
                    self.owner.node_items[key].setOpacity(1)
            for key in self.owner.branch_items:
                if key in self._resolver.moving:
                    self.owner.branch_items[key].setOpacity(1)
        self._display_nodes = {}
        self._base = self._resolver = None
        self._ghost_nodes = self._ghost_branches = None
