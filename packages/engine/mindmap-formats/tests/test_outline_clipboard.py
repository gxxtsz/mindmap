"""缩进片段的多根层级、格式识别、混用消歧与原文行号验收。"""

import pytest
from mindmap_core import MindMapError
from mindmap_formats import (
    decode_document,
    detect_outline_format,
    import_indented_fragment,
    import_outline,
)


def fragment_rows(fragment):
    """按片段原文顺序读取深度与正文，不将合成根纳入断言。"""
    rows = []
    stack = [(key, 0) for key in reversed(fragment.root.children)]
    while stack:
        key, depth = stack.pop()
        node = fragment.nodes[key]
        rows.append((depth, node.text))
        stack.extend((child, depth + 1) for child in reversed(node.children))
    return rows


EXPECTED_ROWS = [(0, "甲"), (1, "开发"), (2, "页面"), (1, "测试"), (0, "乙"), (1, "想法")]


@pytest.mark.parametrize("indent", ["\t", "  ", "    "])
@pytest.mark.parametrize("baseline", ["", "   ", "\t", "\t    "])
def test_indented_fragment_preserves_forest_order_and_common_indentation(indent, baseline):
    """每种缩进均保持多根、同级及深层层级，整段共同缩进不会产生额外节点。"""
    lines = ["甲", indent + "开发", indent * 2 + "页面", indent + "测试", "乙", indent + "想法"]
    fragment = import_indented_fragment("\n".join(baseline + line for line in lines))
    assert fragment_rows(fragment) == EXPECTED_ROWS
    assert len(fragment.root.children) == 2 and len(fragment.nodes) == 7
    fragment.validate()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_indented_fragment_accepts_bom_blank_lines_and_common_newlines(newline, bom):
    fragment = import_indented_fragment(bom + newline.join(["", "甲", " \t", "  孩子", "", "乙", ""]))
    assert fragment_rows(fragment) == [(0, "甲"), (1, "孩子"), (0, "乙")]


def test_indented_fragment_preserves_body_whitespace_symbols_and_independent_styles():
    """只裁剪缩进与行尾空白；正文符号和内部 Tab 不解释，节点默认样式互相独立。"""
    fragment = import_indented_fragment("主题  正文\t尾部  \n  ├── 字面字符\n  #正文\n  -正文\n  1. 正文")
    assert fragment_rows(fragment) == [
        (0, "主题  正文\t尾部"), (1, "├── 字面字符"), (1, "#正文"), (1, "-正文"), (1, "1. 正文"),
    ]
    root = fragment.nodes[fragment.root.children[0]]
    child = fragment.nodes[root.children[0]]
    child.style["branch"]["kind"] = "line"
    assert root.style["branch"] == fragment.root.style["branch"] == {"kind": "round"}


@pytest.mark.parametrize("text", [
    "甲\n\t开发\n\t  页面\n  测试\n乙\n  想法",
    "甲\n    开发\n\t    页面\n\t测试\n乙\n    想法",
])
def test_indented_fragment_accepts_uniquely_determined_mixed_indentation(text):
    assert fragment_rows(import_indented_fragment(text)) == EXPECTED_ROWS


@pytest.mark.parametrize("text,line", [
    ("甲\n\t乙\n    丙", 3),
    ("\n甲\n\n\t乙\n    丙", 5),
    ("\t甲\n    乙", 2),
])
def test_indented_fragment_rejects_different_valid_tree_interpretations(text, line):
    with pytest.raises(MindMapError, match=rf"第 {line} 行：.*歧义"):
        import_indented_fragment(text)


@pytest.mark.parametrize("text,line,reason", [
    ("甲\n   乙", 2, "每级 2 个空格"),
    ("甲\n  乙\n      丙", 3, "层级跳跃，缺少父节点"),
    ("甲\n    乙\n          丙", 3, "每级 4 个空格"),
    ("\n甲\n\n  乙\n     丙", 5, "每级 2 个空格"),
    ("    甲\n乙", 1, "首个节点缺少父节点"),
    ("甲\n\t乙\n      丙", 3, "层级跳跃，缺少父节点"),
    ("甲\n  乙\n    丙\n   丁", 4, "每级 2 个空格"),
])
def test_indented_fragment_reports_original_line_from_longest_valid_prefix(text, line, reason):
    with pytest.raises(MindMapError, match=rf"第 {line} 行：.*{reason}"):
        import_indented_fragment(text)


@pytest.mark.parametrize("text", ["", " \t", "\ufeff\r\n \t\r"])
def test_indented_fragment_rejects_empty_content(text):
    with pytest.raises(MindMapError, match="^大纲内容不能为空$"):
        import_indented_fragment(text)


def test_text_codec_fragment_option_preserves_default_single_root_contract():
    """默认导入仍将首行作为文档根；片段选项才返回可直接粘贴的合成根。"""
    content = "甲\n  孩子".encode()
    document = decode_document(content, format_id="text")
    assert document.root.text == "甲"
    assert document.nodes[document.root.children[0]].text == "孩子"
    fragment = decode_document(content, format_id="text", options={"as_fragment": True})
    assert fragment_rows(fragment) == [(0, "甲"), (1, "孩子")]
    forest = decode_document("甲\n乙".encode(), format_id="text", options={"as_fragment": True})
    assert fragment_rows(forest) == [(0, "甲"), (0, "乙")]


@pytest.mark.parametrize("option", ["true", 1, None])
def test_text_codec_rejects_non_boolean_fragment_option(option):
    with pytest.raises(MindMapError, match="as_fragment 必须为布尔值"):
        decode_document("甲".encode(), format_id="text", options={"as_fragment": option})


@pytest.mark.parametrize("text", [
    "# 标题\n- 子项", "\ufeff\n  ## 标题", "###### 标题", "- 条目", "* 条目", "+ 条目",
    "1. 条目", "12) 条目", "#\t标题",
])
def test_outline_detection_recognizes_supported_markdown_markers(text):
    assert detect_outline_format(text) == "markdown"


@pytest.mark.parametrize("text", [
    "甲\n  乙", "#标签", "####### 正文", "-正文", "*正文", "+正文", "1.正文",
    "正文\n  - 条目", "\ufeff\n\t ",
])
def test_outline_detection_keeps_plain_symbols_and_empty_content_as_text(text):
    assert detect_outline_format(text) == "text"


@pytest.mark.parametrize("text,line,reason", [
    ("\n# 根\n\n   - 奇数缩进", 4, "缩进"),
    ("\n# 根\n\n      - 跳级", 4, "层级"),
    ("\n## 根\n\n# 提前的根", 4, "层级"),
    ("\n# 根\n\n普通正文", 4, "只支持标题或列表条目"),
])
def test_markdown_outline_errors_report_original_line(text, line, reason):
    with pytest.raises(MindMapError, match=rf"第 {line} 行：.*{reason}"):
        import_outline(text)
