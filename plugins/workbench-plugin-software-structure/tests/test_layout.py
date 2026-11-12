"""验证分层、环检测和长依赖链的布局结果。"""

from workbench_plugin_software_structure.document import Module, ModuleDependency, SoftwareStructure
from workbench_plugin_software_structure.module_deps.layout import LayeredLayout, build_layout


def test_layers_are_stable_and_cycles_stay_in_one_layer():
    modules = tuple(Module(name, name) for name in ("b", "a", "d", "c", "e", "isolated", "loop"))
    edges = tuple(ModuleDependency(source, target) for source, target in (
        ("a", "b"), ("a", "e"), ("b", "d"), ("b", "c"), ("c", "b"), ("c", "d"), ("d", "e"), ("loop", "loop"),
    ))
    layout = build_layout(SoftwareStructure(modules, edges))
    assert layout.layers == (("a", "isolated", "loop"), ("b", "c"), ("d",), ("e",))
    assert layout.cyclic_dependencies == {edges[3], edges[4], edges[7]}
    assert build_layout(SoftwareStructure(modules[::-1], edges[::-1])) == layout


def test_long_chain_uses_iterative_traversal():
    ids = [str(index) for index in range(2200)]
    modules = tuple(Module(module_id, module_id) for module_id in ids)
    edges = tuple(ModuleDependency(source, target) for source, target in zip(ids, ids[1:]))
    assert build_layout(SoftwareStructure(modules, edges)) == LayeredLayout(
        tuple((module_id,) for module_id in ids), frozenset(),
    )


def test_layout_accepts_empty_directory():
    assert build_layout(SoftwareStructure(())) == LayeredLayout((), frozenset())
