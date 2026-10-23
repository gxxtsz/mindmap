"""纯图片节点的原子创建、结构约束和历史资源行为。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError, Node


def test_image_child_creation_is_atomic_and_deduplicated():
    engine = MindMapEngine()
    engine.set_expanded(engine.root_id, False)
    engine.replace_document(engine.document)
    before = engine.document
    key = engine.add_image_child(engine.root_id, b"source-image", width=1920)
    node = engine.get_node(key)
    assert node.is_image_node and node.text == "" and node.image_width == 1920
    assert engine.get_node(engine.root_id).children == [key]
    assert engine.get_node(engine.root_id).expanded
    after = engine.document
    assert engine.undo() and engine.document == before and not engine.can_undo
    assert engine.redo() and engine.document == after
    another = engine.add_image_child(engine.root_id, b"source-image", width=100)
    assert engine.get_node(another).image_id == node.image_id and len(engine.document.assets) == 1
    engine.remove_nodes([key])
    assert node.image_id in engine.document.assets
    engine.remove_nodes([another])
    assert not engine.document.assets
    assert engine.undo() and node.image_id in engine.document.assets


@pytest.mark.parametrize("width", [0, -1, True, float("nan"), float("inf"), "100"])
def test_invalid_width_rolls_back_new_node_and_resource(width):
    engine = MindMapEngine()
    before, revision = engine.document, engine.revision
    with pytest.raises(MindMapError):
        engine.add_image_child(engine.root_id, b"image", width=width)
    assert engine.document == before and engine.revision == revision and not engine.can_undo


def test_image_nodes_allow_children_but_reject_pages_and_text():
    engine = MindMapEngine()
    key = engine.add_image_child(engine.root_id, b"image", width=640)
    child = engine.add_child(key, "普通后代")
    assert engine.parent_id(child) == key
    before, revision = engine.document, engine.revision
    for operation in (engine.create_submap, engine.create_detail_page):
        with pytest.raises(MindMapError, match="纯图片"):
            operation(key)
    for patch in ({"text": "正文"}, {"image_id": None}):
        with pytest.raises(MindMapError, match="纯图片"):
            engine.update_node(key, **patch)
    assert engine.document == before and engine.revision == revision
    copied, = engine.copy_nodes([key], engine.root_id)
    assert engine.get_node(copied).is_image_node and engine.get_node(copied).children
    assert len(engine.document.assets) == 1


def test_detail_owner_accepts_image_but_submap_owner_rejects_before_creating():
    engine = MindMapEngine()
    detail_owner = engine.add_child(engine.root_id, "详情入口")
    engine.create_detail_page(detail_owner)
    key = engine.add_image_child(detail_owner, b"image", width=640)
    assert engine.parent_id(key) == detail_owner
    submap_owner = engine.add_child(engine.root_id, "子导图入口")
    engine.create_submap(submap_owner)
    before, revision = engine.document, engine.revision
    with pytest.raises(MindMapError, match="子导图"):
        engine.add_image_child(submap_owner, b"another-image", width=640)
    assert engine.document == before and engine.revision == revision


def test_old_image_attachment_keeps_text_and_page_capabilities():
    engine = MindMapEngine()
    key = engine.add_child(engine.root_id, "旧图文节点")
    engine.attach_image(key, b"old-image")
    assert not engine.get_node(key).is_image_node
    engine.create_detail_page(key)
    engine.create_submap(key)
    engine.document.validate()
    assert not Node(metadata={"image_only": False}).is_image_node


def test_imported_image_page_reference_cannot_bypass_validation():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "旧节点")
    engine.attach_image(owner, b"image")
    engine.create_detail_page(owner)
    candidate = engine.document
    candidate.nodes[owner].metadata["image_only"] = True
    candidate.nodes[owner].text = ""
    with pytest.raises(MindMapError, match="纯图片"):
        candidate.validate()
