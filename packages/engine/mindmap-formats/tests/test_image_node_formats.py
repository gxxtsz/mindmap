"""版本四保存纯图片标记，旧图文节点无需迁移。"""

import pytest
from mindmap_core import MindMapEngine
from mindmap_formats import decode_document, encode_document, load_document, save_document


@pytest.mark.parametrize("format_id", ["json", "mindmap"])
def test_image_nodes_preserve_marker_size_and_old_attachments(tmp_path, format_id):
    engine = MindMapEngine()
    pure = engine.add_image_child(engine.root_id, b"image", width=2300.5)
    old = engine.add_child(engine.root_id, "保留原图文")
    engine.attach_image(old, b"old-image", width=73)
    document = engine.document
    encoded = encode_document(document, format_id=format_id)
    assert decode_document(encoded, format_id=format_id) == document
    target = tmp_path / "纯图片与旧图文.mindmap"
    save_document(document, target)
    reopened = load_document(target)
    assert reopened == document
    assert reopened.nodes[pure].is_image_node and reopened.nodes[pure].image_width == 2300.5
    assert not reopened.nodes[old].is_image_node and reopened.nodes[old].text == "保留原图文"
