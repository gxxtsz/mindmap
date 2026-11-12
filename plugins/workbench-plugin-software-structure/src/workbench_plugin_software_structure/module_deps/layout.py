"""按有向图的层级和层内顺序规划模块位置及跨层走线。"""

from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from ..document import ModuleDependency, SoftwareStructure
from .constraints import SpanConstraint, minimize_spans


@dataclass(frozen=True, slots=True)
class LayeredLayout:
    """层内顶点已经排序。模块占前若干编号，其余编号是供连线穿过的虚拟顶点。

    路径按层号从小到大记录。原始依赖仍作为键保存，因此可以在绘制时恢复箭头
    的方向。前后邻接表只含相邻两层之间的连接。
    """

    modules: tuple[str, ...]
    layers: tuple[tuple[int, ...], ...]
    previous: tuple[tuple[int, ...], ...]
    following: tuple[tuple[int, ...], ...]
    routes: dict[ModuleDependency, tuple[int, ...]]


def _finish_order[T](outgoing: dict[T, list[T]]) -> list[T]:
    """以显式栈记录深度优先的完成顺序，使长依赖链也能完成遍历。"""
    visited = set()
    order = []
    for root in outgoing:
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(outgoing[root]))]
        while stack:
            module, neighbors = stack[-1]
            neighbor = next(neighbors, None)
            if neighbor is None:
                order.append(module)
                stack.pop()
            elif neighbor not in visited:
                visited.add(neighbor)
                stack.append((neighbor, iter(outgoing[neighbor])))
    return order


def find_cyclic_dependencies(document: SoftwareStructure) -> frozenset[ModuleDependency]:
    """在完整文档上计算强连通分量，标记所有环内依赖和自环。"""
    outgoing = {module.id: [] for module in document.modules}
    incoming = {module.id: [] for module in document.modules}
    for dependency in document.module_dependencies:
        outgoing[dependency.source].append(dependency.target)
        incoming[dependency.target].append(dependency.source)

    component_by_module = {}
    for root in reversed(_finish_order(outgoing)):
        if root in component_by_module:
            continue
        component = root
        component_by_module[root] = component
        pending = [root]
        while pending:
            for neighbor in incoming[pending.pop()]:
                if neighbor not in component_by_module:
                    component_by_module[neighbor] = component
                    pending.append(neighbor)

    return frozenset(
        edge for edge in document.module_dependencies
        if component_by_module[edge.source] == component_by_module[edge.target]
    )


def _assign_ranks(
    order: list[int], public: set[int], previous: list[list[int]], following: list[list[int]],
) -> list[int]:
    """用最长路径构造可行分层，再整体缩短边跨度，最后均衡同价的层。"""
    ranks = [0] * len(order)
    public_start = 0
    for vertex in order:
        first_rank = public_start if vertex in public else 0
        ranks[vertex] = max(first_rank, max((ranks[parent] + 1 for parent in previous[vertex]), default=0))
        if vertex not in public:
            public_start = max(public_start, ranks[vertex] + 1)

    last_rank = max(ranks, default=0)
    for traversal in (order[::-1], order):
        for vertex in traversal:
            if vertex in public:
                first, last = public_start, last_rank
            else:
                first, last = 0, public_start - 1
            # 入边较多时前移、出边较多时后移。移动不跨过相邻节点，也不跨过公共分组边界。
            before, after = previous[vertex], following[vertex]
            if len(before) > len(after):
                ranks[vertex] = max(first, max(ranks[parent] + 1 for parent in before))
            elif len(after) > len(before):
                ranks[vertex] = min(last, min(ranks[child] - 1 for child in after))

    constraints = [
        SpanConstraint(vertex, child, 1, 1)
        for vertex in order for child in following[vertex]
    ]
    if 0 < len(public) < len(order):
        # 以一个零权重的分界点约束整个公共分组，避免逐对添加跨组约束。
        boundary = len(ranks)
        ranks.append(public_start)
        for vertex in order:
            if vertex in public:
                constraints.append(SpanConstraint(boundary, vertex, 0))
            else:
                constraints.append(SpanConstraint(vertex, boundary, 1))
    optimized = minimize_spans(ranks, constraints)[:len(order)]
    rank_index = {rank: index for index, rank in enumerate(sorted(set(optimized)))}
    ranks = [rank_index[rank] for rank in optimized]
    _balance_ranks(order, public, previous, following, ranks)
    return ranks


