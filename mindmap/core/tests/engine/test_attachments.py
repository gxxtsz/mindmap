"""附件的事务、资源生命周期、原生格式及跨导图复制。"""

import pytest
from mindmap_core import GraphNode, MindMapEngine, MindMapError, NodeGraph
from mindmap_core.formats import (
    decode_document,
    encode_document,
    from_dict,
    load_directory,
    save_directory,
    to_dict,
)


def test_replace_and_remove_attachment_are_separate_undo_steps():
    engine = MindMapEngine()
    key = engine.root_id
    engine.set_node_attachment(key, ("说明.txt", b""))
    original = engine.get_node(key).attachment
    assert engine.document.assets[original.asset_id].data == b""

    engine.set_node_attachment(key, ("说明.txt", b"new"))
    replacement = engine.get_node(key).attachment
    assert set(engine.document.assets) == {replacement.asset_id}
    engine.set_node_attachment(key, None)
    assert engine.get_node(key).attachment is None
    assert not engine.document.assets

    assert engine.undo()
    assert engine.get_node(key).attachment == replacement
    assert engine.undo()
    assert engine.get_node(key).attachment == original
    assert engine.redo()
    assert engine.get_node(key).attachment == replacement
    assert engine.redo()
    assert not engine.document.assets


def test_image_and_tree_and_graph_attachments_share_resource_until_last_reference_is_removed():
    engine = MindMapEngine()
    key = engine.root_id
    engine.set_node_graph(key, NodeGraph((GraphNode("a", "图节点"),)))
    engine.set_node_attachment("a", ("图表.png", b"shared"), graph_owner_id=key)
    engine.set_node_attachment(key, ("备份.png", b"shared"))
    image_id = engine.attach_image(key, b"shared")
    assert set(engine.document.assets) == {image_id}
    assert engine.document.assets[image_id].is_image

    engine.set_node_attachment(key, None)
    engine.update_node(key, image_id=None)
    assert set(engine.document.assets) == {image_id}
    engine.set_node_graph(key, NodeGraph())
    assert not engine.document.assets
    assert engine.undo()
    assert engine.node_graph(key).nodes[0].attachment.asset_id == image_id
    assert engine.document.assets[image_id].data == b"shared"


@pytest.mark.parametrize("format_id", ["directory", "json", "mindmap"])
def test_native_save_preserves_tree_and_graph_when_source_disappears(tmp_path, format_id):
    source = tmp_path / "旅行 计划 #1.pdf"
    data = b"%PDF-1.7\n"
    source.write_bytes(data)
    engine = MindMapEngine()
    key = engine.root_id
    engine.set_node_graph(key, NodeGraph((GraphNode("a", "图节点"),)))
    engine.set_node_attachment(key, (source.name, source.read_bytes()))
    engine.set_node_attachment("a", (source.name, source.read_bytes()), graph_owner_id=key)
    attachment = engine.get_node(key).attachment
    document = engine.document

    if format_id == "directory":
        path = tmp_path / "导图"
        save_directory(document, path)
        source.unlink()
        loaded = load_directory(path)
        assert (path / "assets" / attachment.asset_id).read_bytes() == data
    else:
        payload = encode_document(document, format_id=format_id)
        source.unlink()
        loaded = decode_document(payload, format_id=format_id)
    assert loaded == document
    assert loaded.assets[attachment.asset_id].data == data


def test_copy_fragments_keeps_attachments_with_or_without_graph_node_pages():
    source, destination = MindMapEngine(), MindMapEngine()
    owner = source.add_child(source.root_id, "图的入口")
    source.set_node_graph(owner, NodeGraph((GraphNode("a", "图节点"),)))
    source.create_graph_node_page(owner, "a", kind="submap")
    source.set_node_attachment(owner, ("笔记.txt", b"tree"))
    source.set_node_attachment("a", ("参考.txt", b"graph"), graph_owner_id=owner)
    fragment = source.copy_fragment([owner])
    [pasted] = destination.paste_fragment(destination.root_id, fragment)
    graph_attachment = source.node_graph(owner).nodes[0].attachment
    assert destination.get_node(pasted).attachment == source.get_node(owner).attachment
    assert destination.node_graph(pasted).nodes[0].attachment == graph_attachment

    graph_fragment = source.copy_graph_fragment(owner, ["a"])
    [pasted_local] = destination.paste_graph_fragment(pasted, graph_fragment, include_pages=False)
    node = next(entry for entry in destination.node_graph(pasted).nodes if entry.id == pasted_local)
    assert node.attachment == graph_attachment and node.document_node_id is None
    assert destination.document.assets[graph_attachment.asset_id].data == b"graph"
    assert len(destination.document.assets) == 2


def test_editing_one_graph_preserves_attachment_by_local_id():
    engine = MindMapEngine()
    first = engine.root_id
    second = engine.add_child(first, "另一张图")
    for owner in (first, second):
        engine.set_node_graph(owner, NodeGraph((GraphNode("a", "图节点"),)))
        engine.set_node_attachment("a", ("笔记.txt", owner.encode()), graph_owner_id=owner)
    before = engine.node_graph(first).nodes[0].attachment
    updated = NodeGraph((GraphNode("a", "改过的正文"),))
    engine.set_node_graph(first, updated)
    assert engine.node_graph(first).nodes[0].attachment == before
    assert engine.node_graph(second).nodes[0].attachment != before


def test_legacy_record_and_attachment_validation():
    engine = MindMapEngine()
    key = engine.root_id
    engine.set_node_graph(key, NodeGraph((GraphNode("a", "正文", text_format="markdown"),)))
    old = to_dict(engine.document)
    assert old["schema_version"] == 9 and "attachment" not in old["nodes"][0]
    assert from_dict(old).root.attachment is None

    with pytest.raises(MindMapError, match="有效的文件名"):
        engine.set_node_attachment(key, ("../导图.json", b"invalid"))
    assert not engine.document.assets
    engine.set_node_attachment("a", ("说明.txt", b"hello"), graph_owner_id=key)
    record = to_dict(engine.document)
    assert record["schema_version"] == 10
    assert record["nodes"][0]["extension_pages"]["graph"]["version"] == 4
    record["assets"].clear()
    with pytest.raises(MindMapError, match="附件资源缺失"):
        from_dict(record)
