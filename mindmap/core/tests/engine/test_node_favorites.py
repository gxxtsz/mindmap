"""节点收藏的事务、页面归属及原生片段复制语义。"""

import pytest
from mindmap_core import GraphNode, MindMapEngine, MindMapError, NodeGraph
from mindmap_core.node_pages import graph_page_id


def test_tree_favorite_is_idempotent_and_follows_original_node():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "拥有分支")
    leaf = engine.add_child(owner, "原节点")
    engine.mark_saved()
    events = []
    engine.subscribe(events.append)

    engine.set_node_favorite(leaf, True)
    revision = engine.revision
    engine.set_node_favorite(leaf, True)
    assert engine.get_node(leaf).favorite and engine.is_dirty
    assert engine.revision == revision and len(events) == 1
    assert engine.undo() and not engine.get_node(leaf).favorite and not engine.is_dirty
    assert engine.redo() and engine.get_node(leaf).favorite
    engine.update_node(leaf, text="同一节点已经改名")
    assert engine.get_node(leaf).favorite

    page = engine.convert_descendants_to_submap(owner)
    root = engine.page_document(page).root_id
    assert engine.page_document(page).nodes[leaf].favorite
    assert not engine.get_node(root).favorite and not engine.is_submap_favorite(page)
    engine.set_node_favorite(root, True)
    engine.convert_submap_to_descendants(owner)
    assert engine.get_node(owner).children == [root]
    assert engine.get_node(root).favorite and engine.get_node(leaf).favorite

    engine.move_to_inspiration([root], positions={root: (50, 20)})
    assert engine.page_document(engine.inspiration_id).nodes[root].favorite
    engine.remove_nodes([root])
    assert engine.undo() and engine.get_node(leaf).favorite


def test_favorite_validation_preserves_document_and_history():
    engine = MindMapEngine()
    before = engine.document, engine.revision, engine.can_undo
    for key, value, owner, message in (
        (engine.root_id, 1, None, "布尔值"),
        ("", True, None, "节点标识"),
        ("n", True, "", "所属节点"),
        ("n", True, engine.root_id, "节点图不存在"),
    ):
        with pytest.raises(MindMapError, match=message):
            engine.set_node_favorite(key, value, graph_owner_id=owner)
        assert (engine.document, engine.revision, engine.can_undo) == before


def test_graph_favorite_uses_local_id_and_keeps_bound_page_independent():
    engine = MindMapEngine()
    first = engine.add_child(engine.root_id, "第一张图")
    second = engine.add_child(engine.root_id, "第二张图")
    for owner in (first, second):
        engine.set_node_graph(owner, NodeGraph((GraphNode("x", "同一局部标识"),)))
    submap = engine.create_graph_node_page(first, "x", kind="submap")
    entity = engine.graph_node_document_id(first, "x")
    root = engine.page_document(submap).root_id
    engine.set_submap_favorite(submap, True)
    changes = []
    engine.subscribe(changes.append)
    engine.set_node_favorite("x", True, graph_owner_id=first)
    assert changes[-1].changes.page_id == graph_page_id(first)
    assert engine.page_document(graph_page_id(first)).nodes["x"].favorite
    assert not engine.page_document(graph_page_id(second)).nodes["x"].favorite
    assert not engine.get_node(root).favorite and engine.is_submap_favorite(submap)
    with pytest.raises(MindMapError, match="局部标识"):
        engine.set_node_favorite(entity, True)
    with pytest.raises(MindMapError, match="图节点不存在"):
        engine.set_node_favorite("missing", True, graph_owner_id=first)
    assert engine.undo() and not engine.node_graph(first).nodes[0].favorite
    assert engine.redo() and engine.node_graph(first).nodes[0].favorite


def test_paste_clears_favorites_in_tree_and_graph_copies_with_attached_pages():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "原分支")
    engine.set_node_graph(owner, NodeGraph((GraphNode("x", "图节点"),)))
    submap = engine.create_graph_node_page(owner, "x", kind="submap")
    subroot = engine.page_document(submap).root_id
    engine.set_node_favorite(owner, True)
    engine.set_node_favorite(subroot, True)
    engine.set_node_favorite("x", True, graph_owner_id=owner)

    [copied] = engine.copy_nodes([owner], engine.root_id)
    graph_copy = engine.node_graph(copied)
    copied_page = engine.get_node(graph_copy.nodes[0].document_node_id).submap_id
    assert not engine.get_node(copied).favorite and not graph_copy.nodes[0].favorite
    assert not engine.page_document(copied_page).root.favorite
    assert graph_copy.to_record()["version"] < 10

    fragment = engine.copy_graph_fragment(owner, ["x"])
    [pasted] = engine.paste_graph_fragment(owner, fragment)
    first, second = engine.node_graph(owner).nodes
    assert first.id == "x" and first.favorite and second.id == pasted and not second.favorite
    pasted_page = engine.get_node(second.document_node_id).submap_id
    assert not engine.page_document(pasted_page).root.favorite
    assert engine.get_node(owner).favorite and engine.get_node(subroot).favorite
    assert engine.undo() and engine.redo()
    assert not engine.page_document(graph_page_id(owner)).nodes[pasted].favorite
