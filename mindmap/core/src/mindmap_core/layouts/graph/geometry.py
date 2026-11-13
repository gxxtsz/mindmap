"""按节点尺寸布置层内坐标，并沿虚拟顶点绘制避开节点的样条。"""

import math
from collections import defaultdict
from itertools import chain, pairwise

from ...geometry import BranchPath, LayoutResult, PathCommand, Point, Rect
from ...graph import GraphEdge
from .constraints import SpanConstraint, minimize_spans
from .layers import LayeredLayout, build_layout

_RANK_GAP = 46.0
_NODE_GAP = 22.0
_LANE_GAP = 7.0
# 箭头放大后长 9.6 个场景单位，两端的直段需容纳放大后的箭头。
_PORT_STRAIGHT = 11.0
_COORDINATE_SCALE = 2
_SINGLE_NODE_ALIGNMENT_WEIGHT = 64


def _align_layer(
    layer: tuple[int, ...], offsets: list[float], positions: list[float], layout: LayeredLayout,
) -> None:
    """在保持层内顺序和最小间隔的前提下，尽量向相邻层的节点靠拢。

    减去每个节点的最小间距前缀后，剩余位移只需单调不减。若两段位移相互
    挤压，则合并为同一加权平均值。虚拟节点之间的连接权重更高，长线更直。
    """
    blocks: list[tuple[float, float, int]] = []
    node_count = len(layout.nodes)
    for vertex, offset in zip(layer, offsets):
        weight = 0.25
        total = positions[vertex] * weight
        for neighbor in (*layout.previous[vertex], *layout.following[vertex]):
            strength = 6.0 if vertex >= node_count and neighbor >= node_count else 1.0
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


def _minimum_gap(left: int, right: int, half_sizes: list[float], node_count: int) -> float:
    """按实际节点尺寸或虚拟走线位置，计算层内相邻点的最小中心间距。"""
    gap = _NODE_GAP if left < node_count and right < node_count else _LANE_GAP
    return half_sizes[left] + gap + half_sizes[right]


def _optimize_cross_positions(
    layout: LayeredLayout, half_sizes: list[float], positions: list[float],
) -> list[float]:
    """在层内间距和图宽约束下，全局缩短连线在横向的偏移。

    每段连线引入一个辅助点。该点的坐标不超过两端，所以优化会把它移到两端较小值，
    此时两条辅助边的总跨度等于端点间距。虚拟节点之间权重更高，优先拉直跨层边。
    """
    vertex_count = len(positions)
    node_count = len(layout.nodes)
    initial = [round(position * _COORDINATE_SCALE) for position in positions]
    extent = [math.ceil(size * _COORDINATE_SCALE) for size in half_sizes]
    constraints = []
    single_nodes = set()
    for layer in layout.layers:
        nodes_in_layer = [vertex for vertex in layer if vertex < node_count]
        if len(nodes_in_layer) == 1:
            single_nodes.add(nodes_in_layer[0])
        for left, right in pairwise(layer):
            gap = math.ceil(_minimum_gap(left, right, half_sizes, node_count) * _COORDINATE_SCALE)
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
            virtual_ends = int(source >= node_count) + int(target >= node_count)
            weight = (4, 8, 32)[virtual_ends]
            # 连续两个各只有一个真实节点的层形成明确的阅读顺序。优先对齐这两个节点，
            # 免得大量穿过中间层的长边把两个节点分别拉到画布两侧。
            if source in single_nodes and target in single_nodes:
                weight = _SINGLE_NODE_ALIGNMENT_WEIGHT
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
    node_count = len(layout.nodes)
    for layer in layout.layers:
        offsets = [0.0]
        for left, right in pairwise(layer):
            offsets.append(offsets[-1] + _minimum_gap(left, right, half_sizes, node_count))
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
) -> dict[tuple[GraphEdge, bool], float]:
    """按相邻虚拟顶点或目标节点的位置分配端口，布置同侧的多条入边和出边。"""
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


def _join_lanes(path: list[PathCommand], start: Point, end: Point) -> None:
    """在指定的层间净空内改变横坐标，转弯两端以纵向切线接入相邻走线。

    调用方传入已避开箭头直段的转弯起止坐标。到达转弯区之前沿当前走线直行。
    """
    start_x, start_y = start
    end_x, end_y = end
    middle_y = (start_y + end_y) / 2
    path.append(PathCommand("L", (start,)))
    path.append(PathCommand("C", ((start_x, middle_y), (end_x, middle_y), end)))


