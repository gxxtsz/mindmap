"""覆盖图的事务、存储校验和复制时的所有权。"""

from dataclasses import replace

import pytest
from mindmap_core import GraphEdge, GraphNode, MindMapEngine, MindMapError, NodeGraph
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
