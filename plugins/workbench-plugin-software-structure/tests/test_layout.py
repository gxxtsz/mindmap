"""验证分层顺序、交叉优化以及 Qt 路由的避让和箭头方向。"""

import pytest
from PySide6.QtCore import QSizeF
from PySide6.QtGui import QPainterPathStroker
from workbench_plugin_software_structure.document import Module, ModuleDependency, SoftwareStructure
from workbench_plugin_software_structure.module_deps.constraints import SpanConstraint, minimize_spans
from workbench_plugin_software_structure.module_deps.geometry import build_geometry
from workbench_plugin_software_structure.module_deps.layout import (
    LayeredLayout,
    build_layout,
    find_cyclic_dependencies,
)


def test_layers_are_stable_and_cyclic_edges_keep_their_original_identities():
    """同一环中的互逆边共用层级方向计算，但各自保留完整的依赖身份。"""
    modules = tuple(Module(name, name) for name in ("b", "a", "d", "c", "e", "isolated", "loop"))
    edges = tuple(ModuleDependency(source, target) for source, target in (
        ("a", "b"), ("a", "e"), ("b", "d"), ("b", "c"), ("c", "b"), ("c", "d"), ("d", "e"), ("loop", "loop"),
    ))
    ids = tuple(module.id for module in modules)
    layout = build_layout(ids, edges, frozenset())
    assert find_cyclic_dependencies(SoftwareStructure(modules, edges)) == {edges[3], edges[4], edges[7]}
    forward, backward = layout.routes[edges[3]], layout.routes[edges[4]]
    assert (forward[0], forward[-1]) == (backward[0], backward[-1])
    assert len(layout.routes[edges[1]]) > 2
    assert build_layout(ids[::-1], edges[::-1], frozenset()) == layout


def test_layer_order_reduces_crossings():
    """两条原本相交的相邻层连线经层内排序后应当互相平行。"""
    edges = (ModuleDependency("a", "d"), ModuleDependency("b", "c"))
    layout = build_layout(("a", "b", "c", "d"), edges, frozenset())
    source_order = [layout.modules[vertex] for vertex in layout.layers[0]]
    target_order = [layout.modules[vertex] for vertex in layout.layers[1]]
    assert target_order == [{"a": "d", "b": "c"}[module_id] for module_id in source_order]


def test_span_optimization_can_move_connected_vertices_together():
    """单独移动两个节点中的任一个都会变差或违反约束，整体移动可以缩短跨度。"""
    initial = [0, 1, 2, 3, 0, 1]
    constraints = [
        SpanConstraint(0, 1, 1, 1),
        SpanConstraint(1, 2, 1, 1),
        SpanConstraint(2, 3, 1, 1),
        SpanConstraint(0, 4, 0),
        SpanConstraint(4, 5, 1, 5),
        SpanConstraint(5, 3, 1, 3),
    ]
    result = minimize_spans(initial, constraints)
    assert all(result[edge.target] - result[edge.source] >= edge.minimum for edge in constraints)
    assert [position - result[0] for position in result] == [0, 1, 2, 3, 1, 2]


def test_equal_cost_vertices_fill_less_crowded_ranks():
    """两条等价的旁路分布到不同层，主路径的层级和总跨度保持可行。"""
    edges = tuple(ModuleDependency(source, target) for source, target in (
        ("a", "b"), ("b", "c"), ("c", "d"), ("a", "e"), ("e", "d"), ("a", "f"), ("f", "d"),
    ))
    layout = build_layout("abcdef", edges, frozenset())
    ranks = {layout.modules[vertex]: rank for rank, layer in enumerate(layout.layers)
             for vertex in layer if vertex < len(layout.modules)}
    assert {ranks["e"], ranks["f"]} == {1, 2}


def test_long_chain_uses_iterative_traversal():
    ids = [str(index) for index in range(2200)]
    edges = tuple(ModuleDependency(source, target) for source, target in zip(ids, ids[1:]))
    layout = build_layout(ids, edges, frozenset())
    assert [layout.modules[layer[0]] for layer in layout.layers] == ids
    assert all(len(layer) == 1 for layer in layout.layers)


def test_layout_accepts_empty_directory():
    assert build_layout((), (), frozenset()) == LayeredLayout((), (), (), (), {})


@pytest.mark.parametrize("public", [frozenset(), frozenset({"d", "e"})], ids=["ungrouped", "grouped"])
def test_vertical_splines_avoid_nodes_and_point_to_the_original_target(public):
    """校验有无公共分组时都按纵轴分层，并检查跨层边、逆向边和自环。"""
    sizes = {name: QSizeF(130 + index * 8, 38 + index * 5) for index, name in enumerate("abcde")}
    edges = tuple(ModuleDependency(source, target) for source, target in (
        ("a", "b"), ("b", "c"), ("c", "d"), ("a", "d"), ("d", "b"), ("c", "c"), ("b", "e"),
    ))
    geometry = build_geometry(sizes, edges, public)
    source, target = geometry.boxes["a"], geometry.boxes["b"]
    assert source.bottom() < target.top()
    curve, arrow = geometry.edges[edges[0]]
    assert curve.pointAtPercent(0).y() == source.bottom()
    assert arrow[0].y() == target.top()
    assert arrow[1].y() < arrow[0].y()

    bounds = geometry.public_bounds
    if public:
        assert bounds is not None
        assert all(bounds.contains(geometry.boxes[name]) for name in public)
        assert all(not bounds.intersects(box) for name, box in geometry.boxes.items() if name not in public)
    else:
        assert bounds is None

    stroker = QPainterPathStroker()
    stroker.setWidth(1)
    for edge, (curve, arrow) in geometry.edges.items():
        assert curve.currentPosition() == arrow[0]
        assert geometry.boxes[edge.target].adjusted(-1, -1, 1, 1).contains(arrow[0])
        stroke = stroker.createStroke(curve)
        assert all(not stroke.intersects(box) for name, box in geometry.boxes.items()
                   if name not in (edge.source, edge.target))
