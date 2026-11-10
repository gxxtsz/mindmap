"""详情页归属、单层限制、页面共存及事务历史的行为验收。"""

from copy import deepcopy

import pytest
from mindmap_core import LayoutConfig, MindMapEngine, MindMapError, Theme


def populated():
    """构造同时拥有两类页面的入口，并在子导图内部再附加详情。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "用户系统")
    detail = engine.create_detail_page(owner)
    content = engine.add_child(engine.page_document(detail).root_id, "业务说明")
    engine.attach_image(content, b"detail-image")
    submap = engine.create_submap(owner)
    child = engine.add_child(engine.page_document(submap).root_id, "权限")
    nested_detail = engine.create_detail_page(child)
    return engine, owner, detail, submap, nested_detail


def test_all_regular_nodes_can_have_details_without_changing_submap_rules():
    engine, owner, detail, submap, _ = populated()
    root_detail = engine.create_detail_page(engine.root_id)
    assert engine.get_node(owner).detail_page_id == detail
    assert engine.get_node(owner).submap_id == submap
    assert engine.node_page_id(engine.page_document(root_detail).root_id) == root_detail
    sibling = engine.add_child(engine.root_id, "普通分支")
    engine.add_child(sibling, "普通子节点")
    engine.create_detail_page(sibling)
    engine.add_child(sibling, "详情入口仍可新增普通子节点")
    with pytest.raises(MindMapError, match="叶子"):
        engine.create_submap(sibling)
    with pytest.raises(MindMapError, match="已有子导图"):
        engine.add_child(owner)
    with pytest.raises(MindMapError, match="已有详情页"):
        engine.create_detail_page(owner)
    engine.document.validate()


def test_detail_nodes_reject_both_pages_and_direct_reference_changes_atomically():
    engine, owner, detail, _, _ = populated()
    root = engine.page_document(detail).root_id
    leaf = engine.add_child(root, "详情中的叶子")
    before, revision = engine.document, engine.revision
    for key in (root, leaf):
        for create in (engine.create_detail_page, engine.create_submap):
            with pytest.raises(MindMapError, match="详情页内"):
                create(key)
            assert engine.document == before and engine.revision == revision
    with pytest.raises(MindMapError, match="直接修改"):
        engine.update_node(owner, detail_page_id=None)
    with pytest.raises(MindMapError):
        with engine.transaction("失败操作整体回滚"):
            engine.add_child(leaf, "不得残留")
            engine.create_detail_page(leaf)
    assert engine.document == before


def test_titles_sync_across_both_relations_but_configs_and_snapshots_are_independent():
    engine, owner, detail, submap, _ = populated()
    captured = engine.capture_save_snapshot()
    frozen = deepcopy(captured.document)
    root = engine.page_document(detail).root_id
    engine.update_node(root, text="账户模块")
    assert engine.get_node(owner).text == engine.page_document(submap).root.text == "账户模块"
    assert engine.undo() and engine.get_node(owner).text == "用户系统"
    assert engine.redo() and engine.get_node(root).text == "账户模块"
    engine.set_layout(LayoutConfig(direction="left"), page_id=detail)
    engine.set_theme(Theme.dark(), page_id=detail)
    assert engine.page_document(detail).layout.direction == "left"
    assert engine.page_document(submap).layout.direction == "right"
    assert engine.page_document().theme != Theme.dark()
    assert engine.page_document(detail).theme == Theme.dark()
    assert captured.document == frozen


def test_copy_and_separate_deletions_keep_the_other_owned_page():
    engine, owner, detail, submap, nested = populated()
    before = engine.document
    copied = engine.paste_fragment(engine.root_id, engine.copy_fragment([owner]))[0]
    assert engine.get_node(copied).detail_page_id != detail
    assert engine.get_node(copied).submap_id != submap
    assert len(engine.document.detail_pages) == 4
    engine.update_node(copied, text="副本")
    assert engine.get_node(owner).text == "用户系统"
    assert engine.undo() and engine.undo() and engine.document == before
    engine.remove_submap(owner)
    assert engine.get_node(owner).detail_page_id == detail
    assert nested not in engine.document.detail_pages and not engine.document.submaps
    assert engine.document.assets
    assert engine.undo() and engine.document == before
    engine.remove_detail_page(owner)
    assert engine.get_node(owner).submap_id == submap
    assert nested in engine.document.detail_pages and not engine.document.assets
    assert engine.undo() and engine.document == before
    engine.remove_nodes([owner])
    assert not engine.document.detail_pages and not engine.document.submaps and not engine.document.assets
    assert engine.undo() and engine.document == before


def test_detail_paste_explicitly_omits_pages_without_modifying_fragment():
    engine, owner, detail, _, _ = populated()
    child = engine.add_child(engine.root_id, "含普通后代")
    ordinary = engine.add_child(child, "保留的内容")
    engine.attach_image(ordinary, b"ordinary-image")
    engine.create_detail_page(child)
    fragment = engine.copy_fragment([owner, child])
    original = deepcopy(fragment)
    target = engine.page_document(detail).root_id
    before = engine.document
    with pytest.raises(MindMapError, match="详情页内"):
        engine.paste_fragment(target, fragment)
    assert engine.document == before
    roots = engine.paste_fragment(target, fragment, include_pages=False)
    copied = engine.page_document(detail)
    assert len(roots) == 2
    assert all(not node.detail_page_id and not node.submap_id for node in copied.nodes.values())
    assert any(node.text == "保留的内容" and node.image_id for node in copied.nodes.values())
    assert fragment == original
    assert engine.undo() and engine.document == before
    assert engine.redo()
    engine.document.validate()


def test_detail_local_operations_do_not_scan_other_pages():
    engine, _, detail, _, _ = populated()
    root = engine.page_document(detail).root_id

    class NoScan(dict):
        def forbidden(self, *args, **kwargs):
            raise AssertionError("详情页局部编辑不能枚举整份文档")

        __iter__ = keys = values = items = forbidden

    for name in ("nodes", "submaps", "detail_pages"):
        setattr(engine._state.document, name, NoScan(getattr(engine._state.document, name)))
    key = engine.add_child(root, "局部内容")
    engine.update_node(key, text="局部修改")
    engine.set_theme(Theme.dark(), page_id=detail)
    assert engine.undo() and engine.undo() and engine.undo()
    assert engine.redo() and engine.redo() and engine.redo()
