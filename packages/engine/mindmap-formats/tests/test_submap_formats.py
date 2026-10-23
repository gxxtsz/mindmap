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
    assert to_dict(load_document(path))["schema_version"] == 4


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
