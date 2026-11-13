"""从图数据生成与树页共用的节点和连线几何。"""

from ...geometry import LayoutResult
from ...graph import NodeGraph
from .geometry import build_graph_layout, move_graph_nodes

__all__ = ["build_graph_layout", "graph_layout", "move_graph_nodes"]


def graph_layout(graph: NodeGraph, automatic: LayoutResult, *, show_all=False) -> LayoutResult:
    """在完整布局上应用手动坐标及筛选，筛选切换时保持画布内容范围。"""
    layout = move_graph_nodes(automatic, graph.edges, graph.positions)
    if show_all or not graph.hidden_node_ids:
        return layout
    hidden = graph.hidden_node_ids
    branches = {edge.key: layout.branches[edge.key] for edge in graph.edges
                if edge.source not in hidden and edge.target not in hidden}
    return LayoutResult(layout.nodes, branches, layout.bounds)
