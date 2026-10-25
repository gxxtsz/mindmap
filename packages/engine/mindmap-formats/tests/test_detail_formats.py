"""详情页文件兼容、完整往返与非法页面结构验收。"""

from copy import deepcopy
from dataclasses import asdict

import pytest
from mindmap_core import MindMapEngine, MindMapError, MindMapPage, Node
from mindmap_formats import can_process_snapshot, from_dict, load_document, save_document, to_dict


@pytest.mark.parametrize("version", [2, 3])
def test_old_versions_keep_ids_and_upgrade_only_when_saved(tmp_path, version):
    engine = MindMapEngine()
    key = engine.add_child(engine.root_id, "原有入口")
    if version == 3:
        engine.create_submap(key)
    data = to_dict(engine.document)
    data["schema_version"] = version
    del data["inspiration"]
    del data["detail_pages"]
    if version == 2:
        del data["submaps"]
    for node in data["nodes"]:
        del node["detail_page_id"]
        if version == 2:
            del node["submap_id"]
    original = deepcopy(data)
    loaded = from_dict(data)
    assert loaded == engine.document and data == original
    path = tmp_path / "升级保存.mindmap"
    save_document(loaded, path)
    assert to_dict(load_document(path))["schema_version"] == 5


def test_both_page_types_roundtrip_with_images_and_process_snapshot(tmp_path):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "双入口")
    detail = engine.create_detail_page(owner)
    submap = engine.create_submap(owner)
    root = engine.page_document(submap).root_id
    child = engine.add_child(root, "子图内部主题")
    engine.create_detail_page(child)
    assert engine.resolve_detail_page(root) == (owner, detail)
    engine.attach_image(engine.page_document(detail).root_id, b"image")
    document = engine.document
    assert from_dict(to_dict(document)) == document
    path = tmp_path / "含详情的完整文档.mindmap"
    save_document(document, path)
    assert load_document(path) == document
    assert can_process_snapshot(engine.capture_save_snapshot().document)


@pytest.mark.parametrize("conflict", [False, True])
def test_legacy_root_details_roundtrip_without_migration(tmp_path, conflict):
    """旧版根独立详情原样读写，多详情保留归属，单详情从外层入口也可访问。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "Rust")
    submap = engine.create_submap(owner)
    root = engine.page_document(submap).root_id
    outer_detail = engine.create_detail_page(owner) if conflict else None
    document = engine.document
    legacy_root = Node("Rust")
    legacy = MindMapPage("旧版独立详情", legacy_root.id, root)
    document.nodes[legacy_root.id] = legacy_root
    document.nodes[root].detail_page_id = legacy.id
    document.detail_pages[legacy.id] = legacy
    path = tmp_path / "旧版详情.mindmap"
    save_document(document, path)
    original = path.read_bytes()
    loaded = load_document(path)
    restored = MindMapEngine(loaded)
    assert loaded == document and path.read_bytes() == original
    assert to_dict(loaded)["schema_version"] == 5
    assert restored.resolve_detail_page(root) == (root, legacy.id)
    assert restored.resolve_detail_page(owner) == ((owner, outer_detail) if conflict else (root, legacy.id))
    save_document(loaded, path)
    assert load_document(path) == document


@pytest.mark.parametrize("corruption", ["missing", "owner", "name", "orphan", "shared_id", "nested_detail", "nested_submap"])
def test_invalid_detail_ownership_and_nested_pages_are_rejected(corruption):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "入口")
    engine.create_detail_page(owner)
    data = to_dict(engine.document)
    page = data["detail_pages"][0]
    nodes = {node["id"]: node for node in data["nodes"]}
    if corruption == "missing":
        data["detail_pages"].clear()
    elif corruption == "owner":
        page["owner_id"] = engine.root_id
    elif corruption == "name":
        nodes[page["root_id"]]["text"] = "不同名称"
    elif corruption == "orphan":
        extra = deepcopy(page)
        extra["id"] = "没有入口"
        data["detail_pages"].append(extra)
    elif corruption == "shared_id":
        data["submaps"].append(deepcopy(page))
    else:
        root = Node("入口")
        record = deepcopy(page)
        record.update(id="非法嵌套", root_id=root.id, owner_id=page["root_id"])
        data["nodes"].append(asdict(root))
        field = "detail_page_id" if corruption == "nested_detail" else "submap_id"
        nodes[page["root_id"]][field] = record["id"]
        data["detail_pages" if corruption == "nested_detail" else "submaps"].append(record)
    with pytest.raises(MindMapError):
        from_dict(data)
