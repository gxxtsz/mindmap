"""单个想法读模型的隔离、坐标及侧栏摘要契约。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError, MindMapForestView


def test_single_projection_keeps_identity_without_mutating_overview():
    engine = MindMapEngine()
    first = engine.add_inspiration("甲", position=(-800, 30))
    child = engine.add_child(first, "子主题")
    detail = engine.create_detail_page(child)
    engine.add_child(engine.document.detail_pages[detail].root_id, "详情正文")
    other = engine.add_inspiration("乙", position=(900, -400))
    engine.mark_saved()
    original, revision = engine.document, engine.revision
    view = engine.page_document(engine.inspiration_id, inspiration_root_id=first)
    assert isinstance(view, MindMapForestView)
    assert view.kind == "inspiration"
    assert view.root_ids == (first,)
    assert set(view.nodes) == {first, child}
    assert view.inspiration.positions == {first: (0, 0)}
    assert view.nodes[child].detail_page_id == detail
    assert engine.inspiration_items() == ((first, "甲", 1), (other, "乙", 0))
    view.nodes[first].text = "隔离修改"
    view.inspiration.positions[first] = (123, 456)
    assert engine.document == original and engine.revision == revision and not engine.is_dirty
    overview = engine.page_document(engine.inspiration_id)
    assert overview.root_ids == (first, other)
    assert overview.inspiration.positions[first] == (-800, 30)
    for page, root in ((None, first), (engine.inspiration_id, child), (engine.inspiration_id, "无效")):
        with pytest.raises(MindMapError):
            engine.page_document(page, inspiration_root_id=root)
    assert engine.document == original
