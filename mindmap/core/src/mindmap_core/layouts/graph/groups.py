"""分层安排组框和节点卡片，所有几何使用场景逻辑像素。"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from itertools import chain

from ...geometry import GraphGroupFrame, LayoutResult, Point, Rect
from ...graph import GraphEdge, GraphGroup
from .geometry import _layout_result, _move_graph_edge, build_graph_layout

_PADDING = 16
_HEADER_PADDING = 8
_MIN_FRAME_WIDTH = 160
_MIN_FRAME_HEIGHT = 100


def _scopes(keys: Iterable[str], edges: tuple[GraphEdge, ...], groups: tuple[GraphGroup, ...]):
    """按验过的父组建立各层的直属成员和边，并从顶层迭代遍历。"""
    parents = {key: group.id for group in groups for key in group.member_ids}
    members = {None: tuple(key for key in keys if key not in parents)}
    members.update({group.id: group.member_ids for group in groups})
    links = defaultdict(list)
    for edge in edges:
        links[parents.get(edge.source)].append(edge)
    nested = defaultdict(list)
    for group in groups:
        nested[parents.get(group.id)].append(group.id)
    order = [None]
    for scope in order:
        order.extend(nested[scope])
    return members, links, order


def _contents(boxes, branches) -> Rect | None:
    """测量一层的节点、嵌套框和组内箭头，仅空层返回 None。"""
    bounds = None
    for rect in chain(boxes.values(), (branch.bounds for branch in branches.values())):
        bounds = rect if bounds is None else bounds.union(rect)
    return bounds


def _frame(group: GraphGroup, origin: Point, title_size: Point, content_size: Point) -> GraphGroupFrame:
    """用标题和实际内容计算有效框，同时给缩放手柄提供内容下限。"""
    title_width, title_height = title_size
    header = title_height + 2 * _HEADER_PADDING
    width, height = content_size
    required = (max(_MIN_FRAME_WIDTH, title_width + 2 * _PADDING, width + _PADDING),
                max(_MIN_FRAME_HEIGHT, header + 2 * _PADDING, height + _PADDING))
    minimum = group.min_size or (0, 0)
    return GraphGroupFrame(Rect(*origin, *map(max, required, minimum)), header, required)


def _title_box(frame: GraphGroupFrame, size: Point) -> Rect:
    return Rect(frame.bounds.x + _PADDING, frame.bounds.y + _HEADER_PADDING, *size)


def build_grouped_layout(
    sizes: dict[str, Point], edges: tuple[GraphEdge, ...], groups: tuple[GraphGroup, ...], *, color: str,
) -> LayoutResult:
    """对已经校验的嵌套图由内向外计算 DOT 布局。

    sizes 为每个节点卡片的宽高。包含分组的同级布局用组框宽高占位，连线以框为
    边界。返回的 nodes 提供实际卡片位置，group_frames 另外保存外框和投放区。
    """
    by_id = {group.id: group for group in groups}
    members, links, order = _scopes(sizes, edges, groups)
    layouts, frames, offsets = {}, {}, {}
    for scope in reversed(order):
        scoped_sizes = {
            key: (frames[key].bounds.width, frames[key].bounds.height) if key in frames else sizes[key]
            for key in members[scope]
        }
        layout = build_graph_layout(scoped_sizes, tuple(links[scope]), color=color)
        layouts[scope] = layout
        if scope is None:
            continue
        content = _contents(layout.nodes, layout.branches) or Rect(0, 0, 0, 0)
        header = sizes[scope][1] + 2 * _HEADER_PADDING
        frames[scope] = _frame(by_id[scope], (0, 0), sizes[scope],
                               (content.width + _PADDING, content.height + header + _PADDING))
        offsets[scope] = _PADDING - content.x, header + _PADDING - content.y

    nodes, branches, placed = {}, {}, {}
    origins = {None: (0.0, 0.0)}
    for scope in order:
        dx, dy = origins[scope]
        layout = layouts[scope]
        for key, rect in layout.nodes.items():
            rect = rect.translated(dx, dy)
            if key in frames:
                frame = replace(frames[key], bounds=rect)
                placed[key] = frame
                nodes[key] = _title_box(frame, sizes[key])
                x, y = offsets[key]
                origins[key] = rect.x + x, rect.y + y
            else:
                nodes[key] = rect
        branches.update({key: branch.translated(dx, dy) for key, branch in layout.branches.items()})
    return _layout_result({key: nodes[key] for key in sizes}, branches,
                          {key: placed[key] for key in order if key is not None})


def fit_graph_groups(
    layout: LayoutResult, edges: tuple[GraphEdge, ...], groups: tuple[GraphGroup, ...],
    positions: Mapping[str, Point],
) -> LayoutResult:
    """保持给定的场景坐标，按实际卡片、子组和连线重新贴合组框。

    positions 覆盖节点卡片左上角。组标题未定位时沿内容的左上侧摆放，已定位时
    保留该锚点并允许向外扩张以容纳内容。重新布置的框和卡片会更新同级连线。
    """
    by_id = {group.id: group for group in groups}
    members, links, order = _scopes(layout.nodes, edges, groups)
    nodes = dict(layout.nodes)
    for key, (x, y) in positions.items():
        if key in nodes:
            nodes[key] = replace(nodes[key], x=x, y=y)
    frames, branches = {}, {}
    for scope in reversed(order):
        boxes = {key: frames[key].bounds if key in frames else nodes[key] for key in members[scope]}
        old_boxes = {
            key: layout.group_frames[key].bounds if key in layout.group_frames else layout.nodes[key]
            for key in boxes
        }
        changed = {key for key, rect in boxes.items() if rect != old_boxes[key]}
        scoped_branches = {}
        edge_set = set(links[scope])
        for edge in links[scope]:
            old = layout.branches[edge.key]
            scoped_branches[edge.key] = _move_graph_edge(
                old, old_boxes[edge.source], old_boxes[edge.target], boxes[edge.source], boxes[edge.target],
                loop=edge.source == edge.target,
                paired=GraphEdge(edge.target, edge.source) in edge_set,
            ) if edge.source in changed or edge.target in changed else old
        branches.update(scoped_branches)
        if scope is None:
            continue
        title = nodes[scope]
        x, y = title.x - _PADDING, title.y - _HEADER_PADDING
        content = _contents(boxes, scoped_branches)
        extent = (0, 0)
        if content is not None:
            left = content.x - _PADDING
            top = content.y - 2 * _HEADER_PADDING - title.height - _PADDING
            x, y = (min(x, left), min(y, top)) if scope in positions else (left, top)
            extent = content.right - x, content.bottom - y
        frame = _frame(by_id[scope], (x, y), (title.width, title.height), extent)
        frames[scope] = frame
        nodes[scope] = _title_box(frame, (title.width, title.height))
    return _layout_result(nodes, branches, {key: frames[key] for key in order if key is not None})
