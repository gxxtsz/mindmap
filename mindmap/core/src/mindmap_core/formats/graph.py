"""供图页和剪贴板使用的逐行文本，解析过程不接触文件或活动文档。"""

import json
import re

from ..errors import MindMapError
from ..graph import (
    GRAPH_NODE_ID,
    GraphEdge,
    GraphGroup,
    GraphNode,
    NodeGraph,
    graph_edge_in_scope,
    graph_membership_cycle,
    upgrade_legacy_graph_groups,
    validate_graph_group,
    validate_graph_title,
    validate_legacy_graph_group,
)

_NODE = re.compile(rf"节点[ \t]+({GRAPH_NODE_ID})[ \t]+(.+)")
_EDGE = re.compile(rf"({GRAPH_NODE_ID})[ \t]*->[ \t]*({GRAPH_NODE_ID})")
_LEGACY_GROUP = re.compile(rf'分组[ \t]+({GRAPH_NODE_ID})[ \t]+("(?:[^"\\]|\\.)*")(?:[ \t]+(.*))?')
_GROUP = re.compile(rf"分组[ \t]+({GRAPH_NODE_ID})(?:[ \t]+((?:{GRAPH_NODE_ID})(?:[ \t]+{GRAPH_NODE_ID})*))?")
_QT_TEXT_ESCAPES = str.maketrans({"\u00a0": r"\u00a0", "\u2028": r"\u2028", "\u2029": r"\u2029"})


def parse_graph_text(text: str) -> NodeGraph:
    """解析节点、分组及有向连线，返回可直接提交的独立内容，错误携带原文行号。

    声明按原顺序收集，全部行读取后再校验引用，因此分组和连线可写在节点声明前面。
    「节点 标识 "名称"」定义组节点，「分组 标识 成员标识…」定义直接成员。旧版带引号
    组名的分组行可迁移，整份输入须使用同一版分组语法。新语法下拒绝跨层连线。
    只按 LF 或 CRLF 切行，允许标题包含以 JSON 字符串表示的 Unicode 分隔符。
    """
    if not isinstance(text, str):
        raise MindMapError("图文本必须是字符串")
    nodes: dict[str, GraphNode] = {}
    edges: dict[GraphEdge, int] = {}
    groups: list[tuple[GraphGroup, int]] = []
    legacy = []
    for line_no, raw in enumerate(text.removeprefix("\ufeff").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if match := _NODE.fullmatch(line):
            key, label = match.groups()
            if key in nodes:
                raise MindMapError(f"第 {line_no} 行：节点标识重复（{key}）")
            try:
                title = json.loads(label)
                validate_graph_title(title)
            except json.JSONDecodeError as error:
                raise MindMapError(f"第 {line_no} 行：标题应使用有效的双引号字符串") from error
            except MindMapError as error:
                raise MindMapError(f"第 {line_no} 行：{error}") from error
            nodes[key] = GraphNode(key, title)
        elif match := _LEGACY_GROUP.fullmatch(line):
            key, label, members = match.groups()
            try:
                title = json.loads(label)
            except json.JSONDecodeError as error:
                raise MindMapError(f"第 {line_no} 行：分组名称应使用有效的双引号字符串") from error
            legacy.append((key, title, tuple((members or "").split()), line_no))
        elif match := _GROUP.fullmatch(line):
            key, members = match.groups()
            groups.append((GraphGroup(key, tuple((members or "").split())), line_no))
        elif match := _EDGE.fullmatch(line):
            edge = GraphEdge(*match.groups())
            if edge in edges:
                raise MindMapError(f"第 {line_no} 行：连线重复（{edge.key}）")
            edges[edge] = line_no
        else:
            raise MindMapError(f'第 {line_no} 行：请输入节点声明、分组或连线，例如 节点 app "应用"、'
                               '分组 g1 app 或 app -> core')
    for edge, line_no in edges.items():
        for key in (edge.source, edge.target):
            if key not in nodes:
                raise MindMapError(f"第 {line_no} 行：连线引用了未声明的节点（{key}）")
    group_ids: set[str] = set()
    grouped_ids: set[str] = set()
    if legacy and groups:
        raise MindMapError(f"第 {max(legacy[0][3], groups[0][1])} 行：请统一使用一种分组声明")
    if legacy:
        for key, title, members, line_no in legacy:
            try:
                validate_legacy_graph_group(key, title, members, nodes, group_ids, grouped_ids)
            except MindMapError as error:
                raise MindMapError(f"第 {line_no} 行：{error}") from error
            group_ids.add(key)
            grouped_ids.update(members)
        return upgrade_legacy_graph_groups(NodeGraph(tuple(nodes.values()), tuple(edges)),
                                           [(key, title, members) for key, title, members, _ in legacy])
    for group, line_no in groups:
        try:
            validate_graph_group(group, node_ids=nodes, group_ids=group_ids, grouped_ids=grouped_ids)
        except MindMapError as error:
            raise MindMapError(f"第 {line_no} 行：{error}") from error
        group_ids.add(group.id)
        grouped_ids.update(group.member_ids)
    graph = NodeGraph(tuple(nodes.values()), tuple(edges), groups=tuple(group for group, _ in groups))
    parents = graph.parent_groups()
    if (cycle := graph_membership_cycle(parents)) is not None:
        line_no = next(line for group, line in groups if group.id == cycle)
        raise MindMapError(f"第 {line_no} 行：分组不能包含自身或形成循环")
    for edge, line_no in edges.items():
        if not graph_edge_in_scope(edge, parents):
            raise MindMapError(f"第 {line_no} 行：连线的两端必须处于同一层（{edge.key}）")
    return graph


def format_graph_text(graph: NodeGraph) -> str:
    """生成节点、连线和分组文本，各自保留声明顺序。

    格式、宽度、附页、坐标及组框最小宽高由原生记录保存。分组行只声明成员，正文
    来自普通节点行。省去成员可声明空组。
    """
    # 文本控件会将特殊空格或分隔符转换为普通空格或换行。只转义这些字符，
    # 中文标题仍直接显示，生成的文本在编辑器和剪贴板中可以无损往返。
    lines = [f"节点 {node.id} {json.dumps(node.title, ensure_ascii=False).translate(_QT_TEXT_ESCAPES)}"
             for node in graph.nodes]
    if lines and graph.edges:
        lines.append("")
    lines.extend(f"{edge.source} -> {edge.target}" for edge in graph.edges)
    if lines and graph.groups:
        lines.append("")
    lines.extend(" ".join(("分组", group.id, *group.member_ids)) for group in graph.groups)
    return "\n".join(lines)
