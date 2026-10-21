"""正式增量服务与完整布局的坐标、连接线、历史及旧版本一致性。"""

import random

import pytest
from mindmap_core import BranchPath, BranchStyle, Curve, MindMapDocument, MindMapEngine, Node
from mindmap_layouts.incremental_result import IncrementalTreeLayout
from mindmap_layouts.persistent import PersistentMap
from mindmap_layouts.tree import TreeLayout


def make_document(count=81):
    document = MindMapDocument.new()
    keys = [document.root_id]
    for index in range(1, count):
        node = Node(str(index))
        document.nodes[node.id] = node
        document.nodes[keys[(index - 1) // 5]].children.append(node.id)
        keys.append(node.id)
    return document, keys


def measure(node):
    return 90 + len(node.text) * 3 + (20 if node.children else 0), 36 + len(node.tags) * 15


def coordinates(branch):
    points = branch.points if isinstance(branch, Curve) else (
        branch.start, branch.end, *(point for command in branch.commands for point in command.points),
        *(branch.arrow or ()),
    )
    return tuple(number for point in points for number in point)


def assert_layout(actual, expected):
    assert list(actual.nodes) == list(expected.nodes)
    for key, rect in expected.nodes.items():
        got = actual.nodes[key]
        assert (got.x, got.y, got.width, got.height) == pytest.approx(
            (rect.x, rect.y, rect.width, rect.height), abs=1e-7
        )
    assert list(actual.branches) == list(expected.branches)
    for key, branch in expected.branches.items():
        got = actual.branches[key]
        assert type(got) is type(branch) and got.color == branch.color
        assert coordinates(got) == pytest.approx(coordinates(branch), abs=1e-7)
        if isinstance(branch, BranchPath):
            assert [command.op for command in got.commands] == [command.op for command in branch.commands]


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("kind", [None, *BranchStyle])
def test_all_directions_and_branch_styles_match_full_layout(direction, kind):
    document, keys = make_document()
    document.layout.direction = direction
    document.layout.compact = len(str(kind)) % 2 == 0
    for index, node in enumerate(document.nodes.values()):
        node.style = {} if kind is None else {"branch": {"kind": kind.value}}
        if index in (3, 8):
            node.expanded = False
        if index in (1, 4):
            node.direction = "right"
    sizes = {key: measure(node) for key, node in document.nodes.items()}
    service = IncrementalTreeLayout(document, sizes)
    result = service.result()
    assert_layout(result, TreeLayout().layout(document, sizes))
    assert sorted(result.nodes, key=result.geometry.layout_order) == list(result.nodes)
    for key in keys[-4:]:
        if key in result.nodes:
            rect = result.nodes[key]
            assert result.bounds.x <= rect.x and result.bounds.right >= rect.right


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
def test_sync_entities_history_style_summary_and_frozen_versions(direction):
    document, keys = make_document()
    document.layout.direction = direction
    engine = MindMapEngine(document)
    read_model = engine.document
    sizes = {key: measure(node) for key, node in read_model.nodes.items()}
    service = IncrementalTreeLayout(read_model, sizes)
    result = service.result()
    old_results = [(result, dict(result.nodes), dict(result.branches))]

    def consume(event):
        if event.changes is None:
            return
        event.changes.apply(read_model, forward=event.forward)
        for key in event.node_ids:
            if key in read_model.nodes:
                sizes[key] = measure(read_model.nodes[key])
            else:
                sizes.pop(key, None)
        service.update(read_model, sizes, event.node_ids)
        current = service.result()
        assert_layout(current, TreeLayout().layout(read_model, sizes))
        assert service.style_kinds([read_model.root_id]) == {
            (BranchStyle(node.style["branch"]["kind"]) if "branch" in node.style else None)
            for node in read_model.nodes.values()
        }
        for previous, nodes, branches in old_results[-5:]:
            assert dict(previous.nodes) == nodes
            assert dict(previous.branches) == branches
        old_results.append((current, dict(current.nodes), dict(current.branches)))

    # 监听器异常不会使提交失败，最终直接比对，确保上述断言没有被事件边界吞掉。
    errors = []
    original_consume = consume

    def checked(event):
        try:
            original_consume(event)
        except Exception as exc:
            errors.append(exc)

    engine.subscribe(checked)
    child = engine.add_child(keys[-1], "新增有尺寸的主题")
    engine.insert_parent(child, "新增父级")
    engine.move_nodes([keys[1]], keys[2])
    engine.update_node(keys[3], direction="left", tags=["尺寸改变"])
    engine.set_expanded(keys[2], False)
    engine.set_branch_style([keys[2]], "arrow")
    engine.remove_nodes([keys[3]])
    for _ in range(7):
        assert engine.undo()
    for _ in range(7):
        assert engine.redo()
    assert not errors, errors
    assert read_model == engine.document


def test_persistent_mapping_keeps_old_roots_and_balances_insertions_and_deletions():
    randomizer = random.Random(411)
    values = PersistentMap()
    reference, snapshots = {}, []
    for turn in range(500):
        key = str(randomizer.randrange(120))
        if turn % 3:
            values[key] = reference[key] = turn
        else:
            values.pop(key, None)
            reference.pop(key, None)
        if turn % 37 == 0:
            snapshots.append((values.fork(), reference.copy()))
        assert dict(values) == reference
        for old, expected in snapshots:
            assert dict(old) == expected


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
def test_directional_keyboard_query_matches_full_scan(direction):
    document, keys = make_document(301)
    document.layout.direction = direction
    sizes = {key: measure(node) for key, node in document.nodes.items()}
    geometry = IncrementalTreeLayout(document, sizes).geometry
    all_rects = geometry.all_rectangles()
    for source in keys[::17]:
        center = all_rects[source].center
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            candidates = []
            for key, rect in all_rects.items():
                x, y = rect.center[0] - center[0], rect.center[1] - center[1]
                along = x * dx + y * dy
                if along > 1:
                    candidates.append((along + abs(y if dx else x) * 2, key))
            expected = min(candidates)[1] if candidates else None
            assert geometry.nearest(source, dx, dy) == expected
