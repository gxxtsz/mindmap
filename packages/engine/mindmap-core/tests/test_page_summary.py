"""最近访问所需的轻量摘要：稳定身份、路径更新、失效处理及无副作用查询。"""

from dataclasses import FrozenInstanceError

import pytest
from mindmap_core import MindMapEngine


def test_summary_covers_all_page_kinds_without_document_snapshots(monkeypatch):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "同名页面")
    submap = engine.create_submap(owner)
    detail = engine.create_detail_page(owner)
    idea = engine.add_inspiration("同名页面", position=(0, 0))
    targets = [(None, None), (submap, None), (detail, None), (engine.inspiration_id, idea)]
    before, revision = engine.document, engine.revision
    events = []
    engine.subscribe(events.append)
    with monkeypatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail("页面摘要不能复制整图或页面快照")
        patch.setattr(MindMapEngine, "document", property(forbidden))
        patch.setattr(engine, "page_document", forbidden)
        patch.setattr(engine, "capture_read_snapshot", forbidden)
        summaries = [engine.page_summary(*target) for target in targets]
    assert [summary.kind for summary in summaries] == ["main", "submap", "detail", "inspiration"]
    assert [summary.key for summary in summaries] == targets
    assert summaries[-1].path[0].text == "灵感区"
    assert summaries[1].path[-1].page_id == submap
    assert summaries[2].path[-1].page_id == detail
    assert engine.document == before and engine.revision == revision and not events
    with pytest.raises(FrozenInstanceError):
        summaries[0].title = "不能修改"


def test_summary_updates_names_and_paths_and_rejects_invalid_targets():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "旧标题")
    page = engine.create_submap(owner)
    old = engine.page_summary(page)
    engine.update_node(owner, text="新标题")
    assert old.title == "旧标题" and engine.page_summary(page).title == "新标题"
    engine.move_to_inspiration([owner], positions={owner: (0, 0)})
    assert engine.page_summary(page).path[0].text == "灵感区"
    assert engine.page_summary(engine.inspiration_id, owner).title == "新标题"
    engine.attach_inspirations([owner], engine.root_id)
    assert engine.page_summary(engine.inspiration_id, owner) is None
    assert engine.page_summary(engine.inspiration_id) is None
    assert engine.page_summary(None, owner) is None
    assert engine.page_summary(page, owner) is None
    engine.remove_nodes([owner])
    assert engine.page_summary(page) is None
    engine.undo()
    assert engine.page_summary(page).title == "新标题"


@pytest.mark.parametrize("page,root", [("不存在", None), ([], None), (None, "错误根"), ("@inspiration", [])])
def test_invalid_summary_inputs_return_none(page, root):
    assert MindMapEngine().page_summary(page, root) is None
