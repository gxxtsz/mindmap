"""从图数据生成与树页共用的节点和连线几何。"""

from ...geometry import LayoutResult, Point
from ...graph import GraphEdge, GraphGroup, NodeGraph
from .geometry import build_graph_layout as _build_graph_layout
from .geometry import graph_child_position, move_graph_nodes
from .groups import build_grouped_layout, fit_graph_groups

__all__ = ["build_graph_layout", "fit_graph_groups", "graph_child_position", "graph_layout", "move_graph_nodes"]


def build_graph_layout(
    sizes: dict[str, Point], edges: tuple[GraphEdge, ...], *, color: str,
    groups: tuple[GraphGroup, ...] = (),
) -> LayoutResult:
    """从节点实测尺寸、有向边及可选分组计算图的自动布局。

    sizes 提供全部节点卡片的逻辑宽高，包含组标题卡片。存在分组时先排最内层，
    组间使用整个外框占位和接线。
    """
    if groups:
        return build_grouped_layout(sizes, edges, groups, color=color)
    return _build_graph_layout(sizes, edges, color=color)


def graph_layout(graph: NodeGraph, automatic: LayoutResult) -> LayoutResult:
    """把手动坐标叠加到自动布局，重新计算受影响的连线、组框及内容范围。

    automatic 应来自同一图的卡片测量。坐标只覆盖已保存的位置，组框按实际内容
    从内向外重新计算，导出和画布共用这一过程。
    """
    if graph.groups and graph.positions:
        return fit_graph_groups(automatic, graph.edges, graph.groups, graph.positions)
    return move_graph_nodes(automatic, graph.edges, graph.positions)
