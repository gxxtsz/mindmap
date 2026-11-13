"""覆盖图的事务、存储校验和复制时的所有权。"""

from dataclasses import replace

import pytest
from mindmap_core import GraphEdge, GraphNode, MindMapEngine, MindMapError, NodeGraph, OperationRejected
from mindmap_core.formats import from_dict, load_document, save_document, to_dict
from mindmap_core.node_pages import graph_page_id


def test_graph_persists_with_owner_and_undo_uses_graph_page_identity(tmp_path):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "图入口")
    graph = NodeGraph((GraphNode("a", "甲"),), (GraphEdge("a", "a"),), {"a": (12, 35)})
    events = []
    engine.subscribe(events.append)
    engine.set_node_graph(owner, graph)
    page = graph_page_id(owner)
    assert events[-1].changes.page_id == page
    assert engine.page_summary(page).kind == "graph"
    assert engine.page_document(page).graph == graph
    snapshot = engine.node_graph(owner)
    snapshot.positions.clear()
    assert engine.node_graph(owner).positions == graph.positions
    path = tmp_path / "导图.mindmap"
    save_document(engine.document, path)
    restored = MindMapEngine(load_document(path))
    assert restored.node_graph(owner) == graph
    copy, = restored.copy_nodes([owner], restored.root_id)
    assert graph_page_id(copy) != page and restored.node_graph(copy) == graph
    restored.remove_node_graph(owner)
    assert restored.page_summary(page) is None and restored.node_graph(copy) == graph
    assert restored.undo() and restored.node_graph(owner) == graph
    assert restored.redo() and restored.node_graph(owner) is None


@pytest.mark.parametrize("patch", [
    {"nodes": (GraphNode("a", ""), GraphNode("a", "重复"))},
    {"edges": (GraphEdge("a", "missing"),)},
    {"positions": {"a": (float("inf"), 0)}},
    {"positions": {"a": (1e300, 0)}},
])
def test_invalid_graph_aborts_outer_transaction(patch):
    engine = MindMapEngine()
    before = engine.document
    graph = NodeGraph((GraphNode("a", "甲"),))
    with pytest.raises(MindMapError):
        with engine.transaction("批量添加"):
            engine.add_child(engine.root_id, "应回滚")
            try:
                engine.set_node_graph(engine.root_id, replace(graph, **patch))
            except MindMapError:
                pass
    assert engine.document == before and not engine.can_undo


@pytest.mark.parametrize(("field", "value", "message"), [
    ("edges", [["a", "b"]], "连线"),
    ("hidden_node_ids", ["missing"], "筛选"),
])
def test_native_reader_validates_graph_even_when_it_is_not_open(field, value, message):
    engine = MindMapEngine()
    engine.set_node_graph(engine.root_id, NodeGraph())
    payload = to_dict(engine.document)
    payload["nodes"][0]["extension_pages"]["graph"]["data"][field] = value
    with pytest.raises(MindMapError, match=message):
        from_dict(payload)


