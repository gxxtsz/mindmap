"""收藏随完整文件持久化，旧文件及单页剪贴板保持原有兼容性。"""

import pytest
from mindmap_core import MindMapEngine
from mindmap_formats import from_dict, load_document, save_document, to_dict


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
