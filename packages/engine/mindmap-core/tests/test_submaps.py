"""子导图的叶子约束、跨页名称、全局历史及深层图结构验收。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapEngine, MindMapError, Theme


def nested():
    engine = MindMapEngine()
    branch = engine.add_child(engine.root_id, "基础")
    owner = engine.add_child(branch, "整数")
    page = engine.create_submap(owner)
    root = engine.page_document(page).root_id
    leaf = engine.add_child(root, "十进制")
    inner = engine.create_submap(leaf)
    return engine, branch, owner, page, root, leaf, inner


def test_leaf_constraints_title_aliases_and_atomic_history():
    engine, branch, owner, page, root, leaf, inner = nested()
    for key in (branch, owner):
        with pytest.raises(MindMapError):
            engine.create_submap(key)
    before = engine.document
    for operation in (
        lambda: engine.add_child(owner),
        lambda: engine.move_nodes([branch], owner),
        lambda: engine.paste_fragment(owner, engine.copy_fragment([leaf])),
        lambda: engine.update_node(owner, submap_id=None),
    ):
        with pytest.raises(MindMapError):
            operation()
        assert engine.document == before
    engine.update_node(root, text="新的整数")
    assert engine.get_node(owner).text == "新的整数"
    assert engine.undo() and engine.document == before
    assert engine.redo() and engine.get_node(owner).text == "新的整数"
    engine.document.validate()
    path = engine.node_path(engine.page_document(inner).root_id)
    assert [item.text for item in path] == ["中心主题", "基础", "新的整数", "十进制"]
    assert [item.page_id for item in path] == [None, None, page, inner]


def test_delete_copy_and_resources_include_owned_pages():
    engine, branch, owner, page, root, leaf, inner = nested()
    engine.attach_image(engine.page_document(inner).root_id, "共享图片".encode())
    before = engine.document
    fragment = engine.copy_fragment([owner])
    copied = engine.paste_fragment(engine.root_id, fragment)[0]
    assert engine.get_node(copied).submap_id not in (None, page)
    assert len(engine.document.submaps) == 4
    engine.update_node(copied, text="副本")
    assert engine.get_node(owner).text == "整数"
    assert engine.undo() and engine.undo() and engine.document == before
    engine.remove_submap(owner)
    assert not engine.document.submaps and not engine.document.assets
    assert engine.get_node(owner).submap_id is None
    assert engine.undo() and engine.document == before
    engine.remove_nodes([branch])
    assert not engine.document.submaps and not engine.document.assets
    assert engine.undo() and engine.document == before


def test_page_config_snapshots_and_failed_nested_transaction_are_isolated():
    engine, branch, owner, page, root, leaf, inner = nested()
    before = engine.document
    captured = engine.capture_save_snapshot()
    expected = deepcopy(captured.document)
    engine.set_theme(Theme.dark(), page_id=page)
    assert engine.page_document(page).theme == Theme.dark()
    assert engine.page_document().theme == before.theme
    assert captured.document == expected
    assert engine.undo() and engine.document == before
    with pytest.raises(MindMapError):
        with engine.transaction():
            engine.remove_submap(owner)
            engine.create_submap(owner)
            engine.add_child(owner)
    assert engine.document == before
    assert engine.node_page_id(root) == page
    assert engine.node_page_id(leaf) == page


def test_root_only_page_chain_is_iterative_and_keeps_each_navigation_target():
    engine = MindMapEngine(history_limit=2)
    key = engine.root_id
    for _ in range(1050):
        page = engine.create_submap(key)
        key = engine.page_document(page).root_id
    document = engine.document
    document.validate()
    assert len(engine.node_path(key)) == 1051
    engine.update_node(key, text="整条名称链")
    assert engine.get_node(engine.root_id).text == "整条名称链"
    engine.remove_submap(engine.root_id)
    assert engine.node_count == 1
    assert engine.undo() and len(engine.document.submaps) == 1050


def test_local_submap_edit_does_not_scan_other_pages_or_copy_the_document(monkeypatch):
    """局部页内编辑沿父链查询归属，页面很多时也不能退回全文件遍历。"""
    engine, branch, owner, page, root, leaf, inner = nested()

    class NoScan(dict):
        def forbidden(self, *args, **kwargs):
            raise AssertionError("局部编辑不能枚举全文件的节点表或页面表")

        __iter__ = keys = values = items = forbidden

    def no_document_copy(self, memo):
        raise AssertionError("局部编辑不能复制整份文件")

    engine._state.document.nodes = NoScan(engine._state.document.nodes)
    engine._state.document.submaps = NoScan(engine._state.document.submaps)
    with monkeypatch.context() as patch:
        patch.setattr(type(engine._state.document), "__deepcopy__", no_document_copy, raising=False)
        key = engine.add_child(root, "局部新增")
        engine.update_node(key, text="局部重命名")
        engine.set_theme(Theme.dark(), page_id=page)
        assert engine.node_page_id(key) == page
        assert engine.undo() and engine.undo() and engine.undo()
        assert engine.redo() and engine.redo() and engine.redo()
