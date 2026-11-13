"""验证逐行格式的正反转换、行号和解析原子性。"""

import pytest
from mindmap_core import GraphEdge, GraphNode, MindMapError, NodeGraph
from mindmap_core.formats.graph import format_graph_text, parse_graph_text


def test_graph_text_accepts_forward_edges_cycles_and_escaped_chinese():
    text = '\ufeff# 示例\r\nApp->core-x\r\n\r\n节点 core-x "核心\\n\\"引号\\""\r\n节点 App "应用"\r\n'
    graph = parse_graph_text(text + '节点 _alone ""\r\nApp -> App\r\ncore-x -> App')
    assert graph.nodes == (GraphNode("core-x", '核心\n"引号"'), GraphNode("App", "应用"), GraphNode("_alone", ""))
    assert graph.edges == (GraphEdge("App", "core-x"), GraphEdge("App", "App"), GraphEdge("core-x", "App"))
    assert parse_graph_text(format_graph_text(graph)) == graph
    assert parse_graph_text("# 空图\n") == NodeGraph()


def test_canonical_text_escapes_characters_normalized_by_text_widgets():
    graph = NodeGraph((GraphNode("a", "甲\u00a0乙\u2028丙\u2029丁"),))
    text = format_graph_text(graph)
    assert text == r'节点 a "甲\u00a0乙\u2028丙\u2029丁"'
    assert parse_graph_text(text) == graph


@pytest.mark.parametrize(("text", "message"), [
    ('节点 A "甲"\n节点 A "乙"', "第 2 行.*标识重复"),
    ('节点 a "甲"\na -> a\n# 注释\na->a', "第 4 行.*连线重复"),
    ('a -> b\n节点 a "甲"', "第 1 行.*未声明.*b"),
    ('节点 a "甲"\nA -> a', "第 2 行.*未声明.*A"),
    ('节点 2a "甲"', "第 1 行.*节点声明"),
    ('节点 a 123', "第 1 行.*双引号"),
    ('节点 a "结束" # 注释', "第 1 行.*双引号"),
    ('节点 a "\\ud800"', "第 1 行.*Unicode"),
])
def test_graph_text_errors_name_original_line(text, message):
    with pytest.raises(MindMapError, match=message):
        parse_graph_text(text)
