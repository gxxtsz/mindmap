"""用最小费用流求解分层和坐标共用的加权跨度问题。"""

from dataclasses import dataclass
from heapq import heappop, heappush
from math import inf

_MAX_EDGE_SCANS = 1_200_000


@dataclass(frozen=True, slots=True)
class SpanConstraint:
    """约束目标坐标至少比起点大 minimum，并给两点的跨度赋予非负权重。"""

    source: int
    target: int
    minimum: int
    weight: int = 0


@dataclass(slots=True)
class _ResidualEdge:
    target: int
    reverse: int
    capacity: int
    cost: int


def _add_edge(graph: list[list[_ResidualEdge]], source: int, target: int, capacity: int, cost: int) -> None:
    """加入一对正反残量边，后续增广可以撤回已经分配的流量。"""
    graph[source].append(_ResidualEdge(target, len(graph[target]), capacity, cost))
    graph[target].append(_ResidualEdge(source, len(graph[source]) - 1, 0, -cost))


def minimize_spans(initial: list[int], constraints: list[SpanConstraint]) -> list[int]:
    """保持全部间距约束，求解加权跨度和最小的整数坐标。

    输入坐标已满足每条差分约束。将各点的入边权重减去出边权重作为流量需求，
    每条约束边的费用取最小间距的相反数。这个最小费用流是跨度问题的对偶，
    增广结束后的负势能就是所需坐标。若工作量超出交互预算，则沿用输入的可行解。
    """
    balance = [0] * len(initial)
    has_slack = False
    for edge in constraints:
        balance[edge.source] -= edge.weight
        balance[edge.target] += edge.weight
        has_slack |= edge.weight > 0 and initial[edge.target] - initial[edge.source] > edge.minimum
    if not has_slack:
        return initial
    supplies = [vertex for vertex, amount in enumerate(balance) if amount < 0]
    demands = [vertex for vertex, amount in enumerate(balance) if amount > 0]
    if not supplies or len(constraints) * max(len(supplies), len(demands)) > _MAX_EDGE_SCANS:
        return initial

    source, sink = len(initial), len(initial) + 1
    graph: list[list[_ResidualEdge]] = [[] for _ in range(sink + 1)]
    remaining = sum(balance[vertex] for vertex in demands)
    for edge in constraints:
        # 容量大于总需求，原约束的正向残量边始终存在，因此势能在求解中保持可行。
        _add_edge(graph, edge.source, edge.target, remaining + 1, -edge.minimum)
    for vertex in supplies:
        _add_edge(graph, source, vertex, -balance[vertex], 0)
    for vertex in demands:
        _add_edge(graph, vertex, sink, balance[vertex], 0)
    potentials = [-coordinate for coordinate in initial]
    potentials.extend((max(potentials[vertex] for vertex in supplies),
                       min(potentials[vertex] for vertex in demands)))

    scans = 0
    while remaining:
        distances = [inf] * len(graph)
        distances[source] = 0
        parents = [(-1, -1)] * len(graph)
        pending = [(0, source)]
        # 可行坐标使初始折算费用非负。每次增广后更新势能，后续仍可使用最短路。
        while pending:
            distance, vertex = heappop(pending)
            if distance != distances[vertex]:
                continue
            if vertex == sink:
                break
            scans += len(graph[vertex])
            if scans > _MAX_EDGE_SCANS:
                return initial
            for index, edge in enumerate(graph[vertex]):
                if not edge.capacity:
                    continue
                candidate = distance + edge.cost + potentials[vertex] - potentials[edge.target]
                if candidate < distances[edge.target]:
                    distances[edge.target] = candidate
                    parents[edge.target] = vertex, index
                    heappush(pending, (candidate, edge.target))
        if distances[sink] == inf:
            return initial

        # 截断到汇点距离，保证尚未访问到的分量也满足原来的间距约束。
        limit = int(distances[sink])
        for vertex, distance in enumerate(distances):
            potentials[vertex] += int(min(distance, limit))
        path = []
        vertex = sink
        while vertex != source:
            parent, index = parents[vertex]
            path.append(graph[parent][index])
            vertex = parent
        amount = min(edge.capacity for edge in path)
        for edge in path:
            edge.capacity -= amount
            graph[edge.target][edge.reverse].capacity += amount
        remaining -= amount

    return [-potential for potential in potentials[:source]]
