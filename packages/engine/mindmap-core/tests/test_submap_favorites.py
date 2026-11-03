"""子导图收藏的有序查询、原子历史及删除、复制语义。"""

from copy import deepcopy

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, MindMapError, MindMapPage, Node


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


def test_document_order_preserves_recent_order_storage_and_readonly_state():
    """展示排序不能改写收藏顺序、保存标记、历史或发送修改通知。"""
    engine, owner, first, inner, second = favorite_book()
    for page in (first, second, inner):
        engine.set_submap_favorite(page, True)
    engine.mark_saved()
    before = engine.document, engine.revision, engine.undo_label
    events = []
    unsubscribe = engine.subscribe(events.append)
    try:
        assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [first, inner, second]
        assert [entry.page_id for entry in engine.favorite_submaps()] == [inner, second, first]
        assert (engine.document, engine.revision, engine.undo_label) == before
        assert not engine.is_dirty and not events
        with pytest.raises(ValueError, match="收藏排序"):
            engine.favorite_submaps(order="unknown")
    finally:
        unsubscribe()


def test_document_order_follows_ordinary_ancestors_reordering_and_history():
    """跨未收藏页面及普通分组时按原分支顺序比较，不能按页面创建时间或标题比较。"""
    engine, owner, first, inner, second = favorite_book()
    second_owner = engine.document.submaps[second].owner_id
    group = engine.add_child(engine.root_id, "普通分组", index=0)
    engine.move_nodes([owner], group)
    for page in (inner, second):
        engine.set_submap_favorite(page, True)
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [inner, second]
    engine.move_nodes([group], second_owner, "after")
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [second, inner]
    assert engine.undo()
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [inner, second]
    assert engine.redo()
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [second, inner]


def test_document_order_places_main_before_inspiration_and_tracks_attachment():
    """区域间顺序固定，灵感区遵循游离根顺序，并正确处理连续根节点子导图。"""
    document = MindMapDocument.new()
    # 区域标识与节点标识可以同名，排序不能把两者的兄弟序号表混在一起。
    document.inspiration.id = document.root_id
    engine = MindMapEngine(document)
    first_root = engine.add_inspiration("同名", position=(0, 0))
    second_root = engine.add_inspiration("同名", position=(200, 0))
    second = engine.create_submap(second_root)
    first = engine.create_submap(first_root)
    nested = engine.create_submap(engine.page_document(first).root_id)
    main_owner = engine.add_child(engine.root_id, "同名")
    main = engine.create_submap(main_owner)
    for page in (nested, main, first, second):
        engine.set_submap_favorite(page, True)
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [main, first, nested, second]
    engine.attach_inspirations([first_root], engine.root_id)
    engine.move_nodes([first_root], main_owner, "before")
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [first, nested, main, second]


def test_document_order_does_not_scan_collections_or_capture_document_snapshots(monkeypatch):
    """直接禁止枚举节点、页面及父索引，确保查询只访问收藏祖先和相关兄弟列表。"""
    engine, owner, first, inner, second = favorite_book()
    for page in (first, second, inner):
        engine.set_submap_favorite(page, True)

    class NoScan(dict):
        def forbidden(self, *args, **kwargs):
            raise AssertionError("收藏排序不能枚举整份文档")

        __iter__ = keys = values = items = forbidden

    def no_document_copy(self, memo):
        raise AssertionError("收藏排序不能复制整份文档")

    engine._state.document.nodes = NoScan(engine._state.document.nodes)
    engine._state.document.submaps = NoScan(engine._state.document.submaps)
    engine._state.parents = NoScan(engine._state.parents)
    monkeypatch.setattr(MindMapDocument, "__deepcopy__", no_document_copy, raising=False)
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [first, inner, second]


def test_document_order_supports_page_chains_beyond_python_recursion_limit():
    """直接构造长链以单独验证排序，避免把大量编辑和历史构建混进该测试。"""
    document = MindMapDocument.new("连续根")
    owner, pages = document.root_id, []
    for depth in range(1100):
        root = Node("连续根")
        page = MindMapPage(f"page-{depth}", root.id, owner)
        document.nodes[owner].submap_id = page.id
        document.nodes[root.id] = root
        document.submaps[page.id] = page
        pages.append(page.id)
        owner = root.id
    document.metadata["submap_favorites"] = [pages[-1], pages[400], pages[0]]
    engine = MindMapEngine(document)
    assert [entry.page_id for entry in engine.favorite_submaps(order="document")] == [pages[0], pages[400], pages[-1]]
