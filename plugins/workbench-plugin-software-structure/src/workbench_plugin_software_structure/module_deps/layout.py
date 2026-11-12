"""把有环的模块图压缩为有向无环图，计算可重复的水平分层。"""

from dataclasses import dataclass
from graphlib import TopologicalSorter

from ..document import ModuleDependency, SoftwareStructure


@dataclass(frozen=True, slots=True)
class LayeredLayout:
    """从左到右的模块层及所有属于循环的边。每层的模块按标识排序。"""

    layers: tuple[tuple[str, ...], ...]
    cyclic_dependencies: frozenset[ModuleDependency]


def _finish_order(outgoing: dict[str, list[str]]) -> list[str]:
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


def build_layout(document: SoftwareStructure) -> LayeredLayout:
    """基于已经过校验的文档计算分层，不依赖 Qt 控件或文件中的条目顺序。

    按正向深度优先的完成顺序反向遍历，得到强连通分量。同一分量内的依赖
    都属于循环。再对分量图做拓扑排序，每条跨分量依赖从较左的层连向较右的层。
    """
    outgoing = {module.id: [] for module in document.modules}
    incoming = {module.id: [] for module in document.modules}
    for dependency in document.module_dependencies:
        outgoing[dependency.source].append(dependency.target)
        incoming[dependency.target].append(dependency.source)

    component_by_module = {}
    predecessors: list[set[int]] = []
    for root in reversed(_finish_order(outgoing)):
        if root in component_by_module:
            continue
        component = len(predecessors)
        predecessors.append(set())
        component_by_module[root] = component
        pending = [root]
        while pending:
            for neighbor in incoming[pending.pop()]:
                if neighbor not in component_by_module:
                    component_by_module[neighbor] = component
                    pending.append(neighbor)

    cyclic = set()
    for dependency in document.module_dependencies:
        source = component_by_module[dependency.source]
        target = component_by_module[dependency.target]
        if source == target:
            cyclic.add(dependency)
        else:
            predecessors[target].add(source)

    ranks = [0] * len(predecessors)
    for component in TopologicalSorter(dict(enumerate(predecessors))).static_order():
        ranks[component] = max((ranks[parent] + 1 for parent in predecessors[component]), default=0)

    layers = [[] for _ in range(max(ranks, default=-1) + 1)]
    for module in sorted(outgoing):
        layers[ranks[component_by_module[module]]].append(module)
    return LayeredLayout(tuple(tuple(layer) for layer in layers), frozenset(cyclic))
