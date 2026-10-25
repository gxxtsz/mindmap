"""交互来源的嵌套恢复、重入通知及共享历史语义。"""

import pytest
from mindmap_core import MindMapEngine


def test_source_context_restores_on_exception_and_undo_uses_current_source():
    engine = MindMapEngine()
    events = []
    engine.subscribe(events.append)
    with engine.event_source("标签甲"):
        engine.add_child(engine.root_id, "甲")
        with pytest.raises(RuntimeError), engine.event_source("标签乙"):
            engine.add_child(engine.root_id, "乙")
            raise RuntimeError("离开内层")
        engine.add_child(engine.root_id, "丙")
    with engine.event_source("标签乙"):
        assert engine.undo()
    assert engine.redo()
    assert [event.source_id for event in events] == ["标签甲", "标签乙", "标签甲", "标签乙", None]


def test_reentrant_events_keep_source_at_creation_for_every_subscriber():
    engine = MindMapEngine()
    first, second = [], []

    def reenter(event):
        first.append(event.source_id)
        if len(first) == 1:
            with engine.event_source("重入标签"):
                engine.add_child(engine.root_id, "重入")

    engine.subscribe(reenter)
    engine.subscribe(lambda event: second.append(event.source_id))
    with engine.event_source("原标签"):
        engine.add_child(engine.root_id, "最初")
    assert first == second == ["原标签", "重入标签"]


@pytest.mark.parametrize("source", ["", 1, False, []])
def test_invalid_source_does_not_change_engine(source):
    engine = MindMapEngine()
    with pytest.raises(ValueError), engine.event_source(source):
        pytest.fail("非法来源不能进入上下文")
    assert not engine.can_undo and engine.revision == 0
