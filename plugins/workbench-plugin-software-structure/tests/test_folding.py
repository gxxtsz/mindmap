"""验证公共依赖的覆盖率边界，以及折叠后的连线保护。"""

from workbench_plugin_software_structure.document import Module, ModuleDependency, SoftwareStructure
from workbench_plugin_software_structure.module_deps.folding import find_public_dependencies
from workbench_plugin_software_structure.module_deps.layout import find_cyclic_dependencies


def test_public_dependencies_use_strict_majority_inside_each_connected_graph():
    """十一模块子图按自身规模计算，恰好半数和自依赖都不会导致误分组。"""
    ordinary = tuple(f"module-{index}" for index in range(9))
    unrelated = ("small", "small-a", "small-b")
    modules = tuple(Module(name, name) for name in ("shared", "half", *ordinary, *unrelated))
    shared_edges = tuple(ModuleDependency(name, "shared") for name in ordinary[:6])
    edges = (
        *shared_edges,
        *(ModuleDependency(name, "half") for name in ordinary[:5]),
        *(ModuleDependency(source, target) for source, target in zip(ordinary[1:], ordinary)),
        ModuleDependency("half", "half"),
        ModuleDependency("shared", ordinary[0]),
        ModuleDependency("small-a", "small"),
        ModuleDependency("small-b", "small"),
    )
    document = SoftwareStructure(modules, edges)
    public = find_public_dependencies(document, find_cyclic_dependencies(document))
    assert public.dependent_counts == {"shared": 6}
    assert public.folded == frozenset(shared_edges[1:])


def test_folding_preserves_one_connection_for_every_ordinary_module():
    """普通模块仅依赖公共模块时保留一条关联，包括原本只有一条边的模块。"""
    ordinary = tuple(f"module-{index}" for index in range(9))
    modules = tuple(Module(name, name) for name in ("shared-a", "shared-b", *ordinary))
    first = tuple(ModuleDependency(name, "shared-a") for name in ordinary)
    second = tuple(ModuleDependency(name, "shared-b") for name in ordinary[1:])
    document = SoftwareStructure(modules, (*second, *first))
    public = find_public_dependencies(document, frozenset())
    assert public.dependent_counts == {"shared-a": 9, "shared-b": 8}
    assert public.folded == frozenset(second)
