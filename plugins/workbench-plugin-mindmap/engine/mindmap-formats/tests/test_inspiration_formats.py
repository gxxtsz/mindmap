"""灵感区新格式、旧格式兼容及损坏输入拒绝。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_formats import load_document, save_document
from mindmap_formats.schema import from_dict, to_dict


@pytest.mark.parametrize("suffix", [".mindmap", ".json"])
def test_forest_roundtrip_preserves_all_entities(tmp_path, suffix):
    engine = MindMapEngine()
    a = engine.add_inspiration("游离主题", position=[-320, 50])
    b = engine.add_inspiration("另一棵树", position=(650, -90))
    child = engine.add_child(a, "图片附件")
    engine.attach_image(child, b"image")
    detail = engine.create_detail_page(a)
    engine.add_child(engine.document.detail_pages[detail].root_id, "详情")
    submap = engine.create_submap(b)
    engine.set_submap_favorite(submap, True)
    path = tmp_path / ("灵感区" + suffix)
    original = engine.document
    save_document(original, path)
    loaded = load_document(path)
    assert loaded == original
    assert to_dict(loaded)["schema_version"] == 5
    assert loaded.inspiration.positions[a] == (-320, 50)
    assert loaded.nodes.keys() == original.nodes.keys()


@pytest.mark.parametrize("version", [2, 3, 4])
def test_legacy_file_has_empty_area_and_is_not_modified(version):
    engine = MindMapEngine()
    key = engine.add_child(engine.root_id, "旧节点")
    data = to_dict(engine.document)
    data["schema_version"] = version
    data.pop("inspiration")
    if version < 4:
        data.pop("detail_pages")
    if version < 3:
        data.pop("submaps")
    original = deepcopy(data)
    loaded = MindMapEngine(from_dict(data))
    assert loaded.get_node(key).text == "旧节点"
    assert not loaded.document.inspiration.root_ids
    assert not loaded.is_dirty and not loaded.can_undo
    assert data == original


@pytest.mark.parametrize("damage", ["duplicate", "missing", "parent", "coordinate", "page_id"])
def test_invalid_forest_is_rejected(damage):
    engine = MindMapEngine()
    a = engine.add_inspiration(position=(0, 0))
    data = to_dict(engine.document)
    area = data["inspiration"]
    if damage == "duplicate":
        area["root_ids"].append(a)
    elif damage == "missing":
        area["positions"].clear()
    elif damage == "parent":
        data["nodes"][0]["children"].append(a)
    elif damage == "coordinate":
        area["positions"][a] = [True, 0]
    else:
        page = engine.create_detail_page(a)
        data = to_dict(engine.document)
        data["inspiration"]["id"] = page
    with pytest.raises(MindMapError):
        from_dict(data)