def _arrowhead(tip: Point, direction: Point) -> tuple[Point, tuple[Point, Point, Point]]:
    """沿单位方向生成箭头及线身末点，尖端始终贴在目标节点边界。

    线身从底边正中伸入 1 个场景单位，使抗锯齿绘制后依然贴合，同时保留尖端
    的轮廓。尖端固定在卡片端口，选中放大时仍可使用同一个线身末点。
    """
    dx, dy = direction
    x, y = tip[0] - 8 * dx, tip[1] - 8 * dy
    return (x + dx, y + dy), (tip, (x + 3.3 * dy, y - 3.3 * dx), (x - 3.3 * dy, y + 3.3 * dx))


def _reverse_route(path: list[PathCommand]) -> list[PathCommand]:
    """从真实起点重放按层高构造的逆向路径；三次贝塞尔的控制点也反向排列。"""
    segments = list(pairwise(path))
    return [PathCommand("M", (path[-1].points[-1],)),
            *(PathCommand(segment.op, (*reversed(segment.points[:-1]), previous.points[-1]))
              for previous, segment in reversed(segments))]


def build_graph_layout(sizes: dict[str, Point], edges: tuple[GraphEdge, ...], *, color: str) -> LayoutResult:
    """生成分层节点矩形与有向路径，返回树画布和静态导出共用的逻辑几何。

    用节点高度计算层高，用宽度保持层内间距。始终按完整连线布局，筛选只影响
    显示。循环和自环保持原始箭头方向，标识与绘制顺序互相独立。
    """
    if not sizes:
        return LayoutResult()
    layout = build_layout(sizes, edges)
    virtual_count = len(layout.previous) - len(layout.nodes)
    half_heights = [sizes[key][1] / 2 for key in layout.nodes] + [0.0] * virtual_count
    half_widths = [sizes[key][0] / 2 for key in layout.nodes] + [0.0] * virtual_count
    across = _cross_positions(layout, half_widths)
    ranks = [0] * len(across)
    starts, ends, centers = [], [], []
    cursor = 0.0
    for rank, layer in enumerate(layout.layers):
        extent = max((half_heights[vertex] for vertex in layer), default=0.0)
        starts.append(cursor)
        centers.append(cursor + extent)
        ends.append(cursor + 2 * extent)
        cursor = ends[-1] + _RANK_GAP
        for vertex in layer:
            ranks[vertex] = rank

    boxes = {}
    for vertex, key in enumerate(layout.nodes):
        boxes[key] = Rect(across[vertex] - half_widths[vertex], centers[ranks[vertex]] - half_heights[vertex],
                          *sizes[key])

    endpoints = _ports(layout, across, half_widths)
    # 层顶保留入边的直段。逆向边和自环的箭头位于层底，对这些层的出边也延后转弯，
    # 使附近的线身在箭头占用的高度内互相平行。
    ranks_with_upward_arrows = {
        ranks[route[0]] for edge, route in layout.routes.items()
        if route[0] == route[-1] or edge.source != layout.nodes[route[0]]
    }
    bend_starts = [end + _PORT_STRAIGHT if rank in ranks_with_upward_arrows else end
                   for rank, end in enumerate(ends)]
    bend_ends = [start - _PORT_STRAIGHT for start in starts]
    left_corridor = min(position - half for position, half in zip(across, half_widths)) - _NODE_GAP
    right_corridor = max(position + half for position, half in zip(across, half_widths)) + _NODE_GAP
    branches = {}
    for edge, route in layout.routes.items():
        start, end = route[0], route[-1]
        first_rank, last_rank = ranks[start], ranks[end]
        start_y = centers[first_rank] + half_heights[start]
        start_x, end_x = endpoints[edge, False], endpoints[edge, True]
        start_port = (start_x, start_y)
        if start == end:
            # 自环在卡片下方回转，两端以纵向切线与直段相接。
            tip = (end_x, start_y)
            shaft_end, arrow = _arrowhead(tip, (0.0, -1.0))
            turn_y = start_y + _PORT_STRAIGHT
            control_y = start_y + 27
            path = (PathCommand("M", (start_port,)), PathCommand("L", ((start_x, turn_y),)),
                    PathCommand("C", ((start_x, control_y), (end_x, control_y), (end_x, turn_y))),
                    PathCommand("L", (shaft_end,)))
            branches[edge.key] = BranchPath(start_port, tip, path, color, arrow=arrow)
            continue

        end_port = (end_x, centers[last_rank] - half_heights[end])
        reversed_route = edge.source != layout.nodes[start]
        # 仍按从上往下的顺序绘制。逆向边从上方箭头底边内侧起笔，之后再反转路径。
        source, tip = (end_port, start_port) if reversed_route else (start_port, end_port)
        shaft_end, arrow = _arrowhead(tip, (0.0, -1.0 if reversed_route else 1.0))
        path = [PathCommand("M", (shaft_end if reversed_route else start_port,))]
        if len(route) == 2 and last_rank - first_rank > 1:
            # 极密集的图将部分长边送入外侧共享通道。两端均从层间净空转弯。
            corridor = left_corridor if start_x + end_x < left_corridor + right_corridor else right_corridor
            _join_lanes(path, (start_x, bend_starts[first_rank]), (corridor, bend_ends[first_rank + 1]))
            _join_lanes(path, (corridor, bend_starts[last_rank - 1]), (end_x, bend_ends[last_rank]))
        else:
            # 跨过一层时沿其虚拟顶点直行，在两层之间以相同切线方向接上三次贝塞尔曲线。
            lanes = (start_x, *(across[vertex] for vertex in route[1:-1]), end_x)
            for rank, (lane_a, lane_b) in enumerate(pairwise(lanes), start=first_rank):
                _join_lanes(path, (lane_a, bend_starts[rank]), (lane_b, bend_ends[rank + 1]))
        path.append(PathCommand("L", (end_port if reversed_route else shaft_end,)))
        branches[edge.key] = BranchPath(source, tip, tuple(_reverse_route(path) if reversed_route else path),
                                        color, arrow=arrow)

    return _layout_result(boxes, branches)


