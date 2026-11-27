"""验证逐行格式的正反转换、行号和解析原子性。"""

import pytest
from mindmap_core import GraphEdge, GraphGroup, GraphNode, MindMapError, NodeGraph
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


def test_graph_group_text_roundtrips_quoted_names_and_forward_references():
    source = '分组 team "核心 \\"模块\\"" app core\n节点 core "核心"\n节点 app "应用"\napp -> core'
    graph = parse_graph_text(source)
    assert graph.groups == (GraphGroup("team", ("app", "core")),)
    assert graph.nodes[-1] == GraphNode("team", '核心 "模块"')
    output = format_graph_text(graph)
    assert '分组 team app core' in output and parse_graph_text(output) == graph


def test_nested_and_empty_groups_roundtrip_with_explicit_nodes():
    source = '分组 g h a\n分组 h\n节点 a "甲"\n节点 g "外层"\n节点 h "空组"\na -> h'
    graph = parse_graph_text(source)
    assert graph.groups == (GraphGroup("g", ("h", "a")), GraphGroup("h"))
    assert parse_graph_text(format_graph_text(graph)) == graph


@pytest.mark.parametrize(("text", "message"), [
    ('节点 A "甲"\n节点 A "乙"', "第 2 行.*标识重复"),
    ('节点 a "甲"\na -> a\n# 注释\na->a', "第 4 行.*连线重复"),
    ('a -> b\n节点 a "甲"', "第 1 行.*未声明.*b"),
    ('节点 a "甲"\nA -> a', "第 2 行.*未声明.*A"),
    ('节点 2a "甲"', "第 1 行.*节点声明"),
    ('节点 a 123', "第 1 行.*双引号"),
    ('节点 a "结束" # 注释', "第 1 行.*双引号"),
    ('节点 a "\\ud800"', "第 1 行.*Unicode"),
    ('节点 a "甲"\n分组 g "组"', "第 2 行.*至少需要一个成员"),
    ('分组 g "   " a\n节点 a "甲"', "第 1 行.*分组名称"),
    ('分组 g "组\\n名" a\n节点 a "甲"', "第 1 行.*单行"),
    ('分组 g "组" missing\n节点 a "甲"', "第 1 行.*未声明.*missing"),
    ('分组 g "组" a a\n节点 a "甲"', "第 1 行.*成员重复.*a"),
    ('节点 a "甲"\n分组 g "甲组" a\n分组 h "乙组" a', "第 3 行.*成员重复.*a"),
    ('节点 a "甲"\n节点 b "乙"\n分组 g "甲组" a\n分组 g "乙组" b', "第 4 行.*标识无效或重复"),
    ('节点 a "甲"\n节点 g "组"\n分组 g a\ng -> a', "第 4 行.*同一层"),
    ('节点 g "组"\n分组 g g', "第 2 行.*形成循环"),
    ('节点 g "组"\n分组 g no_such_node', "第 2 行.*未声明"),
])
def test_graph_text_errors_name_original_line(text, message):
    with pytest.raises(MindMapError, match=message):
        parse_graph_text(text)
