"""树布局间距的兼容、数值边界、真实几何及缓存失效回归。"""

from copy import deepcopy

import pytest
from mindmap_core import LayoutConfig, MindMapEngine, MindMapError
from mindmap_layouts import TreeLayout, resolve_tree_spacing


@pytest.mark.parametrize("compact,expected", [(False, (64, 22)), (True, (32, 10))])
@pytest.mark.parametrize("direction", ["both", "right", "left", "down"])
def test_legacy_spacing_keeps_geometry(compact, expected, direction):
    engine = MindMapEngine()
    for index in range(4):
        child = engine.add_child(engine.root_id, f"主题{index}")
        engine.add_child(child, "后代")
    document = engine.document
    document.layout = LayoutConfig(direction=direction, compact=compact)
    before = deepcopy(document)
    assert resolve_tree_spacing(document.layout) == expected
    legacy = TreeLayout().layout(document)
    assert document == before
    document.layout.options = dict(zip(("level_gap", "sibling_gap"), expected))
    assert TreeLayout().layout(document) == legacy


@pytest.mark.parametrize("key,minimum", [("level_gap", 24), ("sibling_gap", 4)])
@pytest.mark.parametrize("value", [None, True, False, "30", [], {}, float("inf"), float("nan"), -1, 0, 501])
def test_bad_spacing_is_rejected(key, minimum, value):
    with pytest.raises(MindMapError):
        resolve_tree_spacing(LayoutConfig(options={key: value}))
    with pytest.raises(MindMapError):
        resolve_tree_spacing(LayoutConfig(options={key: minimum - 0.1}))


def test_unknown_options_and_algorithm_are_rejected_and_partial_options_preserve_defaults():
    with pytest.raises(MindMapError, match="额外布局选项"):
        TreeLayout().validate_config(LayoutConfig(options={"拼写错误": 30}))
    with pytest.raises(MindMapError, match="算法标识"):
        resolve_tree_spacing(LayoutConfig(algorithm_id="external"))
    assert resolve_tree_spacing(LayoutConfig(compact=True, options={"level_gap": 42.5})) == (42.5, 10)
    assert resolve_tree_spacing(LayoutConfig(options={"sibling_gap": 12.5})) == (64, 12.5)


@pytest.mark.parametrize("direction", ["right", "left", "down"])
def test_custom_spacing_controls_node_edges_and_invalidates_cache(direction):
    engine = MindMapEngine()
    first = engine.add_child(engine.root_id, "第一个主题")
    second = engine.add_child(engine.root_id, "第二个主题")
    document = engine.document
    document.layout = LayoutConfig(direction=direction, options={"level_gap": 61.5, "sibling_gap": 19.5})
    sizes = {key: (100, 40) for key in document.nodes}
    layout = TreeLayout()
    result = layout.layout(document, sizes)
    root, a, b = (result.nodes[key] for key in (document.root_id, first, second))
    level = a.y - root.bottom if direction == "down" else root.x - a.right if direction == "left" else a.x - root.right
    sibling = b.x - a.right if direction == "down" else b.y - a.bottom
    assert level == 61.5 and sibling == 19.5
    document.layout.options = {"level_gap": 120, "sibling_gap": 48}
    changed = layout.layout(document, sizes)
    assert changed == TreeLayout().layout(document, sizes)
    assert changed.nodes != result.nodes and changed.bounds != result.bounds
