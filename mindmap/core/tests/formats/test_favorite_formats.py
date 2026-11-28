"""页面及节点收藏随完整文件持久化，旧文件保持原有兼容性。"""

import pytest
from mindmap_core import GraphNode, MindMapEngine, MindMapError, NodeGraph
from mindmap_core.formats import (
    from_dict,
    load_directory,
    load_document,
    save_directory,
    save_document,
    to_dict,
)
from mindmap_core.node_pages import GRAPH_PAGE_TYPE, graph_page_id


def test_native_and_json_roundtrip_preserve_favorites_order_and_metadata(tmp_path):
    engine = MindMapEngine()
    first = engine.create_submap(engine.add_child(engine.root_id, "第一张"))
    second = engine.create_submap(engine.add_child(engine.root_id, "第二张"))
    engine.set_submap_favorite(first, True)
    engine.set_submap_favorite(second, True)
    document = engine.document
    document.metadata["自定义数据"] = {"说明": "保留其他元数据"}
    encoded = to_dict(document)
    assert encoded["schema_version"] == 5
    assert encoded["metadata"]["submap_favorites"] == [second, first]
    assert from_dict(encoded) == document
    path = tmp_path / "收藏.mindmap"
    save_document(document, path)
    loaded = MindMapEngine(load_document(path))
    assert loaded.document == document and not loaded.is_dirty
    assert [entry.page_id for entry in loaded.favorite_submaps()] == [second, first]


@pytest.mark.parametrize("version", [2, 3, 4])
def test_old_file_without_favorites_opens_cleanly(version):
    document = MindMapEngine().document
    data = to_dict(document)
    data["schema_version"] = version
    del data["inspiration"]
    if version < 4:
        data.pop("detail_pages")
        for node in data["nodes"]:
            node.pop("detail_page_id")
    if version < 3:
        data.pop("submaps")
        for node in data["nodes"]:
            node.pop("submap_id")
    loaded = MindMapEngine(from_dict(data))
    assert loaded.favorite_submaps() == () and not loaded.is_dirty
    assert "submap_favorites" not in loaded.document.metadata
    assert not loaded.document.root.favorite


@pytest.mark.parametrize("tree_favorite,graph_favorite", [(True, False), (False, True), (True, True)])
def test_node_favorites_set_minimum_version_and_roundtrip(tmp_path, tree_favorite, graph_favorite):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "节点图入口")
    engine.set_node_graph(owner, NodeGraph((GraphNode("a", "甲"), GraphNode("b", "乙")), stack_order=("b", "a")))
    baseline = to_dict(engine.document)
    assert baseline["schema_version"] == 15
    assert all("favorite" not in node for node in baseline["nodes"])
    assert not MindMapEngine(from_dict(baseline)).node_graph(owner).nodes[0].favorite
    engine.set_node_favorite(engine.root_id, tree_favorite)
    engine.set_node_favorite("a", graph_favorite, graph_owner_id=owner)
    document = engine.document
    encoded = to_dict(document)
    assert encoded["schema_version"] == 16
    records = {node["id"]: node for node in encoded["nodes"]}
    assert records[engine.root_id].get("favorite", False) == tree_favorite
    graph_record = records[owner]["extension_pages"][GRAPH_PAGE_TYPE]
    assert graph_record["version"] == (10 if graph_favorite else 9)
    assert graph_record["data"]["nodes"][0].get("favorite", False) == graph_favorite
    assert "favorite" not in graph_record["data"]["nodes"][1]
    assert from_dict(encoded) == document
    file_path, folder = tmp_path / "节点收藏.mindmap", tmp_path / "目录导图"
    save_document(document, file_path)
    save_directory(document, folder)
    for loaded in (load_document(file_path), load_directory(folder)):
        assert loaded == document
        assert loaded.page_view(graph_page_id(owner)).nodes["a"].favorite == graph_favorite
    encoded["schema_version"] = 15
    with pytest.raises(MindMapError, match="收藏需要文档版本 16"):
        from_dict(encoded)


def test_invalid_serialized_favorite_is_rejected():
    engine = MindMapEngine()
    engine.set_node_favorite(engine.root_id, True)
    data = to_dict(engine.document)
    data["nodes"][0]["favorite"] = "true"
    with pytest.raises(MindMapError, match="收藏状态必须为布尔值"):
        from_dict(data)
    with pytest.raises(MindMapError, match="收藏状态必须为布尔值"):
        engine.set_node_graph(engine.root_id, NodeGraph((GraphNode("x", "无效字段", favorite=1),)))
