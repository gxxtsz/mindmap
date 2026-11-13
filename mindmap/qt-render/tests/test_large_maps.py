"""按真实能力归属迁移的组件与行为回归。"""

from copy import deepcopy

from mindmap_core import MindMapDocument, Node
from mindmap_qt_render import rendering
from mindmap_qt_render.rendering import VisualFactory


def document_with_nodes(count):
    """直接建模避开造数据期间的编辑通知，每个标题不同以覆盖内容缓存临界点。"""
    document = MindMapDocument.new("大图回归")
    keys = [document.root_id]
    for index in range(1, count):
        node = Node(f"不同内容的节点 {index}")
        document.nodes[keys[(index - 1) // 5]].children.append(node.id)
        document.nodes[node.id] = node
        keys.append(node.id)
    return document, keys


def test_text_layout_survives_more_than_4096_distinct_nodes(qapp, monkeypatch):
    document, keys = document_with_nodes(4100)
    calls = []
    original = rendering.make_text

    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(rendering, "make_text", measured)
    factory = VisualFactory()
    first, _ = factory.measure(document)
    assert len(calls) == 4100
    second, _ = factory.measure(deepcopy(document))
    assert len(calls) == 4100
    assert all(second[key] is first[key] for key in keys)
    document.nodes[keys[-1]].text = "确实修改正文"
    third, _ = factory.measure(document)
    assert len(calls) == 4101 and third[keys[-1]] is not first[keys[-1]]


def test_rejected_candidate_does_not_supply_unchanged_committed_visuals(qapp):
    document, keys = document_with_nodes(20)
    factory = VisualFactory()
    committed, _ = factory.measure(document)
    candidate = deepcopy(document)
    candidate.nodes[keys[1]].text = "很长的候选内容" * 15
    trial, _ = factory.measure(candidate)
    assert trial[keys[1]] is not committed[keys[1]]
    # 试算被拒绝后修改另一节点；增量基线必须是正式视觉，不能使用最近的试算缓存。
    actual = deepcopy(document)
    actual.nodes[keys[-1]].text = "另一次正式修改"
    updated, _ = factory.measure(
        actual, previous_document=document, previous_visuals=committed, changed_node_ids=[keys[-1]],
    )
    assert updated[keys[1]] is committed[keys[1]]
    actual.theme.accent = "#ff6600"
    themed, _ = factory.measure(
        actual, previous_document=document, previous_visuals=committed, changed_node_ids=[],
    )
    assert themed[document.root_id] is not committed[document.root_id]
