"""节点扩展详情的所有权、事务、复制、格式版本和失效导航回归。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_core.formats import from_dict, load_document, save_document, to_dict
from mindmap_core.node_pages import extension_page_id


def test_extension_pages_share_history_and_do_not_borrow_input():
    engine = MindMapEngine()
    data = {"modules": [{"id": "核心"}]}
    engine.set_extension_page(engine.root_id, "software-structure", data)
    data["modules"].clear()
    assert engine.extension_page(engine.root_id, "software-structure")["data"]["modules"] == [{"id": "核心"}]
    snapshot = engine.extension_page(engine.root_id, "software-structure")
    snapshot["data"].clear()
    engine.undo()
    assert engine.extension_page(engine.root_id, "software-structure") is None
    engine.redo()
    assert engine.extension_page(engine.root_id, "software-structure")["data"]["modules"]
    revision = engine.revision
    engine.set_extension_page(engine.root_id, "software-structure", {"modules": [{"id": "核心"}]})
    assert engine.revision == revision


@pytest.mark.parametrize("type_id,data,version", [
    ("../非法", {}, 1), ("valid", [], 1), ("valid", {}, True), ("valid", {}, 0),
    ("valid", {"数值": float("nan")}, 1), ("valid", {1: "非字符串键"}, 1),
    ("valid", {"数组": (1, 2)}, 1),
])
def test_invalid_extension_updates_leave_no_history(type_id, data, version):
    engine = MindMapEngine()
    before = engine.document
    with pytest.raises(MindMapError):
        engine.set_extension_page(engine.root_id, type_id, data, version=version)
    assert engine.document == before and not engine.is_dirty


def test_delete_and_copy_keep_extension_ownership():
    engine = MindMapEngine()
    node = engine.add_child(engine.root_id, "入口")
    engine.set_extension_page(node, "unknown-extension", {"保留": [1, 2]}, version=42)
    fragment = engine.copy_fragment([node])
    other = MindMapEngine()
    copies = other.paste_fragment(other.root_id, fragment)
    assert other.extension_page(copies[0], "unknown-extension") == engine.extension_page(node, "unknown-extension")
    other.remove_nodes(copies)
    other.undo()
    assert other.extension_page(copies[0], "unknown-extension")["version"] == 42
    key = extension_page_id(node, "unknown-extension")
    assert engine.page_summary(key).kind == "extension"
    engine.remove_extension_page(node, "unknown-extension")
    assert engine.page_summary(key) is None
    engine.undo()
    assert engine.page_summary(key) is not None


def test_native_file_preserves_unknown_extensions_and_previous_versions(tmp_path):
    engine = MindMapEngine()
    assert to_dict(engine.document)["schema_version"] == 5
    engine.set_extension_page(engine.root_id, "future", {"自定义": {"列表": [True, None, "中文"]}}, version=99)
    path = tmp_path / "完整文档.mindmap"
    save_document(engine.document, path)
    assert load_document(path) == engine.document
    payload = to_dict(engine.document)
    assert payload["schema_version"] == 7
    old = deepcopy(payload)
    old["schema_version"] = 6
    with pytest.raises(MindMapError, match="版本 7"):
        from_dict(old)
    assert from_dict(payload) == engine.document


def test_extension_coexists_with_detail_and_survives_submap_conversion():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "入口")
    detail = engine.create_detail_page(owner)
    engine.set_extension_page(owner, "software-structure", {})
    engine.set_extension_page(engine.page_document(detail).root_id, "other", {})
    submap = engine.create_submap(owner)
    root = engine.page_document(submap).root_id
    engine.set_extension_page(root, "future", {"不能丢失": True})
    engine.convert_submap_to_descendants(owner)
    assert engine.extension_page(root, "future")["data"] == {"不能丢失": True}
    engine.document.validate()


def test_caught_extension_failure_aborts_outer_transaction():
    engine = MindMapEngine()
    before = engine.document
    with pytest.raises(MindMapError):
        with engine.transaction("批量添加"):
            engine.add_child(engine.root_id, "不应留下的节点")
            try:
                engine.set_extension_page(engine.root_id, "invalid", {"数字": float("inf")})
            except MindMapError:
                pass
    assert engine.document == before and not engine.is_dirty
