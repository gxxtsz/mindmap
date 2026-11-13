"""从图数据生成与树页共用的节点和连线几何。"""

from ...geometry import LayoutResult
from ...graph import NodeGraph
from .geometry import build_graph_layout, move_graph_nodes

__all__ = ["build_graph_layout", "graph_layout", "move_graph_nodes"]


def graph_layout(graph: NodeGraph, automatic: LayoutResult) -> LayoutResult:
    """将图的手动坐标应用到分层布局，重新计算受影响的连线及完整内容范围。"""
    return move_graph_nodes(automatic, graph.edges, graph.positions)
