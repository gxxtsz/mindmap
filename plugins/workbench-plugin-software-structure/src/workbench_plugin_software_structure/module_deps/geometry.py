"""按节点尺寸布置层内坐标，并沿虚拟顶点绘制避开节点的样条。"""

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

from PySide6.QtCore import QPointF, QRectF, QSizeF
from PySide6.QtGui import QPainterPath, QPolygonF

from ..document import ModuleDependency
from .constraints import SpanConstraint, minimize_spans
from .layout import LayeredLayout, build_layout

_RANK_GAP = 46.0
_NODE_GAP = 22.0
_LANE_GAP = 7.0
_GROUP_PADDING = 18.0
_GROUP_HEADER = 28.0
_COORDINATE_SCALE = 2
_SINGLE_MODULE_ALIGNMENT_WEIGHT = 64


@dataclass(frozen=True, slots=True)
class GraphGeometry:
    """Qt 画布所需的节点矩形、依赖曲线与箭头，以及可选的公共依赖分组。"""

    boxes: dict[str, QRectF]
    edges: dict[ModuleDependency, tuple[QPainterPath, QPolygonF]]
    public_bounds: QRectF | None = None


def _align_layer(
    layer: tuple[int, ...], offsets: list[float], positions: list[float], layout: LayeredLayout,
) -> None:
    """在保持层内顺序和最小间隔的前提下，尽量向相邻层的节点靠拢。

    减去每个节点的最小间距前缀后，剩余位移只需单调不减。若两段位移相互
    挤压，则合并为同一加权平均值。虚拟节点之间的连接权重更高，长线更直。
    """
    blocks: list[tuple[float, float, int]] = []
    module_count = len(layout.modules)
    for vertex, offset in zip(layer, offsets):
        weight = 0.25
        total = positions[vertex] * weight
        for neighbor in (*layout.previous[vertex], *layout.following[vertex]):
            strength = 6.0 if vertex >= module_count and neighbor >= module_count else 1.0
            weight += strength
            total += positions[neighbor] * strength
        blocks.append((total - offset * weight, weight, 1))
        while len(blocks) > 1:
            left, right = blocks[-2], blocks[-1]
            if left[0] / left[1] <= right[0] / right[1]:
                break
            blocks.pop()
            blocks[-1] = left[0] + right[0], left[1] + right[1], left[2] + right[2]
    index = 0
    for total, weight, count in blocks:
        shift = total / weight
        for _ in range(count):
            positions[layer[index]] = offsets[index] + shift
            index += 1


def _minimum_gap(left: int, right: int, half_sizes: list[float], module_count: int) -> float:
    """按实际模块尺寸或虚拟走线位置，计算层内相邻点的最小中心间距。"""
    gap = _NODE_GAP if left < module_count and right < module_count else _LANE_GAP
    return half_sizes[left] + gap + half_sizes[right]


def _optimize_cross_positions(
    layout: LayeredLayout, half_sizes: list[float], positions: list[float],
) -> list[float]:
    """在层内间距和图宽约束下，全局缩短连线在横向的偏移。

    每段连线引入一个辅助点。该点的坐标不超过两端，所以优化会把它移到两端较小值，
    此时两条辅助边的总跨度等于端点间距。虚拟节点之间权重更高，优先拉直跨层边。
    """
    vertex_count = len(positions)
    module_count = len(layout.modules)
    initial = [round(position * _COORDINATE_SCALE) for position in positions]
    extent = [math.ceil(size * _COORDINATE_SCALE) for size in half_sizes]
    constraints = []
    single_modules = set()
    for layer in layout.layers:
        modules_in_layer = [vertex for vertex in layer if vertex < module_count]
        if len(modules_in_layer) == 1:
            single_modules.add(modules_in_layer[0])
        for left, right in pairwise(layer):
            gap = math.ceil(_minimum_gap(left, right, half_sizes, module_count) * _COORDINATE_SCALE)
            initial[right] = max(initial[right], initial[left] + gap)
            constraints.append(SpanConstraint(left, right, gap, 1))

    # 给所有层共享一对边界，允许整体收紧，但避免以放大画布为代价拉直一条线。
    left_bound, right_bound = vertex_count, vertex_count + 1
    initial.extend((min(value - half for value, half in zip(initial, extent)),
                    max(value + half for value, half in zip(initial, extent))))
    width = initial[right_bound] - initial[left_bound]
    constraints.extend((SpanConstraint(left_bound, right_bound, 0, 1),
                        SpanConstraint(right_bound, left_bound, -width)))
    for layer in layout.layers:
        if layer:
            first, last = layer[0], layer[-1]
            constraints.extend((SpanConstraint(left_bound, first, extent[first]),
                                SpanConstraint(last, right_bound, extent[last])))

    for source, following in enumerate(layout.following):
        for target in following:
            virtual_ends = int(source >= module_count) + int(target >= module_count)
            weight = (4, 8, 32)[virtual_ends]
            # 连续两个各只有一个真实模块的层形成明确的阅读顺序。优先对齐这对模块，
            # 免得大量穿过中间层的长边把两个节点分别拉到画布两侧。
            if source in single_modules and target in single_modules:
                weight = _SINGLE_MODULE_ALIGNMENT_WEIGHT
            auxiliary = len(initial)
            initial.append(min(initial[source], initial[target]))
            constraints.extend((SpanConstraint(auxiliary, source, 0, weight),
                                SpanConstraint(auxiliary, target, 0, weight)))

    optimized = minimize_spans(initial, constraints)
    result = [coordinate / _COORDINATE_SCALE for coordinate in optimized[:vertex_count]]
    center = (optimized[left_bound] + optimized[right_bound]) / (2 * _COORDINATE_SCALE)
    for layer in layout.layers:
        if layer and not any(layout.previous[vertex] or layout.following[vertex] for vertex in layer):
            # 无连线的分组有整体平移自由度，居中摆放比贴住求解边界更容易阅读。
            first, last = layer[0], layer[-1]
            shift = center - (result[first] - half_sizes[first] + result[last] + half_sizes[last]) / 2
            for vertex in layer:
                result[vertex] += shift
    return result


