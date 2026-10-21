"""以现有全量布局为参照，检验增量几何与视口裁剪的正确性及工作量。"""

import random
from copy import deepcopy

import pytest
from mindmap_core import MindMapDocument, MindMapError, Node, Rect
from mindmap_layouts.incremental import IncrementalTreeGeometry
from mindmap_layouts.tree import TreeLayout


def make_tree(count=121, width=5):
    document = MindMapDocument.new()
    keys = [document.root_id]
    for index in range(1, count):
        parent = document.nodes[keys[(index - 1) // width]]
        node = Node(str(index), direction="left" if index == 2 else None)
        document.nodes[node.id] = node
        parent.children.append(node.id)
        keys.append(node.id)
    sizes = {key: (80.0 + index % 7 * 15, 28.0 + index % 3 * 9) for index, key in enumerate(keys)}
    return document, keys, sizes


def assert_rectangles(actual, expected):
    assert actual.keys() == expected.keys()
    for key, rect in expected.items():
        got = actual[key]
        assert (got.x, got.y, got.width, got.height) == pytest.approx(
            (rect.x, rect.y, rect.width, rect.height), abs=1e-7
        )


@pytest.mark.parametrize("direction", ["both", "right", "left", "down"])
@pytest.mark.parametrize("compact", [False, True])
def test_local_commands_match_full_layout_and_viewport(direction, compact):
    document, keys, sizes = make_tree()
    document.layout.direction = direction
    document.layout.compact = compact
    geometry = IncrementalTreeGeometry(document, sizes)
    randomizer = random.Random(703)
    layout = TreeLayout()
    for turn in range(40):
        key = randomizer.choice(keys)
        if turn % 3 == 0:
            expanded = not document.nodes[key].expanded
            geometry.set_expanded(key, expanded)
            document.nodes[key].expanded = expanded
        elif turn % 3 == 1:
            sizes[key] = randomizer.uniform(50, 240), randomizer.uniform(22, 80)
            geometry.resize(key, sizes[key])
        elif key != document.root_id:
            candidates = set(keys) - set(document.descendants(key))
            parent = randomizer.choice(sorted(candidates))
            old = document.parents()[key]
            document.nodes[old].children.remove(key)
            index = randomizer.randrange(len(document.nodes[parent].children) + 1)
            document.nodes[parent].children.insert(index, key)
            document.nodes[parent].expanded = True
            geometry.move(key, parent, index)
        expected = layout.layout(document, sizes).nodes
        actual = geometry.all_rectangles()
        assert_rectangles(actual, expected)
        selected = randomizer.choice(list(expected))
        assert_rectangles({selected: geometry.rectangle(selected)}, {selected: expected[selected]})
        center = expected[selected].center
        viewport = Rect(center[0] - 300, center[1] - 200, 600, 400)
        assert_rectangles(geometry.query(viewport), {
            key: rect for key, rect in expected.items() if rect.intersects(viewport)
        })


def test_wide_tree_skips_offscreen_siblings_and_updates_only_ancestors():
    document, keys, sizes = make_tree(10001, width=10000)
    parent = keys[-1]
    for index in range(5):
        node = Node(f"叶子{index}")
        document.nodes[node.id] = node
        document.nodes[parent].children.append(node.id)
        sizes[node.id] = 100, 36
    geometry = IncrementalTreeGeometry(document, sizes)
    before = geometry.rectangle(parent)
    geometry.set_expanded(parent, False)
    assert geometry.last_updated_nodes == 2
    viewport = Rect(before.x - 200, before.y - 400, 600, 800)
    actual = geometry.query(viewport)
    assert geometry.last_visited_nodes < 30
    expected = geometry.all_rectangles()
    assert_rectangles(actual, {key: rect for key, rect in expected.items() if rect.intersects(viewport)})
    geometry.set_expanded(parent, False)
    assert geometry.last_updated_nodes == 0


def test_prototype_owns_structure_and_rejects_invalid_commands_without_partial_change():
    document, keys, sizes = make_tree()
    before = deepcopy(document)
    geometry = IncrementalTreeGeometry(document, sizes)
    geometry.set_expanded(keys[1], False)
    with pytest.raises(MindMapError, match="不可见"):
        geometry.rectangle(document.nodes[keys[1]].children[0])
    rectangles = geometry.all_rectangles()
    for action in (
        lambda: geometry.move(keys[1], document.nodes[keys[1]].children[0]),
        lambda: geometry.move(keys[1], keys[2], -1),
        lambda: geometry.set_expanded(keys[1], "是"),
        lambda: geometry.resize(keys[1], (0, 20)),
    ):
        with pytest.raises(MindMapError):
            action()
        assert geometry.all_rectangles() == rectangles
    geometry.move(keys[-1], keys[2])
    geometry.resize(keys[2], (150, 70))
    assert document == before


def test_deep_tree_uses_iterative_queries():
    document, keys, sizes = make_tree(1500, width=1)
    geometry = IncrementalTreeGeometry(document, sizes)
    assert geometry.rectangle(keys[-1]) == geometry.all_rectangles()[keys[-1]]
    geometry.set_expanded(keys[-2], False)
    assert len(geometry.all_rectangles()) == 1499
