"""节点后代与子导图双向迁移的内容保留、原子历史及局部开销验收。"""

from copy import deepcopy

import pytest
from mindmap_core import LayoutConfig, MindMapEngine, MindMapError, MindMapPage, Node, Theme


def branch_with_pages():
    """构造有普通后代、嵌套页面和图片的分支，返回稳定标识供两种转换复用。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "课程")
    first = engine.add_child(owner, "第一章")
    deep = engine.add_child(first, "知识点")
    second = engine.add_child(owner, "第二章")
    nested = engine.create_submap(second)
    engine.add_child(engine.page_document(nested).root_id, "第二章正文")
    engine.create_detail_page(first)
    engine.attach_image(deep, "共享图片".encode())
    engine.update_node(first, direction="left", expanded=False, note="章节备注")
    engine.set_submap_favorite(nested, True)
    return engine, owner, first, second, nested


def test_roundtrip_preserves_nodes_resources_order_and_atomic_history():
    engine, owner, first, second, nested = branch_with_pages()
    before = engine.document
    captured = engine.capture_save_snapshot()
    frozen = deepcopy(captured.document)
    events = []
    engine.subscribe(events.append)
    page = engine.convert_descendants_to_submap(owner)
    root = engine.page_document(page).root_id
    converted = engine.document
    assert engine.get_node(owner).children == []
    assert engine.get_node(owner).submap_id == page
    assert engine.get_node(root).children == [first, second]
    assert converted.submaps[page].theme == before.theme
    assert converted.submaps[page].layout == before.layout
    assert converted.assets == before.assets
    assert all(converted.nodes[key] == node for key, node in before.nodes.items() if key != owner)
    assert engine.parent_id(first) == root and engine.node_page_id(first) == page
    assert engine.node_page_id(engine.page_document(nested).root_id) == nested
    assert not engine.is_submap_favorite(page)
    assert len(events) == 1
    assert {move.node_id for move in events[0].changes.moves} == {first, second}
    assert all(move.before.page_id is None and move.after.page_id == page for move in events[0].changes.moves)
    assert engine.undo() and engine.document == before
    assert engine.redo() and engine.document == converted
    events.clear()
    engine.convert_submap_to_descendants(owner)
    assert engine.document == before
    assert root not in engine.document.nodes
    assert len(events) == 1
    assert all(move.before.page_id == page and move.after.page_id is None for move in events[0].changes.moves)
    assert engine.undo() and engine.document == converted
    assert engine.redo() and engine.document == before
    assert captured.document == frozen
    engine.document.validate()


@pytest.mark.parametrize("patch", [
    {"note": "根的独立备注"}, {"hyperlink": "https://example.com"}, {"tags": ["标签"]},
    {"icons": ["星标"]}, {"style": {"font_size": 23}}, {"metadata": {"附加": "资料"}},
    {"text_format": "markdown"}, {"direction": "left"},
])
def test_independent_root_attributes_keep_root_and_its_children(patch):
    engine = MindMapEngine()
    owner = engine.root_id
    page = engine.create_submap(owner)
    root = engine.page_document(page).root_id
    child = engine.add_child(root, "正文")
    engine.update_node(root, **patch)
    before = engine.document
    root_before = engine.get_node(root)
    engine.convert_submap_to_descendants(owner)
    assert engine.get_node(owner).children == [root]
    assert engine.get_node(root) == root_before
    assert engine.get_node(root).children == [child]
    assert engine.parent_id(root) == owner and engine.node_page_id(child) is None
    engine.document.validate()
    assert engine.undo() and engine.document == before
    assert engine.redo() and engine.get_node(owner).children == [root]


def test_equal_root_attributes_and_expansion_do_not_add_an_extra_level():
    engine = MindMapEngine()
    owner = engine.root_id
    engine.update_node(owner, note="相同备注", tags=["共同标签"])
    page = engine.create_submap(owner)
    root = engine.page_document(page).root_id
    child = engine.add_child(root, "正文")
    engine.update_node(root, note="相同备注", tags=["共同标签"], expanded=False)
    engine.set_expanded(owner, False)
    engine.convert_submap_to_descendants(owner)
    assert engine.get_node(owner).children == [child]
    assert engine.get_node(owner).expanded
    assert root not in engine.document.nodes


@pytest.mark.parametrize("shared_image,different_width", [(False, False), (True, False), (True, True)])
def test_root_image_is_kept_when_content_or_size_differs(shared_image, different_width):
    engine = MindMapEngine()
    owner = engine.root_id
    page = engine.create_submap(owner)
    root = engine.page_document(page).root_id
    asset = engine.attach_image(root, "图片".encode())
    if shared_image:
        engine.attach_image(owner, "图片".encode())
    if different_width:
        engine.update_node(root, image_width=256)
    before = engine.document
    engine.convert_submap_to_descendants(owner)
    keep = not shared_image or different_width
    assert (root in engine.document.nodes) == keep
    assert engine.get_node(owner).children == ([root] if keep else [])
    assert asset in engine.document.assets
    assert engine.undo() and engine.document == before


@pytest.mark.parametrize("with_nested", [False, True])
def test_nested_root_and_legacy_detail_keep_identity_and_favorites(with_nested):
    engine = MindMapEngine()
    owner = engine.root_id
    page = engine.create_submap(owner)
    root = engine.page_document(page).root_id
    nested = engine.create_submap(root) if with_nested else None
    if not with_nested:
        engine.add_child(root, "原有正文")
    shared = engine.create_detail_page(owner)
    document = engine.document
    legacy_root = Node(document.nodes[root].text)
    legacy_page = MindMapPage("历史根详情", legacy_root.id, root)
    document.nodes[legacy_root.id] = legacy_root
    document.detail_pages[legacy_page.id] = legacy_page
    document.nodes[root].detail_page_id = legacy_page.id
    engine = MindMapEngine(document)
    if nested:
        engine.set_submap_favorite(nested, True)
    engine.set_submap_favorite(page, True)
    before = engine.document
    engine.convert_submap_to_descendants(owner)
    assert engine.get_node(owner).children == [root]
    assert engine.get_node(root).submap_id == nested
    assert engine.resolve_detail_page(owner) == (owner, shared)
    assert engine.resolve_detail_page(root) == (root, legacy_page.id)
    favorites = [nested] if nested else []
    assert engine.document.metadata.get("submap_favorites", []) == favorites
    engine.document.validate()
    assert engine.undo() and engine.document == before
    assert engine.redo() and engine.document.metadata.get("submap_favorites", []) == favorites


@pytest.mark.parametrize("location", ["main", "submap", "inspiration"])
def test_page_roots_can_convert_and_page_config_is_independent(location):
    engine = MindMapEngine()
    owner = engine.root_id
    source = None
    if location == "submap":
        source = engine.create_submap(owner)
        owner = engine.page_document(source).root_id
    elif location == "inspiration":
        owner = engine.add_inspiration("独立想法", position=(17, 38))
        source = engine.inspiration_id
    engine.set_layout(LayoutConfig(direction="both"), page_id=source)
    engine.set_theme(Theme.dark(), page_id=source)
    child = engine.add_child(owner, "正文")
    before = engine.document
    page = engine.convert_descendants_to_submap(owner)
    assert engine.node_page_id(child) == page
    assert engine.page_document(page).theme == Theme.dark()
    engine.set_theme(Theme(), page_id=page)
    assert engine.page_document(source).theme == Theme.dark()
    engine.convert_submap_to_descendants(owner)
    assert engine.document == before
    assert engine.node_page_id(child) == source


def test_empty_submap_converts_to_an_ordinary_leaf():
    engine = MindMapEngine()
    page = engine.create_submap(engine.root_id)
    before = engine.document
    engine.convert_submap_to_descendants(engine.root_id)
    assert engine.node_count == 1 and not engine.get_node(engine.root_id).submap_id
    assert not engine.document.submaps and not engine.get_node(engine.root_id).children
    assert engine.undo() and engine.document == before
    assert engine.document.has_page(page)


def test_invalid_targets_and_failed_outer_transaction_leave_no_partial_changes():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "入口")
    detail = engine.create_detail_page(owner)
    detail_root = engine.page_document(detail).root_id
    engine.add_child(detail_root, "详情后代")
    image = engine.add_image_child(engine.root_id, "图片".encode(), width=160)
    engine.add_child(image, "图片后代")
    leaf = engine.add_child(engine.root_id, "空叶子")
    page = engine.create_submap(owner)
    before, revision = engine.document, engine.revision
    for key in ("不存在", owner, detail_root, image, leaf):
        with pytest.raises(MindMapError):
            engine.convert_descendants_to_submap(key)
        assert engine.document == before and engine.revision == revision
    with pytest.raises(MindMapError):
        engine.convert_submap_to_descendants(detail_root)
    with pytest.raises(MindMapError):
        with engine.transaction("失败时回滚全部转换"):
            engine.convert_submap_to_descendants(owner)
            engine.convert_descendants_to_submap(detail_root)
    assert engine.document == before and engine.revision == revision
    assert engine.node_page_id(engine.page_document(page).root_id) == page
    with pytest.raises(MindMapError):
        with engine.transaction("正向转换失败后回滚新页及父引用"):
            engine.convert_descendants_to_submap(engine.root_id)
            engine.add_child(engine.root_id, "入口不能再拥有普通子节点")
    assert engine.document == before and engine.revision == revision
    assert engine.parent_id(image) == engine.root_id
    engine.before_operation = lambda name, args: False
    with pytest.raises(MindMapError):
        engine.convert_submap_to_descendants(owner)
    assert engine.document == before and engine.revision == revision


def test_conversion_only_reads_related_entities_and_never_copies_the_document(monkeypatch):
    engine, owner, first, second, nested = branch_with_pages()
    expected = engine.document

    class NoScan(dict):
        """允许字典单点读取和复制索引，禁止枚举完整实体表。"""

        def forbidden(self, *args, **kwargs):
            raise AssertionError("转换不能扫描全文件实体表")

        __iter__ = keys = values = items = forbidden

    def no_copy(self, memo):
        raise AssertionError("转换不能复制整份文档")

    for name in ("nodes", "submaps", "detail_pages"):
        setattr(engine._state.document, name, NoScan(getattr(engine._state.document, name)))
    with monkeypatch.context() as patch:
        patch.setattr(type(engine._state.document), "__deepcopy__", no_copy, raising=False)
        page = engine.convert_descendants_to_submap(owner)
        assert engine.node_page_id(first) == page
        engine.convert_submap_to_descendants(owner)
        assert engine.parent_id(first) == owner
        assert engine.undo() and engine.undo() and engine.redo() and engine.redo()
    for name in ("nodes", "submaps", "detail_pages"):
        setattr(engine._state.document, name, {key: value for key, value in
                                              dict.items(getattr(engine._state.document, name))})
    assert engine.document == expected