def _cross_positions(layout: LayeredLayout, half_sizes: list[float]) -> list[float]:
    """先生成间距可行的初始坐标，再整体对齐相邻层的节点和走线位置。"""
    positions = [0.0] * len(half_sizes)
    offsets_by_layer = []
    module_count = len(layout.modules)
    for layer in layout.layers:
        offsets = [0.0]
        for left, right in pairwise(layer):
            offsets.append(offsets[-1] + _minimum_gap(left, right, half_sizes, module_count))
        offsets_by_layer.append(offsets)
        if layer:
            middle = (offsets[-1] + half_sizes[layer[-1]] - half_sizes[layer[0]]) / 2
            for vertex, offset in zip(layer, offsets):
                positions[vertex] = offset - middle
    rows = tuple(zip(layout.layers, offsets_by_layer))
    for traversal in (rows, rows[::-1], rows, rows[::-1]):
        for layer, offsets in traversal:
            _align_layer(layer, offsets, positions, layout)
    positions = _optimize_cross_positions(layout, half_sizes, positions)
    first = min(position - half for position, half in zip(positions, half_sizes))
    return [position - first for position in positions]


def _ports(
    layout: LayeredLayout, across: list[float], half_sizes: list[float],
) -> dict[tuple[ModuleDependency, bool], float]:
    """按相邻虚拟顶点或目标模块的位置分配端口，布置同侧的多条入边和出边。"""
    sides = defaultdict(list)
    for edge, route in layout.routes.items():
        start, end = route[0], route[-1]
        if start == end:
            sides[start, 1].extend(((across[start] - 12, edge, False), (across[start] + 12, edge, True)))
        else:
            sides[start, 1].append((across[route[1]], edge, False))
            sides[end, -1].append((across[route[-2]], edge, True))

    endpoints = {}
    for (vertex, _), entries in sides.items():
        span = len(entries) - 1
        step = min(9.0, max(0.0, 2 * half_sizes[vertex] - 18) / max(1, span))
        ordered = sorted(entries, key=lambda entry: (entry[0], entry[1].source, entry[1].target, entry[2]))
        for index, (_, edge, is_end) in enumerate(ordered):
            endpoints[edge, is_end] = across[vertex] + (index - span / 2) * step
    return endpoints


def _join_lanes(
    path: QPainterPath, point: Callable[[float, float], QPointF],
    start: tuple[float, float], end: tuple[float, float],
) -> None:
    """只在两层之间调整横向坐标，曲线控制点完全位于两层的净空内。"""
    main_a, cross_a = start
    main_b, cross_b = end
    middle = (main_a + main_b) / 2
    path.lineTo(point(main_a, cross_a))
    path.cubicTo(point(middle, cross_a), point(middle, cross_b), point(main_b, cross_b))


def _arrowhead(tip: QPointF, direction: QPointF) -> QPolygonF:
    """按路径末端方向构造箭头，首个顶点与被依赖模块的连接点重合。"""
    length = math.hypot(direction.x(), direction.y())
    unit = direction / length
    base = tip - unit * 8
    side = QPointF(-unit.y(), unit.x()) * 3.3
    return QPolygonF([tip, base + side, base - side])