def _balance_ranks(
    order: list[int], public: set[int], previous: list[list[int]], following: list[list[int]], ranks: list[int],
) -> None:
    """把入边和出边同样多的模块移到更空的可行层，跨度代价保持相同。"""
    last_rank = max(ranks, default=-1)
    public_start = min((ranks[vertex] for vertex in public), default=last_rank + 1)
    occupancy = [0] * (last_rank + 1)
    for rank in ranks:
        occupancy[rank] += 1
    for vertex in order:
        before, after = previous[vertex], following[vertex]
        if not before or len(before) != len(after):
            continue
        first, last = (public_start, last_rank) if vertex in public else (0, public_start - 1)
        lower = max(first, max(ranks[parent] + 1 for parent in before))
        upper = min(last, min(ranks[child] - 1 for child in after))
        if lower < upper:
            current = ranks[vertex]
            rank = min(range(lower, upper + 1), key=lambda item: (occupancy[item], abs(item - current), item))
            occupancy[current] -= 1
            occupancy[rank] += 1
            ranks[vertex] = rank


def _weighted_median(neighbors: list[int], positions: list[int]) -> float:
    """以相邻层的节点位置为依据，偶数个邻居时向分布更集中的一侧偏移。"""
    values = sorted(positions[vertex] for vertex in neighbors)
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    left, right = values[middle - 1], values[middle]
    left_span, right_span = left - values[0], values[-1] - right
    if left_span + right_span == 0:
        return (left + right) / 2
    return (left * right_span + right * left_span) / (left_span + right_span)


def _crossings(layers: list[list[int]], following: list[list[int]], positions: list[int]) -> int:
    """用树状数组计数相邻层的边顺序逆转，跳过共用同一端点的边。"""
    crossings = 0
    for left, right in zip(layers, layers[1:]):
        if len(left) < 2 or len(right) < 2:
            continue
        counts = [0] * (len(right) + 1)
        seen = 0
        for vertex in left:
            targets = [positions[child] + 1 for child in following[vertex]]
            for index in targets:
                preceding = 0
                while index:
                    preceding += counts[index]
                    index -= index & -index
                crossings += seen - preceding
            for index in targets:
                while index < len(counts):
                    counts[index] += 1
                    index += index & -index
            seen += len(targets)
    return crossings


def _swap_gain(left: list[int], right: list[int], positions: list[int]) -> int:
    """计算交换同层相邻节点后少了几处交叉，共用的邻居不计入。"""
    neighbors = sorted(positions[vertex] for vertex in right)
    return sum(
        bisect_left(neighbors, positions[vertex]) + bisect_right(neighbors, positions[vertex]) - len(neighbors)
        for vertex in left
    )


def _reduce_crossings(
    layers: list[list[int]], previous: list[list[int]], following: list[list[int]],
) -> tuple[tuple[int, ...], ...]:
    """比较模块初始顺序与从末层回溯的顺序，保留交叉更少的结果。"""
    alternate = None
    if len(previous) <= 512:
        # 小图可多试一次从末端出发的排列。汇聚到同一目标的边会先得到接近的位置。
        pending = deque(vertex for layer in reversed(layers) for vertex in layer if not following[vertex])
        seen = set(pending)
        discovery = {}
        while pending:
            vertex = pending.popleft()
            discovery[vertex] = len(discovery)
            for parent in previous[vertex]:
                if parent not in seen:
                    seen.add(parent)
                    pending.append(parent)
        alternate = [sorted(layer, key=discovery.__getitem__) for layer in layers]
    best, count = _sweep_layers(layers, previous, following)
    if count and alternate is not None:
        candidate, candidate_count = _sweep_layers(alternate, previous, following)
        if candidate_count < count:
            best = candidate
    return best


