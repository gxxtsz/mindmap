"""精确移动元数据：实际对象、前后位置、事务隔离及历史重放方向。"""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
from mindmap_core import (
    ChangeSet,
    LayoutConfig,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Node,
    NodeMove,
    NodePlacement,
)


def tree():
    """所有一级分支显式向右，避免比较同级交换时引入额外的自动方向变化。"""
    document = MindMapDocument.new("第二大脑")
    document.layout = LayoutConfig(direction="right")
    a, b, c = [Node(text, direction="right") for text in ("哲学", "07.人文学科", "其他主题")]
    child = Node("随行后代")
    a.children.append(child.id)
    document.root.children = [a.id, b.id, c.id]
    document.nodes.update({node.id: node for node in (a, b, c, child)})
    return MindMapEngine(document), (a.id, b.id, c.id, child.id)


def test_cross_parent_move_records_unchanged_node_without_including_parents_or_descendants():
    """真正移动的节点可不在实体差量里，不能把变更列表里的父节点当成移动对象。"""
    engine, (a, b, c, child) = tree()
    events = []
    engine.subscribe(events.append)
    engine.move_nodes([a], b)
    event = events[-1]
    assert a not in event.node_ids and child not in event.node_ids
    assert len(event.changes.moves) == 1
    move = event.changes.moves[0]
    assert move.node_id == a
    assert move.before == NodePlacement("哲学", engine.root_id, "第二大脑", 1, "right")
    assert move.after == NodePlacement("哲学", b, "07.人文学科", 1, "right")
    assert c not in event.node_ids and engine.parent_id(child) == a


def test_identical_sibling_swap_results_keep_different_dragged_nodes():
    """拖动甲到乙后面与拖动乙到甲前面结果相同，必须由事务意图区分对象。"""
    first, (a, b, *_) = tree()
    second = MindMapEngine(first.document)
    first_events, second_events = [], []
    first.subscribe(first_events.append)
    second.subscribe(second_events.append)
    first.move_nodes([a], b, "after")
    second.move_nodes([b], a, "before")
    assert first.document == second.document
    first_move, = first_events[-1].changes.moves
    second_move, = second_events[-1].changes.moves
    assert first_move.node_id == a and second_move.node_id == b
    assert (first_move.before.index, first_move.after.index) == (1, 2)
    assert (second_move.before.index, second_move.after.index) == (2, 1)
    assert first_move.before.parent_id == first_move.after.parent_id == first.root_id


def test_multi_selection_records_only_independent_roots_in_original_order():
    """去重并排除已选祖先的后代，新增父级内容和被动兄弟不生成额外移动项。"""
    engine, (a, b, c, child) = tree()
    events = []
    engine.subscribe(events.append)
    with engine.transaction("拖动节点"):
        engine.move_nodes([c, child, a, a], b)
    assert len(events) == 1
    moves = events[0].changes.moves
    assert [move.node_id for move in moves] == [a, c]
    assert [move.before.index for move in moves] == [1, 3]
    assert [move.after.index for move in moves] == [1, 2]
    assert all(move.before.parent_id == engine.root_id and move.after.parent_id == b for move in moves)
    assert engine.parent_id(child) == a


def test_automatic_side_is_resolved_before_and_after_same_position_move():
    """原始 direction=None 仍需准确描述有效方向，不能把自动方向当成未变化。"""
    engine, (a, b, c, _) = tree()
    document = engine.document
    document.layout = LayoutConfig(direction="both")
    for key in (a, b, c):
        document.nodes[key].direction = None
    engine = MindMapEngine(document)
    events = []
    engine.subscribe(events.append)
    engine.move_nodes([a], b, "before")
    move, = events[-1].changes.moves
    assert move.node_id == a and move.before.index == move.after.index == 1
    assert move.before.direction == "left" and move.after.direction == "right"


def test_reorder_and_side_change_are_both_recorded():
    engine, (a, b, c, _) = tree()
    document = engine.document
    document.layout = LayoutConfig(direction="both")
    document.nodes[b].direction = "left"
    engine = MindMapEngine(document)
    events = []
    engine.subscribe(events.append)
    engine.move_nodes([c], b, "before")
    move, = events[-1].changes.moves
    assert move.node_id == c
    assert (move.before.index, move.after.index) == (3, 2)
    assert (move.before.direction, move.after.direction) == ("right", "left")


