"""内置树布局的方向、折叠、聚焦和几何不变量。"""

import pytest
from mindmap_core import LayoutConfig, MindMapDocument, MindMapEngine, MindMapError
from mindmap_layouts import create_default_layout_registry


def example():
    engine = MindMapEngine()
    with engine.transaction():
        engine.set_layout(LayoutConfig(direction="right"))
        a = engine.add_child(engine.root_id, "中文\n多行")
        b = engine.add_child(engine.root_id, "另一分支")
        engine.add_child(a, "内容")
        engine.attach_image(a, "测试图片字节".encode())
        engine.update_node(b, metadata={"用户字段": [1, "内容"]}, tags=["标签"])
    return engine.document


@pytest.mark.parametrize("direction", ["left", "right", "both", "down"])
def test_layout_nonoverlap_deterministic(direction):
    engine = MindMapEngine()
    with engine.transaction():
        engine.set_layout(LayoutConfig(direction=direction))
        for i in range(8):
            parent = engine.add_child(engine.root_id, f"分支 {i}")
            for j in range(6):
                engine.add_child(parent, f"条目 {j}")
    document = engine.document
    measurements = {key: (60 + i % 5 * 30, 30 + i % 4 * 15) for i, key in enumerate(document.nodes)}
    first = create_default_layout_registry().layout(document, measurements)
    second = create_default_layout_registry().layout(document, measurements)
    assert first == second
    rectangles = list(first.nodes.values())
    for i, rect in enumerate(rectangles):
        assert all(not rect.intersects(other) for other in rectangles[i + 1 :])
    assert len(first.branches) == len(first.nodes) - 1
    parents = document.parents()
    for node_id, branch in first.branches.items():
        for point, rect in (
            (branch.start, first.nodes[parents[node_id]]),
            (branch.end, first.nodes[node_id]),
        ):
            x, y = point
            assert rect.x <= x <= rect.right and rect.y <= y <= rect.bottom
            assert x in (rect.x, rect.right) or y in (rect.y, rect.bottom)
    assert document == engine.document


def test_fold_focus_and_export_layout_are_nonmutating():
    engine = MindMapEngine(example())
    child = engine.document.root.children[0]
    engine.set_expanded(child, False)
    document = engine.document
    folded = create_default_layout_registry().layout(document)
    expanded = create_default_layout_registry().layout(document, expand_all=True)
    focused = create_default_layout_registry().layout(document, focus_id=child, expand_all=True)
    assert len(expanded.nodes) == len(folded.nodes) + 1
    assert len(focused.nodes) == 2
    hidden = document.nodes[child].children[0]
    assert hidden not in folded.branches and hidden in expanded.branches
    assert set(focused.branches) == {hidden}
    assert folded.bounds.width <= expanded.bounds.width
    assert document == engine.document


def test_bad_measurements():
    with pytest.raises(MindMapError):
        document = MindMapDocument.new()
        create_default_layout_registry().layout(document, {document.root_id: (-1, 30)})
