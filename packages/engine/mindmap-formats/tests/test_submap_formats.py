"""多页原生格式往返、版本兼容和损坏引用验证。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_formats import from_dict, load_document, save_document, to_dict


def test_version_two_load_does_not_change_ids_styles_or_assets(tmp_path):
    engine = MindMapEngine()
    key = engine.add_child(engine.root_id, "旧文件节点")
    engine.attach_image(key, b"image")
    current = engine.document
    legacy = to_dict(current)
    legacy["schema_version"] = 2
    del legacy["inspiration"]
    del legacy["submaps"]
    del legacy["detail_pages"]
    for node in legacy["nodes"]:
        del node["submap_id"]
        del node["detail_page_id"]
    loaded = from_dict(legacy)
    assert loaded == current and not loaded.submaps
    path = tmp_path / "升级保存.mindmap"
    save_document(loaded, path)
    assert load_document(path) == current
    assert to_dict(load_document(path))["schema_version"] == 5


def test_deep_nested_pages_roundtrip_with_images(tmp_path):
    engine = MindMapEngine(history_limit=2)
    key = engine.root_id
    for _ in range(1050):
        page = engine.create_submap(key)
        key = engine.page_document(page).root_id
    engine.attach_image(key, b"image")
    path = tmp_path / "深层嵌套.mindmap"
    save_document(engine.document, path)
    assert load_document(path) == engine.document


def test_page_read_model_cannot_accidentally_replace_the_complete_file():
    engine = MindMapEngine()
    engine.create_submap(engine.root_id)
    with pytest.raises(MindMapError, match="完整文件"):
        to_dict(engine.page_document())


@pytest.mark.parametrize("corruption", ["missing", "owner", "cycle", "leaf", "name", "root", "orphan"])
def test_invalid_page_reference_is_rejected(corruption):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "入口")
    page = engine.create_submap(owner)
    payload = to_dict(engine.document)
    record = payload["submaps"][0]
    nodes = {node["id"]: node for node in payload["nodes"]}
    if corruption == "missing":
        payload["submaps"].clear()
    elif corruption == "owner":
        record["owner_id"] = engine.root_id
    elif corruption == "cycle":
        nodes[record["root_id"]]["submap_id"] = page
    elif corruption == "leaf":
        nodes[owner]["children"] = [record["root_id"]]
    elif corruption == "name":
        nodes[record["root_id"]]["text"] = "失去同步"
    elif corruption == "root":
        record["root_id"] = owner
    else:
        extra = deepcopy(record)
        extra["id"] = "orphan"
        payload["submaps"].append(extra)
    with pytest.raises(MindMapError):
        from_dict(payload)


@pytest.mark.parametrize("keep_root", [False, True])
def test_converted_pages_roundtrip_without_changing_format_version(tmp_path, keep_root):
    """双向转换仍使用版本 5；图片、嵌套收藏及旧页面标识不需要格式迁移。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "章节")
    child = engine.add_child(owner, "原有后代")
    engine.attach_image(child, b"image")
    nested = engine.create_submap(child)
    engine.create_detail_page(child)
    engine.set_submap_favorite(nested, True)
    page = engine.convert_descendants_to_submap(owner)
    engine.set_submap_favorite(page, True)
    path = tmp_path / "双向转换.mindmap"
    save_document(engine.document, path)
    loaded = load_document(path)
    assert loaded == engine.document and to_dict(loaded)["schema_version"] == 5
    engine = MindMapEngine(loaded)
    root = loaded.submaps[page].root_id
    if keep_root:
        engine.update_node(root, note="根的独立内容")
    engine.convert_submap_to_descendants(owner)
    save_document(engine.document, path)
    restored = load_document(path)
    assert restored == engine.document
    assert (root in restored.nodes) == keep_root
    assert restored.nodes[child].submap_id == nested
    assert restored.metadata["submap_favorites"] == [nested]
    assert restored.assets == loaded.assets
    assert to_dict(restored)["schema_version"] == 5
