"""公开保存凭据及受控试算的版本、隔离和失败语义。"""

from dataclasses import FrozenInstanceError

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, MindMapError, SaveSnapshot


def test_save_credential_rejects_other_engine_and_old_document():
    engine = MindMapEngine()
    engine.add_child(engine.root_id, "保存版本")
    snapshot = engine.capture_save_snapshot()
    assert isinstance(snapshot, SaveSnapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.document = MindMapDocument.new()
    other = MindMapEngine(engine.document)
    other.update_node(other.root_id, text="其他引擎的修改")
    events = []
    other.subscribe(events.append)
    assert not other.mark_saved_snapshot(snapshot) and other.is_dirty and not events
    engine.replace_document(MindMapDocument.new("新文档"))
    engine.update_node(engine.root_id, text="新修改")
    engine.subscribe(events.append)
    assert not engine.mark_saved_snapshot(snapshot) and engine.is_dirty and not events
    with pytest.raises(TypeError):
        engine.mark_saved_snapshot(object())


def test_preview_exposes_only_readonly_changed_ids_and_always_rolls_back():
    engine = MindMapEngine()
    before = engine.document
    events, hooks = [], []
    engine.subscribe(events.append)
    engine.before_operation = lambda *args: hooks.append(args)
    with engine.preview_operation() as (candidate, changed):
        child = engine.add_child(engine.root_id, "临时子节点")
        assert child in candidate.nodes and child in changed and engine.root_id in changed
        with pytest.raises(AttributeError):
            changed.add("不能添加")
    assert engine.document == before and not engine.can_undo and not events and not hooks
    with pytest.raises(RuntimeError):
        with engine.preview_operation():
            engine.add_child(engine.root_id, "异常候选")
            raise RuntimeError("候选计算失败")
    assert engine.document == before and not events


@pytest.mark.parametrize("operation", ["preview", "capture", "mark"])
def test_version_apis_reject_active_transactions_and_restore_state(operation):
    engine = MindMapEngine()
    snapshot = engine.capture_save_snapshot()
    before = engine.document
    with pytest.raises(MindMapError):
        with engine.transaction():
            engine.add_child(engine.root_id, "必须回滚")
            if operation == "preview":
                with engine.preview_operation():
                    pass
            elif operation == "capture":
                engine.capture_save_snapshot()
            else:
                engine.mark_saved_snapshot(snapshot)
    assert engine.document == before and not engine.can_undo
