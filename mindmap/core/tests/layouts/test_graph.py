"""验证图的分层顺序、交叉优化、路径避让及箭头方向。"""

from mindmap_core import GraphEdge
from mindmap_core.layouts.graph import build_graph_layout, move_graph_nodes
from mindmap_core.layouts.graph.constraints import SpanConstraint, minimize_spans
from mindmap_core.layouts.graph.layers import LayeredLayout, build_layout


def test_layers_are_stable_and_cyclic_edges_keep_their_original_identities():
    """同一环中的互逆边共用层级方向计算，但各自保留完整的依赖身份。"""
    ids = ("b", "a", "d", "c", "e", "isolated", "loop")
    edges = tuple(GraphEdge(source, target) for source, target in (
        ("a", "b"), ("a", "e"), ("b", "d"), ("b", "c"), ("c", "b"), ("c", "d"), ("d", "e"), ("loop", "loop"),
    ))
    layout = build_layout(ids, edges)
    forward, backward = layout.routes[edges[3]], layout.routes[edges[4]]
    assert (forward[0], forward[-1]) == (backward[0], backward[-1])
    assert len(layout.routes[edges[1]]) > 2
    assert build_layout(ids[::-1], edges[::-1]) == layout


def test_layer_order_reduces_crossings():
    """两条原本相交的相邻层连线经层内排序后应当互相平行。"""
    edges = (GraphEdge("a", "d"), GraphEdge("b", "c"))
    layout = build_layout(("a", "b", "c", "d"), edges)
    source_order = [layout.nodes[vertex] for vertex in layout.layers[0]]
    target_order = [layout.nodes[vertex] for vertex in layout.layers[1]]
    assert target_order == [{"a": "d", "b": "c"}[node_id] for node_id in source_order]


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
    edges = tuple(GraphEdge(source, target) for source, target in (
        ("a", "b"), ("b", "c"), ("c", "d"), ("a", "e"), ("e", "d"), ("a", "f"), ("f", "d"),
    ))
    layout = build_layout("abcdef", edges)
    ranks = {layout.nodes[vertex]: rank for rank, layer in enumerate(layout.layers)
             for vertex in layer if vertex < len(layout.nodes)}
    assert {ranks["e"], ranks["f"]} == {1, 2}


def test_long_chain_uses_iterative_traversal():
    ids = [str(index) for index in range(2200)]
    edges = tuple(GraphEdge(source, target) for source, target in zip(ids, ids[1:]))
    layout = build_layout(ids, edges)
    assert [layout.nodes[layer[0]] for layer in layout.layers] == ids
    assert all(len(layer) == 1 for layer in layout.layers)


def test_layout_accepts_empty_directory():
    assert build_layout((), ()) == LayeredLayout((), (), (), (), {})


def test_vertical_splines_avoid_nodes_and_point_to_the_original_target():
    """校验完整图按纵轴分层，并检查跨层边、逆向边和自环。"""
    sizes = {name: (130 + index * 8, 38 + index * 5) for index, name in enumerate("abcde")}
    edges = tuple(GraphEdge(source, target) for source, target in (
        ("a", "b"), ("b", "c"), ("c", "d"), ("a", "d"), ("d", "b"), ("c", "c"), ("b", "e"),
    ))
    layout = build_graph_layout(sizes, edges, color="#334455")
    assert layout.nodes["a"].bottom < layout.nodes["b"].y
    for edge in edges:
        branch = layout.branches[edge.key]
        source, target = layout.nodes[edge.source], layout.nodes[edge.target]
        forward = source.y < target.y
        assert branch.commands[0].points == (branch.start,)
        assert branch.start[1] == (source.bottom if forward or edge.source == edge.target else source.y)
        assert branch.end[1] == (target.y if forward else target.bottom)
        assert branch.arrow[0] == branch.end
        assert target.x <= branch.end[0] <= target.right
        assert (branch.arrow[1][1] < branch.end[1]) == forward


def test_converging_lines_join_their_own_arrow_bases():
    """多条横向偏移较大的连线汇入同一卡片时，线身从各自的底边正中接入。"""
    sizes = {name: (150, 38) for name in ("a", "b", "c", "d", "e", "target")}
    edges = tuple(GraphEdge(source, "target") for source in "abcde")
    layout = build_graph_layout(sizes, edges, color="#334455")
    tips = set()
    for branch in layout.branches.values():
        (x, y), = branch.commands[-1].points
        assert x == branch.arrow[0][0] and y == branch.arrow[1][1] + 1
        tips.add(branch.arrow[0])
    assert len(tips) == len(edges)


def test_free_movement_reroutes_incident_edges_and_preserves_others():
    edges = (GraphEdge("a", "b"), GraphEdge("b", "a"), GraphEdge("b", "c"), GraphEdge("a", "a"))
    initial = build_graph_layout(dict.fromkeys("abc", (80, 32)), edges, color="#334455")
    moved = move_graph_nodes(initial, edges, {"a": (400, 120)})
    assert moved.nodes["a"].x == 400 and moved.nodes["a"].y == 120
    assert moved.nodes["b"] == initial.nodes["b"]
    assert moved.branches["b->c"] == initial.branches["b->c"]
    assert moved.branches["a->b"].start != moved.branches["b->a"].end
    assert moved.branches["a->a"].arrow[0][1] == moved.nodes["a"].bottom
