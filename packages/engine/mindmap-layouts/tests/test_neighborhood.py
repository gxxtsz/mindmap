"""四方向的公开局部邻居查询，不依赖内部坐标变换和索引。"""

import pytest
from mindmap_core import MindMapDocument, Node
from mindmap_layouts import TreeLayout
from mindmap_layouts.incremental import IncrementalTreeGeometry


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
def test_neighbors_preserve_hits_and_skip_only_between_candidates(direction):
    document = MindMapDocument.new()
    document.layout.direction = direction
    children = []
    for index in range(7):
        node = Node(str(index))
        children.append(node.id)
        document.nodes[node.id] = node
    document.root.children = children
    sizes = {key: (100, 40) for key in document.nodes}
    expected = TreeLayout().layout(document, sizes).nodes
    geometry = IncrementalTreeGeometry(document, sizes)
    groups = {}
    for key in children:
        rect = expected[key]
        side = "vertical" if direction == "down" else "left" if rect.center[0] < 0 else "right"
        groups.setdefault(side, []).append(key)
    group = max(groups.values(), key=len)
    group.sort(key=lambda key: expected[key].center[0 if direction == "down" else 1])
    first, last = group[0], group[-1]
    skipped = set(group[1:-1])

    class LazyMembership:
        def __contains__(self, key):
            return key in skipped

    assert list(geometry.adjacent_sibling_pairs([first, last])) == []
    assert list(geometry.adjacent_sibling_pairs([first, last], skipped_ids=LazyMembership())) == [
        (document.root_id, first, last)
    ]
    center = group[len(group) // 2]
    viewport = expected[center].padded(1)
    hits = geometry.query(viewport)
    expanded = geometry.query_with_sibling_neighbors(viewport, skipped_ids=LazyMembership())
    assert hits.keys() <= expanded.keys() and center in expanded
    assert all(rect == expected[key] for key, rect in expanded.items())