def test_graph_node_pages_share_titles_and_survive_transactions_and_native_roundtrip(tmp_path):
    """三个入口共存，标题双向同步，级联删除和撤销保留稳定页面标识。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "软件架构")
    edge = GraphEdge("a", "b")
    graph = NodeGraph((GraphNode("a", "数据库"), GraphNode("b", "数据库")), (edge,), {"a": (30, 45)})
    engine.set_node_graph(owner, graph)
    before = engine.document
    with pytest.raises(MindMapError, match="类型"):
        engine.create_graph_node_page(owner, "a", kind="unknown")
    assert engine.document == before and engine.graph_node_document_id(owner, "a") is None
    detail = engine.create_graph_node_page(owner, "a", kind="detail")
    submap = engine.create_graph_node_page(owner, "a", kind="submap")
    nested = engine.create_graph_node_page(owner, "a", kind="graph")
    anchor = engine.graph_node_document_id(owner, "a")
    assert nested == graph_page_id(anchor)
    assert engine.graph_node_location(anchor) == (owner, "a")
    assert engine.graph_node_document_id(owner, "b") is None
    engine.set_node_graph(anchor, NodeGraph((GraphNode("a", "索引"),)))
    inner_detail = engine.create_graph_node_page(anchor, "a", kind="detail")
    root = engine.page_document(submap).root_id
    assert engine.resolve_detail_page(root) == (anchor, detail)
    assert [item.page_id for item in engine.page_summary(inner_detail).path if item.page_root] == [
        None, graph_page_id(owner), nested, inner_detail,
    ]
    engine.update_node(root, text="数据库设计")
    assert engine.page_document(detail).root.text == engine.node_graph(owner).nodes[0].title == "数据库设计"
    engine.set_node_graph(owner, graph.with_content((GraphNode("a", "查询"), graph.nodes[1]), graph.edges))
    assert engine.node_graph(owner).positions == graph.positions
    assert engine.page_document(submap).root.text == engine.page_document(detail).root.text == "查询"
    assert engine.page_document(nested).title == "查询 · 图"

    path = tmp_path / "附页.mindmap"
    save_document(engine.document, path)
    reopened = MindMapEngine(load_document(path))
    assert reopened.document == engine.document
    payload = to_dict(reopened.document)
    assert payload["schema_version"] == 8
    record = next(node["extension_pages"]["graph"] for node in payload["nodes"] if node["id"] == owner)
    assert record["version"] == 2 and record["data"]["nodes"][0]["document_node_id"] == anchor
    payload["schema_version"] = 7
    with pytest.raises(MindMapError, match="版本 8"):
        from_dict(payload)
    payload["schema_version"] = 8
    record["data"]["nodes"][0]["document_node_id"] = engine.root_id
    with pytest.raises(MindMapError, match="附页实体"):
        from_dict(payload)

    before = engine.document
    engine.remove_detail_page(root)
    assert engine.page_summary(detail) is None and engine.page_summary(nested) is not None
    assert engine.get_node(anchor).submap_id == submap
    assert engine.undo() and engine.document == before
    engine.set_node_graph(owner, NodeGraph((GraphNode("b", "数据库"),)))
    assert engine.page_summary(inner_detail) is None and engine.graph_node_location(anchor) is None
    assert engine.undo() and engine.document == before
    assert engine.redo() and not engine.document.submaps
    assert engine.undo() and engine.document == before
    engine.remove_nodes([owner])
    assert set(engine.document.nodes) == {engine.root_id}
    assert engine.undo() and engine.document == before


def test_graph_clipboard_remaps_internal_references_and_retains_images():
    """跨引擎粘贴复制内部页面与引用，普通树的复制也携带图节点附页。"""
    source = MindMapEngine()
    owner = source.add_child(source.root_id, "入口")
    source.set_node_graph(owner, NodeGraph((GraphNode("a", "甲"), GraphNode("b", "乙")), (GraphEdge("a", "b"),)))
    detail = source.create_graph_node_page(owner, "a", kind="detail")
    submap = source.create_graph_node_page(owner, "b", kind="submap")
    anchor_a = source.graph_node_document_id(owner, "a")
    anchor_b = source.graph_node_document_id(owner, "b")
    submap_root = source.page_document(submap).root_id
    detail_root = source.page_document(detail).root_id
    reference = source.add_child(detail_root, "引用")
    source.set_page_reference(reference, submap_root)
    asset = source.attach_image(reference, b"image")
    graph_fragment = from_dict(to_dict(source.copy_graph_fragment(owner, ["a", "b"])))
    tree_fragment = from_dict(to_dict(source.copy_fragment([owner])))

    target = MindMapEngine()
    target.set_node_graph(target.root_id, NodeGraph())
    before = target.document
    revision = target.revision
    target.before_operation = lambda name, _: name != "paste_graph_fragment"
    with pytest.raises(OperationRejected):
        target.paste_graph_fragment(target.root_id, graph_fragment)
    assert target.document == before and target.revision == revision
    target.before_operation = None
    first, second = target.paste_graph_fragment(target.root_id, graph_fragment)
    assert target.revision == revision + 1
    assert target.node_graph(target.root_id).edges == (GraphEdge(first, second),)
    copy_a = target.graph_node_document_id(target.root_id, first)
    copy_b = target.graph_node_document_id(target.root_id, second)
    assert {copy_a, copy_b}.isdisjoint({anchor_a, anchor_b})
    copied_detail = target.get_node(copy_a).detail_page_id
    copied_submap = target.get_node(copy_b).submap_id
    detail_view = target.page_document(copied_detail)
    copied_ref = detail_view.nodes[detail_view.root.children[0]]
    assert copied_ref.image_id == asset and detail_view.assets[asset].data == b"image"
    assert copied_ref.page_reference_root_id == target.page_document(copied_submap).root_id != submap_root
    assert target.undo() and target.document == before
    assert target.redo() and target.graph_node_location(copy_a) == (target.root_id, first)
    copied_tree, = target.paste_fragment(target.root_id, tree_fragment)
    copied_anchor = target.graph_node_document_id(copied_tree, "a")
    assert copied_anchor not in (copy_a, anchor_a)
    assert target.get_node(copied_anchor).detail_page_id not in (detail, copied_detail)
    target.document.validate()
