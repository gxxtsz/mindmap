"""灵感区结构、无损迁移、快照及失败回滚的行为验收。"""

from copy import deepcopy

import pytest
from mindmap_core import LayoutRegistry, MindMapEngine, MindMapError, MindMapForestView
from mindmap_layouts import TreeLayout, create_incremental_layout


def test_complete_branch_roundtrip_and_history():
    engine = MindMapEngine()
    branch = engine.add_child(engine.root_id, "想法")
    child = engine.add_child(branch, "子主题")
    engine.attach_image(child, b"image")
    detail = engine.create_detail_page(branch)
    detail_child = engine.add_child(engine.document.detail_pages[detail].root_id, "详情内容")
    submap = engine.create_submap(child)
    engine.set_submap_favorite(submap, True)
    original = engine.document
    events = []
    engine.subscribe(events.append)
    assert engine.move_to_inspiration([branch, child, branch], positions={branch: (120, -90)}) == [branch]
    moved = engine.document
    assert len(events) == 1
    assert moved.nodes.keys() == original.nodes.keys()
    assert moved.nodes[branch] == original.nodes[branch]
    assert moved.detail_pages == original.detail_pages
    assert engine.node_page_id(child) == engine.inspiration_id
    assert engine.node_page_id(detail_child) == detail
    assert engine.node_path(detail_child)[0].text == "灵感区"
    assert engine.is_submap_favorite(submap)
    engine.document.validate()
    engine.attach_inspirations([branch], engine.root_id)
    assert engine.document == original
    engine.undo()
    assert engine.document == moved
    engine.undo()
    assert engine.document == original
    engine.redo()
    assert engine.document == moved


def test_multiple_trees_group_detach_and_delete():
    engine = MindMapEngine()
    a = engine.add_inspiration("甲", position=(0, 0))
    b = engine.add_inspiration("乙", position=(500, 40))
    c = engine.add_child(a, "丙")
    original = engine.document
    grouped = engine.group_inspirations([b, c, a], position=(100, 100))
    assert engine.get_node(grouped).children == [a, b]
    assert engine.document.inspiration.root_ids == [grouped]
    engine.document.validate()
    engine.move_to_inspiration([c], positions={c: (-200, 20)})
    assert engine.document.inspiration.root_ids == [grouped, c]
    engine.remove_nodes([grouped, c])
    assert not engine.document.inspiration.root_ids
    assert isinstance(engine.page_document(engine.inspiration_id), MindMapForestView)
    engine.document.validate()
    engine.undo()
    engine.undo()
    engine.undo()
    assert engine.document == original


def test_reparent_free_root_and_insert_parent():
    engine = MindMapEngine()
    a = engine.add_inspiration(position=(0, 0))
    b = engine.add_inspiration(position=(400, 0))
    engine.move_nodes([a], b)
    assert engine.document.inspiration.root_ids == [b]
    assert engine.parent_id(a) == b
    engine.undo()
    parent = engine.insert_parent(a)
    assert engine.parent_id(a) == parent
    engine.document.validate()


def test_owned_page_cycle_and_invalid_moves_are_atomic():
    engine = MindMapEngine()
    a = engine.add_inspiration(position=(0, 0))
    page = engine.create_submap(a)
    target = engine.document.submaps[page].root_id
    for action in (
        lambda: engine.attach_inspirations([a], target),
        lambda: engine.move_to_inspiration([engine.root_id], positions={engine.root_id: (0, 0)}),
        lambda: engine.set_inspiration_positions({a: (float("nan"), 0)}),
        lambda: engine.move_to_inspiration([target], positions={target: (1, 1)}),
    ):
        snapshot = engine.document
        revision = engine.revision
        with pytest.raises(MindMapError):
            action()
        assert engine.document == snapshot
        assert engine.revision == revision
        engine.document.validate()


def test_coordinates_snapshot_layout_and_noop():
    engine = MindMapEngine()
    a = engine.add_inspiration(position=(123, -67))
    b = engine.add_inspiration(position=(-500, 300))
    engine.add_child(a, "孩子")
    snapshot = engine.capture_save_snapshot()
    engine.set_inspiration_positions({a: (140, 70)})
    assert snapshot.document.inspiration.positions[a] == (123, -67)
    revision = engine.revision
    engine.set_inspiration_positions({a: (140, 70)})
    assert engine.revision == revision
    registry = LayoutRegistry()
    registry.register(TreeLayout())
    view = engine.page_document(engine.inspiration_id)
    assert create_incremental_layout(registry, view, {}) is None
    result = registry.layout(view)
    assert (result.nodes[a].x, result.nodes[a].y) == (140, 70)
    assert (result.nodes[b].x, result.nodes[b].y) == (-500, 300)
    assert len(result.branches) == 1
    engine.undo()
    assert engine.document == deepcopy(snapshot.document)
