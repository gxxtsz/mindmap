"""亿图真实协议字段的解析回归；只依赖核心与格式包，不加载 Qt。"""

from xml.sax.saxutils import escape

import pytest
from mindmap_core import MindMapError
from mindmap_formats import create_default_format_registry, import_edraw_xml


def topic(key, text="主题", *, parent=None, children="", kind="SubTopic", content=None):
    """构造亿图 Shape/LevelData 样本，测试顺序和引用时不依赖本机亿图安装。"""
    level = (f'<Super V="{parent}"/>' if parent is not None else "") + f'<SubLevel V="{children}"/>'
    content = content if content is not None else f"<TextBlock><Text><pp><tp>{escape(text)}</tp></pp></Text></TextBlock>"
    return f'<Shape ID="{key}" Type="{kind}"><Text>{content}</Text><LevelData>{level}</LevelData></Shape>'


def clipboard(*shapes):
    return ('<?xml version="1.0" encoding="UTF-8"?><Clipboard Version="14.0.4" Page="100">'
            + "<Shapes>" + "".join(shapes) + "</Shapes></Clipboard>").encode()


def test_tree_order_external_references_and_plain_defaults():
    """来源 XML 顺序不同于 SubLevel；源样式与非主题对象不能进入结果。"""
    data = clipboard(
        topic("1", "中心😀", kind="MainIdea", children="3;999;2"),
        topic("2", "重复标题", parent="1", children="4"),
        topic("3", "重复标题", parent="1"),
        topic("4", "最末层", parent="2"),
        '<Shape ID="10" Type="MMConnector"><Text><HtmlText>不导入</HtmlText></Text></Shape>',
        topic("11", "概要标题", kind="SummaryTopic"),
        topic("12", "装饰", kind="Image"),
    ).replace(b'<Text>', b'<Layout><Toggler V="1"/></Layout><ShapeFormat Color="red"/><Text>', 1)
    document = import_edraw_xml(data)
    root = document.nodes[document.root.children[0]]
    children = [document.nodes[key] for key in root.children]
    assert root.text == "中心😀"
    assert [node.text for node in children] == ["重复标题", "重复标题"]
    assert not children[0].children
    assert document.nodes[children[1].children[0]].text == "最末层"
    assert len(document.nodes) == 5 and not document.assets
    for node in document.nodes.values():
        assert node.id not in {"1", "2", "3", "4", "10", "11", "12"}
        assert node.expanded and node.direction is None and node.text_format == "plain"
        assert not any((node.style, node.note, node.hyperlink, node.image_id, node.tags, node.icons, node.metadata))


def test_forest_and_parent_only_relationships_do_not_fold_roots_together():
    document = import_edraw_xml(clipboard(
        topic("20", "并列一", parent="999", kind="MainTopic"),
        topic("21", "一的后代", parent="20"),
        topic("30", "并列二", kind="Floating"),
        topic("31", "二的后代", parent="30"),
    ))
    roots = [document.nodes[key] for key in document.root.children]
    assert [node.text for node in roots] == ["并列一", "并列二"]
    assert [document.nodes[node.children[0]].text for node in roots] == ["一的后代", "二的后代"]


@pytest.mark.parametrize(("content", "expected"), [
    ('<TextBlock><Text><pp><tp>中</tp><tp>文 &amp; 😀</tp></pp><pp/><pp><tp>尾行</tp></pp></Text></TextBlock>',
     "中文 & 😀\n\n尾行"),
    ('<TextBlock><Text>\n  <pp>\n    <tp>  原有空格 </tp>\n    <tp>和文字</tp>\n  </pp>\n</Text></TextBlock>',
     "  原有空格 和文字"),
    ("<HtmlText>" + escape('<html><head><style>p { color:red; }</style></head><body>'
                          '<p><span>中文 &amp; 😀</span><br/>第二行</p>'
                          '<p><a href="https://example.com">第三行</a></p><script>不导入</script>'
                          '</body></html>') + "</HtmlText>", "中文 & 😀\n第二行\n第三行"),
    ("<HtmlText>" + escape("<p>  前后空格  </p>") + "</HtmlText>", "  前后空格  "),
    ("<HtmlText>" + escape("<p>A<br/></p>") + "</HtmlText>", "A\n"),
    ("<TextBlock/>", ""),
])
def test_text_encodings_paragraphs_and_html(content, expected):
    document = import_edraw_xml(clipboard(topic("1", content=content)))
    assert document.nodes[document.root.children[0]].text == expected


def test_deep_tree_uses_no_recursive_conversion():
    count = 1500
    document = import_edraw_xml(clipboard(*[
        topic(str(i), str(i), parent=str(i - 1) if i > 1 else None) for i in range(1, count + 1)
    ]))
    assert len(document.nodes) == count + 1
    ordered = document.descendants(document.root_id)[1:]
    assert [document.nodes[key].text for key in ordered] == [str(i) for i in range(1, count + 1)]


@pytest.mark.parametrize("data", [
    b"", b"<broken>", b"<Other/>", b"<Clipboard/>", clipboard(),
    clipboard(topic("1"), topic("1")),
    clipboard(topic("")),
    clipboard(topic("1", children="2;2"), topic("2")),
    clipboard(topic("1", children="2"), topic("2", parent="3"), topic("3")),
    clipboard(topic("1", parent="2"), topic("2", parent="1")),
    clipboard(topic("1", children="1")),
    clipboard(topic("1"), topic("2", parent="3"), topic("3", parent="2")),
    clipboard(topic("1", parent="2;3"), topic("2"), topic("3")),
    b'<!DOCTYPE Clipboard [<!ENTITY text "bad">]><Clipboard><Shapes/></Clipboard>',
    b'<?xml version="1.0" encoding="unknown-encoding"?><Clipboard><Shapes/></Clipboard>',
])
def test_bad_input_is_rejected(data):
    with pytest.raises(MindMapError, match="亿图"):
        import_edraw_xml(data)


def test_capacity_limit_and_default_registry(monkeypatch):
    from mindmap_formats import edraw

    data = clipboard(topic("1"))
    monkeypatch.setattr(edraw, "MAX_ARCHIVE_BYTES", len(data) - 1)
    with pytest.raises(MindMapError, match="大小超过"):
        import_edraw_xml(data)
    assert {entry.info.id for entry in create_default_format_registry().list()} == {
        "json", "mindmap", "markdown", "text"
    }
