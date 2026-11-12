"""按直接入度识别公共依赖，并计算总览中可以收起的连线。"""

from collections import Counter
from dataclasses import dataclass

from ..document import ModuleDependency, SoftwareStructure


@dataclass(frozen=True, slots=True)
class PublicDependencies:
    """公共模块的直接依赖方数量，以及总览默认隐藏的边。"""

    dependent_counts: dict[str, int]
    folded: frozenset[ModuleDependency]


def find_public_dependencies(
    document: SoftwareStructure, cyclic: frozenset[ModuleDependency],
) -> PublicDependencies:
    """以弱连通子图为范围计算覆盖率，保留循环警示和普通模块的关联。

    每个子图超过十个模块才参与折叠。直接依赖方按模块计数，自依赖不计入
    分子或分母。文件已经校验同一对模块的同向依赖只出现一次。
    """
    if len(document.modules) <= 10:
        return PublicDependencies({}, frozenset())

    neighbors: dict[str, set[str]] = {module.id: set() for module in document.modules}
    incoming: Counter[str] = Counter()
    for edge in document.module_dependencies:
        if edge.source != edge.target:
            neighbors[edge.source].add(edge.target)
            neighbors[edge.target].add(edge.source)
            incoming[edge.target] += 1

    public = {}
    visited = set()
    # 按无向连通性划分统计范围，避免彼此独立的模块稀释某个子图的覆盖率。
    for root in neighbors:
        if root in visited:
            continue
        visited.add(root)
        pending = [root]
        component = []
        while pending:
            node = pending.pop()
            component.append(node)
            unvisited = neighbors[node] - visited
            visited.update(unvisited)
            pending.extend(unvisited)
        if len(component) > 10:
            public.update((node, incoming[node]) for node in component if 2 * incoming[node] > len(component) - 1)

    folded = {edge for edge in document.module_dependencies if edge.target in public and edge not in cyclic}
    connected = {
        node for edge in document.module_dependencies
        if edge.source != edge.target and edge not in folded
        for node in (edge.source, edge.target)
    }
    # 只有指向公共模块的入边会折叠。因此需要保护的普通模块只可能是这些边的起点。
    # 按标识选定一条边，文件里的条目顺序改变后也能得到相同的总览。
    for edge in sorted(folded, key=lambda item: (item.source, item.target)):
        if edge.source not in public and edge.source not in connected:
            folded.remove(edge)
            connected.add(edge.source)
    return PublicDependencies(public, frozenset(folded))
