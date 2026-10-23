"""子导图收藏的有序查询、原子历史及删除、复制语义。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapEngine, MindMapError


def favorite_book():
    """构造同名页面及嵌套页面，收藏目标始终通过页面 ID 区分。"""
    engine = MindMapEngine()
    first_owner = engine.add_child(engine.root_id, "同名")
    first = engine.create_submap(first_owner)
    inner_owner = engine.add_child(engine.page_document(first).root_id, "内页")
    inner = engine.create_submap(inner_owner)
    second_owner = engine.add_child(engine.root_id, "同名")
    second = engine.create_submap(second_owner)
    return engine, first_owner, first, inner, second


def test_favorites_are_ordered_idempotent_and_resolve_live_titles():
    engine, owner, first, inner, second = favorite_book()
    assert engine.favorite_submaps() == ()
    engine.set_submap_favorite(first, True)
    engine.set_submap_favorite(second, True)
    entries = engine.favorite_submaps()
    assert [item.page_id for item in entries] == [second, first]
    assert [item.text for item in entries] == ["同名", "同名"]
    revision, document = engine.revision, engine.document
    engine.set_submap_favorite(first, True)
    engine.set_submap_favorite(inner, False)
    assert engine.revision == revision and engine.document == document
    engine.update_node(owner, text="新名称")
    assert engine.favorite_submaps()[-1].text == "新名称"
    assert entries[-1].text == "同名"
    engine.set_submap_favorite(first, False)
    engine.set_submap_favorite(first, True)
    assert [item.page_id for item in engine.favorite_submaps()] == [first, second]


def test_favorite_history_and_save_snapshots_are_isolated():
    engine, owner, first, inner, second = favorite_book()
    engine.mark_saved()
    snapshot = engine.capture_save_snapshot()
    before = deepcopy(snapshot.document)
    engine.set_submap_favorite(first, True)
    assert engine.is_dirty and engine.is_submap_favorite(first)
    assert snapshot.document == before
    assert engine.undo() and not engine.is_dirty and not engine.is_submap_favorite(first)
    assert engine.redo() and engine.is_submap_favorite(first)
    assert engine.undo() and engine.document == before


@pytest.mark.parametrize("remove_branch", [False, True])
def test_cascade_delete_restores_favorites_and_order_in_one_undo(remove_branch):
    engine, owner, first, inner, second = favorite_book()
    for page in (first, second, inner):
        engine.set_submap_favorite(page, True)
    before = engine.document
    engine.remove_nodes([owner]) if remove_branch else engine.remove_submap(owner)
    assert [item.page_id for item in engine.favorite_submaps()] == [second]
    assert engine.document.metadata["submap_favorites"] == [second]
    assert engine.undo() and engine.document == before
    assert engine.redo() and [item.page_id for item in engine.favorite_submaps()] == [second]


def test_copied_pages_do_not_inherit_favorites_and_failed_transaction_rolls_back():
    engine, owner, first, inner, second = favorite_book()
    engine.set_submap_favorite(first, True)
    fragment = engine.copy_fragment([owner])
    copied = engine.paste_fragment(engine.root_id, fragment)[0]
    assert not engine.is_submap_favorite(engine.get_node(copied).submap_id)
    before = engine.document
    with pytest.raises(MindMapError):
        with engine.transaction("失败的收藏操作"):
            engine.set_submap_favorite(inner, True)
            engine.set_submap_favorite(None, True)
    assert engine.document == before


@pytest.mark.parametrize("target", [None, "missing", [], 1])
def test_invalid_favorite_target_is_atomic(target):
    engine = MindMapEngine()
    before = engine.document
    with pytest.raises(MindMapError, match="有效的子导图"):
        engine.set_submap_favorite(target, True)
    assert not engine.is_submap_favorite(target) and engine.document == before


def test_detail_pages_and_non_boolean_states_are_rejected():
    engine = MindMapEngine()
    detail = engine.create_detail_page(engine.root_id)
    page = engine.create_submap(engine.root_id)
    before = engine.document
    with pytest.raises(MindMapError):
        engine.set_submap_favorite(detail, True)
    with pytest.raises(MindMapError, match="布尔"):
        engine.set_submap_favorite(page, 1)
    assert engine.document == before


def test_legacy_metadata_is_read_without_mutating_the_document():
    engine, owner, first, inner, second = favorite_book()
    document = engine.document
    document.metadata = {"other": {"value": 1}, "submap_favorites": [first, "missing", first, None]}
    engine.replace_document(document)
    assert [item.page_id for item in engine.favorite_submaps()] == [first]
    assert engine.document == document and not engine.is_dirty
    engine.set_submap_favorite(second, True)
    assert engine.document.metadata == {"other": {"value": 1}, "submap_favorites": [second, first]}