def _sweep_layers(
    layers: list[list[int]], previous: list[list[int]], following: list[list[int]],
) -> tuple[tuple[tuple[int, ...], ...], int]:
    """交替扫描各层，按加权中位数排序，再交换能减少交叉的相邻节点。"""
    positions = [0] * len(previous)
    for layer in layers:
        for offset, vertex in enumerate(layer):
            positions[vertex] = offset
    best = tuple(map(tuple, layers))
    best_count = _crossings(layers, following, positions)
    for iteration in range(6):
        if not best_count:
            break
        traversal, neighbors = (layers[1:], previous) if iteration % 2 == 0 else (layers[-2::-1], following)
        for layer in traversal:
            movable = [offset for offset, vertex in enumerate(layer) if neighbors[vertex]]
            vertices = sorted((layer[offset] for offset in movable),
                              key=lambda vertex: _weighted_median(neighbors[vertex], positions))
            for offset, vertex in zip(movable, vertices):
                layer[offset], positions[vertex] = vertex, offset
        for layer in layers:
            for offset in range(len(layer) - 1):
                left, right = layer[offset], layer[offset + 1]
                gain = (_swap_gain(previous[left], previous[right], positions)
                        + _swap_gain(following[left], following[right], positions))
                if gain > 0:
                    layer[offset], layer[offset + 1] = right, left
                    positions[right], positions[left] = offset, offset + 1
        count = _crossings(layers, following, positions)
        if count < best_count:
            best_count, best = count, tuple(map(tuple, layers))
    return best, best_count


def build_layout(
    module_ids: Iterable[str], dependencies: tuple[ModuleDependency, ...], public: frozenset[str],
) -> LayeredLayout:
    """按当前可见边计算层级、层内顺序和走线路径，不依赖 Qt 或外部程序。

    深度优先完成顺序为环内边提供布局方向。公共模块占据连续的末尾若干层，
    跨组反向依赖也只在布局计算时转向。长边在每个中间层占一个走线位置。
    """
    modules = tuple(sorted(module_ids))
    index = {module_id: vertex for vertex, module_id in enumerate(modules)}
    public_vertices = {index[module_id] for module_id in public}
    outgoing = {vertex: [] for vertex in range(len(modules))}
    edges = sorted(dependencies, key=lambda edge: (edge.source, edge.target))
    for edge in edges:
        source, target = index[edge.source], index[edge.target]
        if source != target and (source in public_vertices) == (target in public_vertices):
            outgoing[source].append(target)
    order = list(reversed(_finish_order(outgoing)))
    order.sort(key=public_vertices.__contains__)
    order_index = {vertex: offset for offset, vertex in enumerate(order)}
    previous: list[list[int]] = [[] for _ in modules]
    following: list[list[int]] = [[] for _ in modules]
    endpoints = {}
    for edge in edges:
        source, target = index[edge.source], index[edge.target]
        start, end = sorted((source, target), key=order_index.__getitem__)
        endpoints[edge] = (start, end)
        if start != end:
            previous[end].append(start)
            following[start].append(end)
    ranks = _assign_ranks(order, public_vertices, previous, following)
    layers: list[list[int]] = [[] for _ in range(max(ranks, default=-1) + 1)]
    for vertex, rank in enumerate(ranks):
        layers[rank].append(vertex)

    # 先保留跨度短的走线位置。特别密集的图使用可复用的外侧通道容纳余下长边，
    # 将虚拟顶点的空间开销限制在与原图规模相同的量级。
    budget = max(4096, len(modules) * 8, len(edges) * 6)
    previous = [[] for _ in modules]
    following = [[] for _ in modules]
    routes = {}
    for edge, (start, end) in sorted(endpoints.items(), key=lambda item: ranks[item[1][1]] - ranks[item[1][0]]):
        if start == end:
            routes[edge] = (start,)
            continue
        count = ranks[end] - ranks[start] - 1
        if count > budget:
            routes[edge] = (start, end)
            continue
        budget -= count
        route = [start]
        for rank in range(ranks[start] + 1, ranks[end]):
            vertex = len(previous)
            layers[rank].append(vertex)
            previous.append([])
            following.append([])
            route.append(vertex)
        route.append(end)
        for first, second in pairwise(route):
            previous[second].append(first)
            following[first].append(second)
        routes[edge] = tuple(route)
    layers_in_order = _reduce_crossings(layers, previous, following)
    return LayeredLayout(modules, layers_in_order, tuple(map(tuple, previous)), tuple(map(tuple, following)), routes)