def _layout_result(nodes, branches) -> LayoutResult:
    bounds = None
    for rect in chain(nodes.values(), (branch.bounds for branch in branches.values())):
        bounds = rect if bounds is None else bounds.union(rect)
    return LayoutResult(nodes, branches, bounds.padded(24) if bounds else Rect(0, 0, 1, 1))


def _free_edge(source: Rect, target: Rect, color: str, *, loop: bool, paired: bool) -> BranchPath:
    """按自由移动后的卡片边界布线，并为双向边及自环分配独立箭头。"""
    if loop:
        x, y = source.center[0], source.bottom
        start, end = (x - 10, y), (x + 10, y)
        shaft, arrow = _arrowhead(end, (0.0, -1.0))
        curve = PathCommand("C", ((x - 24, y + 36), (end[0], y + 36), shaft))
    else:
        dx, dy = target.center[0] - source.center[0], target.center[1] - source.center[1]
        # 相互覆盖时从不同侧面布线，避免零长度导致箭头无法计算。
        if abs(dx) < (source.width + target.width) / 2 and abs(dy) < (source.height + target.height) / 2:
            dx, dy = 0, dy or 1
        side = 6 if paired else 0
        if abs(dx) > abs(dy):
            direction = math.copysign(1.0, dx)
            start = (source.right if direction > 0 else source.x, source.center[1] + side * direction)
            end = (target.x if direction > 0 else target.right, target.center[1] + side * direction)
            normal = (direction, 0.0)
        else:
            direction = math.copysign(1.0, dy)
            start = (source.center[0] - side * direction, source.bottom if direction > 0 else source.y)
            end = (target.center[0] - side * direction, target.y if direction > 0 else target.bottom)
            normal = (0.0, direction)
        shaft, arrow = _arrowhead(end, normal)
        length = max(18.0, math.dist(start, end) * 0.35)
        curve = PathCommand("C", ((start[0] + normal[0] * length, start[1] + normal[1] * length),
                                  (shaft[0] - normal[0] * length, shaft[1] - normal[1] * length), shaft))
    return BranchPath(start, end, (PathCommand("M", (start,)), curve), color, arrow=arrow)


def move_graph_nodes(layout: LayoutResult, edges: tuple[GraphEdge, ...], positions: dict[str, Point]) -> LayoutResult:
    """叠加手动坐标或拖动草稿，只改动实际移动的矩形和受影响的连线。"""
    moved = {key for key, point in positions.items() if key in layout.nodes
             and point != (layout.nodes[key].x, layout.nodes[key].y)}
    if not moved:
        return layout
    nodes, branches = dict(layout.nodes), dict(layout.branches)
    for key in moved:
        nodes[key] = Rect(*positions[key], nodes[key].width, nodes[key].height)
    edge_set = set(edges)
    for edge in edges:
        branch = branches.get(edge.key)
        if branch is not None and (edge.source in moved or edge.target in moved):
            branches[edge.key] = _free_edge(nodes[edge.source], nodes[edge.target], branch.color,
                                           loop=edge.source == edge.target,
                                           paired=GraphEdge(edge.target, edge.source) in edge_set)
    return _layout_result(nodes, branches)
