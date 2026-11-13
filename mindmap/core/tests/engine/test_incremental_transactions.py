"""差量事务的隔离、回滚、索引及后台保存版本验收。"""

from copy import deepcopy

import pytest
from mindmap_core import Asset, MindMapDocument, MindMapEngine, MindMapError, Node, Theme


def document_with_nodes(count=10000):
    document = MindMapDocument.new()
    keys = [document.root_id]
    for index in range(1, count):
        node = Node(str(index))
        document.nodes[node.id] = node
        document.nodes[keys[(index - 1) // 5]].children.append(node.id)
        keys.append(node.id)
    return document, keys


def test_local_edits_and_history_never_scan_or_copy_entire_document(monkeypatch):
    document, keys = document_with_nodes()
    engine = MindMapEngine(document)

    class NoScan(dict):
        def forbidden(self, *args, **kwargs):
            raise AssertionError("局部事务不能枚举整张节点表")
        __iter__ = keys = values = items = forbidden

    def no_document_copy(self, memo):
        raise AssertionError("局部事务不能复制整份文档")

    engine._state.document.nodes = NoScan(engine._state.document.nodes)
    with monkeypatch.context() as patch:
        patch.setattr(MindMapDocument, "__deepcopy__", no_document_copy, raising=False)
        engine.set_expanded(keys[1], False)
        engine.update_node(keys[-1], text="新正文", metadata={"独立": [1, 2]})
        engine.move_nodes([keys[-1]], keys[-3])
        child = engine.add_child(keys[-3], "新增")
        engine.remove_nodes([child])
        for _ in range(5):
            assert engine.undo()
        for _ in range(5):
            assert engine.redo()
    engine._state.document.nodes = dict(dict.items(engine._state.document.nodes))
    current = engine.document
    current.validate()
    assert engine._state.parents == current.parents()


def test_nested_failure_restores_entities_settings_and_runtime_indexes():
    document, keys = document_with_nodes(90)
    engine = MindMapEngine(document)
    before = engine.document
    parents = engine._state.parents.copy()
    with pytest.raises(MindMapError, match="整体回滚"):
        with engine.transaction("复杂失败"):
            added = engine.add_child(keys[-1], "临时新增")
            engine.attach_image(added, "原子资源".encode())
            engine.move_nodes([keys[-2]], added)
            engine.remove_nodes([keys[2]])
            engine.set_theme(Theme.dark())
            engine.set_branch_style([keys[1]], "arrow")
            try:
                engine.update_node(keys[1], style={"font_size": -1})
                # 字段校验在最外层提交；用另一受控失败标记嵌套事务已失败。
                engine.move_nodes([engine.root_id], added)
            except MindMapError:
                pass
    assert engine.document == before
    assert engine._state.parents == parents
    assert not engine._state.asset_counts and not engine.can_undo
    engine.add_child(keys[1], "回滚后继续编辑")
    engine.document.validate()


def test_each_listener_receives_isolated_changes_and_reentrant_events_are_ordered():
    engine = MindMapEngine()
    read_model = engine.document
    observed = []

    def mutate_and_reenter(event):
        if event.operation == "update_node":
            event.changes.collections["nodes"][engine.root_id][1].text = "不能污染后续监听器"
            engine.add_child(engine.root_id, "重入修改")

    def consume(event):
        observed.append(event.revision)
        event.changes.apply(read_model, forward=event.forward)

    engine.subscribe(mutate_and_reenter)
    engine.subscribe(consume)
    engine.update_node(engine.root_id, text="正式正文")
    assert observed == sorted(observed) and len(observed) == 2
    assert read_model == engine.document
    assert engine.get_node(engine.root_id).text == "正式正文"
    assert engine.undo() and engine.undo()
    assert engine.get_node(engine.root_id).text == "中心主题"


def test_save_snapshot_is_stable_across_changes_and_old_save_preserves_new_dirty_state():
    document, keys = document_with_nodes(20)
    engine = MindMapEngine(document)
    engine.update_node(keys[1], text="版本甲", style={"branch": {"kind": "round"}})
    saved = engine.capture_save_snapshot()
    snapshot = saved.document
    expected = deepcopy(snapshot)
    engine.update_node(keys[1], text="版本乙")
    engine.set_branch_style([engine.root_id], "arrow")
    engine.move_nodes([keys[-1]], keys[-3])
    assert snapshot == expected
    assert engine.mark_saved_snapshot(saved) and engine.is_dirty
    for _ in range(3):
        assert engine.undo()
    assert not engine.is_dirty and engine.document == snapshot
    assert engine.redo() and engine.is_dirty
    engine.replace_document(MindMapDocument.new("另一文档"))
    engine.update_node(engine.root_id, text="另一文档的未保存修改")
    assert not engine.mark_saved_snapshot(saved) and engine.is_dirty


def test_read_snapshot_avoids_entity_copy_and_remains_stable_during_edits(monkeypatch):
    """读版本不遍历复制节点，后续文字、结构及历史重放均不改写已捕获内容。"""
    document, keys = document_with_nodes(30)
    engine = MindMapEngine(document)

    def reject_copy(*args):
        raise AssertionError("捕获只读版本不应复制节点")

    with monkeypatch.context() as patch:
        patch.setattr(Node, "__deepcopy__", reject_copy)
        snapshot = engine.capture_read_snapshot()
    expected = deepcopy(snapshot)
    engine.update_node(keys[1], text="新的正式内容")
    engine.move_nodes([keys[-1]], keys[2])
    assert snapshot == expected
    assert engine.undo() and engine.redo()
    assert snapshot == expected
    with pytest.raises(MindMapError, match="事务"):
        with engine.transaction():
            engine.capture_read_snapshot()


def test_read_model_can_apply_events_without_mutating_engine():
    """只读版本的顶层映射独立，事件重放替换实体时不会污染引擎及其他读版本。"""
    document, keys = document_with_nodes(20)
    engine = MindMapEngine(document)
    first, second = engine.capture_read_snapshot(), engine.capture_read_snapshot()
    before = deepcopy(second)
    unsubscribe = engine.subscribe(lambda event: event.changes.apply(first, forward=event.forward))
    try:
        engine.update_node(keys[1], text="已提交的正文")
        assert first == engine.document and second == before
        first.nodes.pop(keys[-1])
        assert keys[-1] in engine.document.nodes and keys[-1] in second.nodes
    finally:
        unsubscribe()


def test_unused_asset_restored_by_undo_is_pruned_by_next_commit():
    document = MindMapDocument.new()
    asset = Asset.from_bytes("未关联资源".encode())
    document.assets[asset.id] = asset
    engine = MindMapEngine(document)
    engine.update_node(engine.root_id, text="触发清理")
    assert not engine.document.assets
    assert engine.undo() and asset.id in engine.document.assets
    engine.set_expanded(engine.root_id, False)
    assert not engine.document.assets


def test_internal_preview_restores_structure_and_never_calls_hooks_or_publishes():
    document, keys = document_with_nodes(40)
    engine = MindMapEngine(document)
    events, hooks = [], []
    engine.subscribe(events.append)
    engine.before_operation = lambda *args: hooks.append(args)
    before = engine.document
    with engine.preview_operation() as (candidate, touched):
        engine.move_nodes([keys[-1]], keys[-3])
        assert keys[-1] in candidate.nodes[keys[-3]].children
        assert keys[-3] in touched
    assert not events and not hooks and not engine.can_undo and engine.revision == 0
    assert engine.document == before and engine._state.parents == before.parents()
    with pytest.raises(RuntimeError):
        with engine.preview_operation():
            engine.remove_nodes([keys[1]])
            raise RuntimeError("试算拒绝")
    assert engine.document == before
    engine.move_nodes([keys[-1]], keys[-3])
    assert events and hooks and engine.can_undo
