"""入口与子导图根的共享详情、历史兼容及查询隔离验收。"""

import pytest
from mindmap_core import LayoutConfig, MindMapEngine, MindMapError, MindMapPage, Node, Theme


def legacy_details(engine, *owners):
    """用独立快照构造旧版允许的根节点独立详情，不借用新的创建接口。"""
    document = engine.document
    for owner in owners:
        root = Node(document.nodes[owner].text)
        page = MindMapPage(f"历史详情-{owner}", root.id, owner)
        document.nodes[root.id] = root
        document.nodes[owner].detail_page_id = page.id
        document.detail_pages[page.id] = page
    return MindMapEngine(document)


@pytest.mark.parametrize("order", ["detail_first", "submap_first", "from_root"])
def test_owner_and_root_share_creation_removal_and_history(order):
    """不同创建顺序都只产生一张详情，两个入口可删除并完整撤销重做。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "Rust")
    detail = engine.create_detail_page(owner) if order == "detail_first" else None
    submap = engine.create_submap(owner)
    root = engine.page_document(submap).root_id
    if detail is None:
        detail = engine.create_detail_page(root if order == "from_root" else owner)
    assert engine.resolve_detail_page(owner) == engine.resolve_detail_page(root) == (owner, detail)
    assert engine.get_node(root).detail_page_id is None
    assert len(engine.document.detail_pages) == 1
    before, revision = engine.document, engine.revision
    for node in (root, owner):
        with pytest.raises(MindMapError, match="已有详情页"):
            engine.create_detail_page(node)
        assert engine.document == before and engine.revision == revision
    engine.remove_detail_page(root)
    assert engine.resolve_detail_page(owner) == engine.resolve_detail_page(root) == (owner, None)
    assert engine.get_node(owner).submap_id == submap
    assert engine.undo() and engine.document == before
    assert engine.redo() and not engine.document.detail_pages
    assert engine.undo() and engine.document == before
    engine.remove_detail_page(owner)
    assert not engine.document.detail_pages
    engine.document.validate()


def test_nested_root_aliases_share_details_but_regular_children_do_not():
    """连续子导图根共用最外层入口，普通子主题和详情根不沿普通父链继承。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "Rust")
    aliases = [owner]
    for _ in range(4):
        page = engine.create_submap(aliases[-1])
        aliases.append(engine.page_document(page).root_id)
    child = engine.add_child(aliases[-1], "所有权")
    engine.update_node(aliases[-1], style={"background": "#123456"})
    engine.set_theme(Theme.dark(), page_id=page)
    engine.set_layout(LayoutConfig(direction="left"), page_id=page)
    detail = engine.create_detail_page(aliases[-1])
    detail_document = engine.page_document(detail)
    assert detail_document.root.style == {"background": "#123456"}
    assert detail_document.theme == Theme.dark() and detail_document.layout.direction == "left"
    assert engine.document.detail_pages[detail].owner_id == owner
    assert all(engine.resolve_detail_page(key) == (owner, detail) for key in aliases)
    assert engine.resolve_detail_page(child) == (child, None)
    assert engine.resolve_detail_page(detail_document.root_id) == (detail_document.root_id, None)
    child_detail = engine.create_detail_page(child)
    assert engine.resolve_detail_page(child) == (child, child_detail)
    with pytest.raises(MindMapError, match="详情页内"):
        engine.create_detail_page(detail_document.root_id)
    engine.document.validate()


@pytest.mark.parametrize("conflict", [False, True])
def test_legacy_root_details_remain_accessible_without_merging(conflict):
    """旧文件单详情双向复用，多详情各自入口优先，删除和撤销不丢失其他内容。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "Rust")
    submap = engine.create_submap(owner)
    root = engine.page_document(submap).root_id
    nested = engine.create_submap(root)
    nested_root = engine.page_document(nested).root_id
    engine = legacy_details(engine, root, *([owner] if conflict else []))
    root_detail = engine.get_node(root).detail_page_id
    outer_detail = engine.get_node(owner).detail_page_id if conflict else root_detail
    assert engine.resolve_detail_page(owner) == (owner if conflict else root, outer_detail)
    assert engine.resolve_detail_page(root) == (root, root_detail)
    assert engine.resolve_detail_page(nested_root) == (owner if conflict else root, outer_detail)
    before = engine.document
    with pytest.raises(MindMapError, match="已有详情页"):
        engine.create_detail_page(nested_root)
    engine.remove_detail_page(root)
    if conflict:
        assert engine.resolve_detail_page(root) == (owner, outer_detail)
        assert outer_detail in engine.document.detail_pages
    else:
        assert engine.resolve_detail_page(owner) == (owner, None)
    assert engine.undo() and engine.document == before


def test_resolving_details_is_readonly_and_does_not_scan_document():
    """查询只访问别名链，不能建立整图副本、枚举页面、发出事件或改变保存状态。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "Rust")
    submap = engine.create_submap(owner)
    root = engine.page_document(submap).root_id
    detail = engine.create_detail_page(root)
    engine.mark_saved()
    revision = engine.revision
    events = []
    engine.subscribe(events.append)

    class NoScan(dict):
        def forbidden(self, *args, **kwargs):
            raise AssertionError("共享详情查询不能枚举整份文档")

        __iter__ = keys = values = items = forbidden

    for name in ("nodes", "submaps", "detail_pages"):
        setattr(engine._state.document, name, NoScan(getattr(engine._state.document, name)))
    assert engine.resolve_detail_page(root) == (owner, detail)
    assert engine.resolve_detail_page(owner) == (owner, detail)
    with pytest.raises(MindMapError, match="节点不存在"):
        engine.resolve_detail_page("失效节点")
    assert engine.revision == revision and not engine.is_dirty and not events
