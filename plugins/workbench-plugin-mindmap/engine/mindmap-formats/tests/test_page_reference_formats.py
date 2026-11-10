"""验证原生文件、JSON、文本大纲与原生剪贴板的页面引用契约。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_formats import decode_document, encode_document, from_dict, load_document, save_document, to_dict


def linked_document():
    """构造同名子导图、详情页与普通引用节点，便于验证目标身份。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "目标页标题")
    page = engine.create_submap(owner)
    engine.create_detail_page(owner)
    target = engine.page_document(page).root_id
    source = engine.add_child(engine.root_id, "引用节点原文")
    engine.set_page_reference(source, target)
    return engine, source, owner, page, target


@pytest.mark.parametrize("extension", [".mindmap", ".json"])
def test_version_six_roundtrip_only_when_a_reference_is_present(tmp_path, extension):
    engine, source, _, page, target = linked_document()
    path = tmp_path / f"含引用{extension}"
    save_document(engine.document, path)
    loaded = load_document(path)
    assert to_dict(loaded)["schema_version"] == 6 and loaded == engine.document
    reopened = MindMapEngine(loaded)
    assert reopened.get_node(source).text == "引用节点原文"
    assert reopened.resolve_page_reference(source).page_id == page
    assert reopened.resolve_page_reference(source).node_id == target
    outline = encode_document(reopened.page_document(), format_id="text", options={"tab_indent": True})
    assert "\t目标页标题\n" in outline.decode("utf-8") and "引用节点原文" not in outline.decode("utf-8")

    reopened.set_page_reference(source, None)
    save_document(reopened.document, path)
    old_payload = to_dict(load_document(path))
    assert old_payload["schema_version"] == 5
    assert all("page_reference_root_id" not in node for node in old_payload["nodes"])


@pytest.mark.parametrize("invalid", ["旧版本", "普通节点", "本页", "页面标识", "根作为入口", "错误类型", "缺失"])
def test_import_rejects_invalid_reference_without_fixing_input(invalid):
    engine, source, owner, page, target = linked_document()
    payload = to_dict(engine.document)
    nodes = {node["id"]: node for node in payload["nodes"]}
    field = "page_reference_root_id"
    if invalid == "旧版本":
        payload["schema_version"] = 5
    elif invalid == "根作为入口":
        nodes[engine.root_id][field] = target
    else:
        nodes[source][field] = {"普通节点": owner, "本页": engine.root_id, "页面标识": page,
                                "错误类型": [], "缺失": "无效根标识"}[invalid]
    with pytest.raises(MindMapError):
        from_dict(payload)


def test_clipboard_reconnects_outside_targets_only_in_the_same_open_document():
    engine, source, owner, page, target = linked_document()
    # 另存副本可能拥有完全一样的 ID；标识相同仍不能把另一份文件认成本文件。
    another = MindMapEngine(engine.document)
    fragment = decode_document(encode_document(engine.copy_fragment([source])))
    same, = engine.paste_fragment(engine.root_id, fragment)
    foreign, = another.paste_fragment(another.root_id, fragment)
    in_target, = engine.paste_fragment(target, fragment)
    assert engine.resolve_page_reference(same).page_id == page
    for destination, node_id in ((another, foreign), (engine, in_target)):
        assert destination.resolve_page_reference(node_id) is None
        assert destination.get_node(node_id).text == "引用节点原文"
        assert not destination.document.metadata

    engine.remove_submap(owner)
    removed, = engine.paste_fragment(engine.root_id, fragment)
    assert engine.resolve_page_reference(removed) is None
    assert engine.get_node(removed).text == "引用节点原文"


def test_clipboard_remaps_references_to_pages_copied_with_the_fragment():
    engine, source, owner, _, target = linked_document()
    payload = encode_document(engine.copy_fragment([owner, source]))
    for destination in (engine, MindMapEngine()):
        copied_owner, copied_source = destination.paste_fragment(destination.root_id, decode_document(payload))
        copied_page = destination.get_node(copied_owner).submap_id
        copied_root = destination.page_document(copied_page).root_id
        assert copied_root != target
        assert destination.resolve_page_reference(copied_source).node_id == copied_root
        assert destination.get_node(copied_source).text == "引用节点原文"
        destination.document.validate()
    _, source_without_page = engine.paste_fragment(engine.root_id, decode_document(payload), include_pages=False)
    assert engine.resolve_page_reference(source_without_page) is None