def build_geometry(
    sizes: dict[str, QSizeF], dependencies: tuple[ModuleDependency, ...], public: frozenset[str],
) -> GraphGeometry:
    """将内部布局转换成画布几何。有公共分组时沿纵轴排版，其余情况沿横轴排版。"""
    if not sizes:
        return GraphGeometry({}, {})
    layout = build_layout(sizes, dependencies, public)
    vertical = bool(public)

    def point(main: float, cross: float) -> QPointF:
        return QPointF(cross, main) if vertical else QPointF(main, cross)

    virtual_count = len(layout.previous) - len(layout.modules)
    main_sizes = [sizes[module].height() if vertical else sizes[module].width() for module in layout.modules]
    cross_sizes = [sizes[module].width() if vertical else sizes[module].height() for module in layout.modules]
    main_half = [size / 2 for size in main_sizes] + [0.0] * virtual_count
    cross_half = [size / 2 for size in cross_sizes] + [0.0] * virtual_count
    across = _cross_positions(layout, cross_half)
    ranks = [0] * len(across)
    starts, ends, centers = [], [], []
    cursor = 0.0
    public_started = False
    for rank, layer in enumerate(layout.layers):
        if not public_started and any(vertex < len(layout.modules) and layout.modules[vertex] in public
                                      for vertex in layer):
            # 公共模块在连续的末尾层。为组标题单独留高，避免外框覆盖前一层节点。
            public_started = True
            cursor += _GROUP_HEADER
        extent = max((main_half[vertex] for vertex in layer), default=0.0)
        starts.append(cursor)
        centers.append(cursor + extent)
        ends.append(cursor + 2 * extent)
        cursor = ends[-1] + _RANK_GAP
        for vertex in layer:
            ranks[vertex] = rank

    boxes = {}
    public_bounds = QRectF()
    for vertex, module_id in enumerate(layout.modules):
        rect = QRectF(QPointF(), sizes[module_id])
        rect.moveCenter(point(centers[ranks[vertex]], across[vertex]))
        boxes[module_id] = rect
        if module_id in public:
            public_bounds = public_bounds.united(rect)

    endpoints = _ports(layout, across, cross_half)
    outer_start = min(position - half for position, half in zip(across, cross_half)) - _NODE_GAP
    outer_end = max(position + half for position, half in zip(across, cross_half)) + _NODE_GAP
    edges = {}
    for edge, route in layout.routes.items():
        start, end = route[0], route[-1]
        first_rank, last_rank = ranks[start], ranks[end]
        start_main = centers[first_rank] + main_half[start]
        cross_a, cross_b = endpoints[edge, False], endpoints[edge, True]
        path = QPainterPath(point(start_main, cross_a))
        if start == end:
            # 自依赖使用节点前方的净空。控制点限制在节点的横向范围内。
            first_control = point(start_main + 30, max(across[start] - cross_half[start], cross_a - 12))
            last_control = point(start_main + 30, min(across[start] + cross_half[start], cross_b + 12))
            tip = point(start_main, cross_b)
            path.cubicTo(first_control, last_control, tip)
            edges[edge] = path, _arrowhead(tip, tip - last_control)
            continue

        if len(route) == 2 and last_rank - first_rank > 1:
            # 极密集的图将部分长边送入外侧共享通道。两端均从层间净空转弯。
            corridor = outer_start if cross_a + cross_b < outer_start + outer_end else outer_end
            _join_lanes(path, point, (ends[first_rank], cross_a), (starts[first_rank + 1], corridor))
            _join_lanes(path, point, (ends[last_rank - 1], corridor), (starts[last_rank], cross_b))
        else:
            # 跨过一层时沿其虚拟顶点直行，在两层之间以相同切线方向接上三次贝塞尔曲线。
            lanes = (cross_a, *(across[vertex] for vertex in route[1:-1]), cross_b)
            for rank, (lane_a, lane_b) in enumerate(pairwise(lanes), start=first_rank):
                _join_lanes(path, point, (ends[rank], lane_a), (starts[rank + 1], lane_b))
        tip = point(centers[last_rank] - main_half[end], cross_b)
        path.lineTo(tip)
        direction = point(1, 0)
        if edge.source != layout.modules[start]:
            # 布局时可能逆转环内或跨组的边。最终沿文档中的实际方向放置箭头。
            path = path.toReversed()
            tip, direction = path.currentPosition(), point(-1, 0)
        edges[edge] = path, _arrowhead(tip, direction)

    bounds = (public_bounds.adjusted(-_GROUP_PADDING, -_GROUP_HEADER, _GROUP_PADDING, _GROUP_PADDING)
              if public else None)
    return GraphGeometry(boxes, edges, bounds)
