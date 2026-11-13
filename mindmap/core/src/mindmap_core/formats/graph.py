"""供图页和剪贴板使用的逐行文本，解析过程不接触文件或活动文档。"""

import json
import re

from ..errors import MindMapError
from ..graph import GRAPH_NODE_ID, GraphEdge, GraphNode, NodeGraph, validate_graph_title

_NODE = re.compile(rf"节点[ \t]+({GRAPH_NODE_ID})[ \t]+(.+)")
_EDGE = re.compile(rf"({GRAPH_NODE_ID})[ \t]*->[ \t]*({GRAPH_NODE_ID})")
_QT_TEXT_ESCAPES = str.maketrans({"\u00a0": r"\u00a0", "\u2028": r"\u2028", "\u2029": r"\u2029"})


def parse_graph_text(text: str) -> NodeGraph:
    """解析节点及有向连线，返回可直接提交的独立内容，错误携带原文行号。

    声明按原顺序收集，全部行读取后再校验端点，因此连线可以写在节点声明前面。
    只按 LF 或 CRLF 切行，允许标题包含以 JSON 字符串表示的 Unicode 分隔符。
    """
    if not isinstance(text, str):
        raise MindMapError("图文本必须是字符串")
    nodes: dict[str, GraphNode] = {}
    edges: dict[GraphEdge, int] = {}
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
        elif match := _EDGE.fullmatch(line):
            edge = GraphEdge(*match.groups())
            if edge in edges:
                raise MindMapError(f"第 {line_no} 行：连线重复（{edge.key}）")
            edges[edge] = line_no
        else:
            raise MindMapError(f"第 {line_no} 行：请输入节点声明或有向连线，例如 节点 app \"应用\" 或 app -> core")
    for edge, line_no in edges.items():
        for key in (edge.source, edge.target):
            if key not in nodes:
                raise MindMapError(f"第 {line_no} 行：连线引用了未声明的节点（{key}）")
    return NodeGraph(tuple(nodes.values()), tuple(edges))


def format_graph_text(graph: NodeGraph) -> str:
    """按声明顺序生成节点标题与连线文本，附页绑定和手动坐标由原生文档保存。"""
    # 文本控件会将特殊空格或分隔符转换为普通空格或换行。只转义这些字符，
    # 中文标题仍直接显示，生成的文本在编辑器和剪贴板中可以无损往返。
    lines = [f"节点 {node.id} {json.dumps(node.title, ensure_ascii=False).translate(_QT_TEXT_ESCAPES)}"
             for node in graph.nodes]
    if lines and graph.edges:
        lines.append("")
    lines.extend(f"{edge.source} -> {edge.target}" for edge in graph.edges)
    return "\n".join(lines)