@pytest.mark.parametrize("kind", ["submap", "detail"])
def test_owned_page_moves_use_page_root_and_layout(kind):
    """附属页的父名与有效方向从本页读取，不借用主导图根节点及布局。"""
    engine, (_, _, owner, _) = tree()
    page = engine.create_submap(owner) if kind == "submap" else engine.create_detail_page(owner)
    root = engine.document.page_record(page).root_id
    a = engine.add_child(root, "页内移动主题")
    b = engine.add_child(root, "页内目标父主题")
    engine.set_layout(LayoutConfig(direction="down"), page_id=page)
    events = []
    engine.subscribe(events.append)
    engine.move_nodes([a], b)
    move, = events[-1].changes.moves
    assert move.before.parent_id == root and move.before.parent_text == "其他主题"
    assert move.after.parent_id == b and move.after.parent_text == "页内目标父主题"
    assert move.before.direction == move.after.direction == "down"
    assert events[-1].changes.page_id == page


def test_snapshots_capture_transaction_endpoints_and_survive_later_renames():
    engine, (a, b, *_) = tree()
    events = []
    engine.subscribe(events.append)
    with engine.transaction("拖动节点"):
        engine.update_node(engine.root_id, text="更名后的主导图")
        engine.update_node(a, text="逻辑学")
        engine.update_node(b, text="更名后的目标")
        engine.move_nodes([a], b)
    move, = events[-1].changes.moves
    assert move.before.node_text == "哲学" and move.after.node_text == "逻辑学"
    assert move.before.parent_text == "第二大脑" and move.after.parent_text == "更名后的目标"
    engine.update_node(a, text="后续改名")
    engine.update_node(b, text="后续父级改名")
    assert move.after.node_text == "逻辑学" and move.after.parent_text == "更名后的目标"
    with pytest.raises(FrozenInstanceError):
        move.after.parent_text = "外部不能修改"


def test_undo_redo_keep_original_move_endpoints_and_apply_only_document_changes():
    engine, (a, b, *_) = tree()
    before, read_model, events = engine.document, engine.document, []

    def consume(event):
        events.append(event)
        event.changes.apply(read_model, forward=event.forward)

    engine.subscribe(consume)
    engine.move_nodes([a], b)
    moves = events[-1].changes.moves
    after = engine.document
    assert engine.undo() and read_model == engine.document == before
    assert events[-1].kind == "undo" and not events[-1].forward and events[-1].changes.moves == moves
    assert engine.redo() and read_model == engine.document == after
    assert events[-1].kind == "redo" and events[-1].forward and events[-1].changes.moves == moves


def test_listener_replacement_of_move_tuple_does_not_pollute_other_listeners_or_history():
    engine, (a, b, *_) = tree()
    events = []
    engine.subscribe(lambda event: setattr(event.changes, "moves", ()))
    engine.subscribe(events.append)
    engine.move_nodes([a], b)
    assert len(events[-1].changes.moves) == 1
    assert engine.undo() and len(events[-1].changes.moves) == 1


def test_empty_moves_preview_rollback_and_round_trip_do_not_create_history():
    engine, (a, b, _, child) = tree()
    before, events = engine.document, []
    engine.subscribe(events.append)
    engine.move_nodes([a], b, "before")
    with engine.transaction("原位往返"):
        engine.move_nodes([a], b)
        engine.move_nodes([a], b, "before")
    with engine.preview_operation():
        engine.move_nodes([a], b)
    with pytest.raises(MindMapError):
        with engine.transaction("移动失败"):
            engine.move_nodes([a], b)
            engine.move_nodes([b], child)
    assert engine.document == before and not events and not engine.can_undo and not engine.is_dirty
    engine.move_nodes([a], b)
    move, = events[-1].changes.moves
    assert move.before.parent_id == engine.root_id and move.after.parent_id == b


def test_default_metadata_and_snapshot_comparison_do_not_invent_actor_or_dirty_state():
    engine, (a, b, *_) = tree()
    before = engine.document
    engine.move_nodes([a], b, "after")
    assert ChangeSet({}, {}, None).moves == ()
    assert ChangeSet.between(before, engine.document).moves == ()
    position = NodePlacement("主题", "父节点", "父节点名称", 1, "right")
    changes = ChangeSet(moves=(NodeMove("节点", position, position),))
    unchanged = deepcopy(before)
    assert changes.empty
    changes.apply(before, forward=True)
    assert before == unchanged
