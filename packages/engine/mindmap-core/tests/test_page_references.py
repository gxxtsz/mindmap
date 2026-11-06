"""验证页面引用与所有权分离、原文保留和结构事务的联动。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError, PathEntry


@pytest.mark.parametrize("kind", ["main", "submap", "detail"])
def test_reference_displays_only_target_body_without_overwriting_source(kind):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "章节")
    page = engine.create_detail_page(owner) if kind == "detail" else engine.create_submap(owner)
    source_page = page if kind == "main" else None
    target_page = None if kind == "main" else page
    source = engine.add_child(engine.page_document(source_page).root_id, "原有正文")
    target = engine.page_document(target_page).root_id
    child = engine.add_child(source, "保留分支")
    detail = engine.create_detail_page(source)
    image = engine.attach_image(source, b"image")
    engine.update_node(source, note="自有备注", style={"bold": True})

    engine.set_page_reference(source, target)
    engine.update_node(target, text="## 新正文", text_format="markdown", note="不引用目标备注")
    shown = engine.page_document(source_page).nodes[source]
    assert (shown.text, shown.text_format) == ("## 新正文", "markdown")
    assert (shown.note, shown.style, shown.children) == ("自有备注", {"bold": True}, [child])
    assert (shown.detail_page_id, shown.image_id) == (detail, image)
    assert engine.get_node(source).text == "原有正文" and engine.get_node(source).text_format == "plain"
    assert engine.resolve_page_reference(source) == PathEntry(target_page, target, "## 新正文", True)
    assert engine.page_reference_sources(target) == {source}

    for patch in ({"text": "误改"}, {"text_format": "markdown"}, {"page_reference_root_id": None}):
        with pytest.raises(MindMapError):
            engine.update_node(source, **patch)
    engine.update_node(source, note="备注仍可编辑")
    engine.set_page_reference(source, None)
    assert engine.page_document(source_page).nodes[source].text == "原有正文"
    assert engine.undo() and engine.resolve_page_reference(source).node_id == target
    assert engine.redo() and engine.resolve_page_reference(source) is None
    assert not engine.page_reference_sources(target)
    engine.document.validate()


def test_source_and_target_must_be_eligible_before_entering_history():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "入口")
    submap = engine.page_document(engine.create_submap(owner)).root_id
    detail = engine.page_document(engine.create_detail_page(owner)).root_id
    idea = engine.add_inspiration("独立想法", position=(0, 0))
    image = engine.add_image_child(engine.root_id, b"image", width=90)
    source = engine.add_child(engine.root_id, "普通文字")
    original, revision = engine.document, engine.revision
    for key, target in (
        (engine.root_id, detail), (submap, engine.root_id), (detail, submap), (idea, detail), (image, detail),
        (source, engine.root_id), (source, source), (source, idea), (source, "无效"), (source, []),
    ):
        with pytest.raises(MindMapError):
            engine.set_page_reference(key, target)
    assert engine.document == original and engine.revision == revision


@pytest.mark.parametrize("operation", ["删除子导图", "删除详情页", "删除整个分支", "转回分支"])
def test_removing_target_clears_all_surviving_references_in_the_same_history_entry(operation):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "页面入口")
    parent = engine.add_child(engine.root_id, "外部来源")
    first = engine.add_child(parent, "甲原文")
    second = engine.add_child(parent, "乙原文")
    page = engine.create_detail_page(owner) if operation == "删除详情页" else engine.create_submap(owner)
    target = engine.page_document(page).root_id
    for key in (first, second):
        engine.set_page_reference(key, target)
    if operation == "转回分支":
        # 即使根节点作为普通节点留下，原来的页面已经消失。
        engine.update_node(target, note="保留此根")
    before = engine.document
    if operation == "删除整个分支":
        engine.remove_nodes([owner])
    else:
        {"删除子导图": engine.remove_submap, "删除详情页": engine.remove_detail_page,
         "转回分支": engine.convert_submap_to_descendants}[operation](owner)

    if operation == "转回分支":
        assert engine.get_node(target).note == "保留此根"
    assert not engine.page_reference_sources(target)
    assert [engine.page_document().nodes[key].text for key in (first, second)] == ["甲原文", "乙原文"]
    assert engine.undo() and engine.document == before
    assert engine.page_reference_sources(target) == {first, second}
    assert engine.redo() and engine.resolve_page_reference(first) is None


def test_cross_page_transfer_only_clears_new_same_page_and_root_references():
    engine = MindMapEngine()
    parent = engine.add_child(engine.root_id, "子图")
    submap = engine.create_submap(parent)
    target = engine.page_document(submap).root_id
    branch = engine.add_child(engine.root_id, "将拆成独立想法")
    first = engine.add_child(branch, "甲")
    second = engine.add_child(branch, "乙")
    for key in (branch, first):
        engine.set_page_reference(key, target)
    engine.move_to_inspiration([branch], positions={branch: (0, 0)})
    assert engine.resolve_page_reference(branch) is None
    engine.set_page_reference(second, engine.root_id)
    engine.attach_inspirations([branch], target)
    assert engine.resolve_page_reference(first) is None
    assert engine.resolve_page_reference(second).node_id == engine.root_id
    assert engine.undo() and engine.resolve_page_reference(first).node_id == target
    assert engine.redo() and engine.resolve_page_reference(first) is None
    # 子导图转为后代时，原先引用主图的主题与目标合到同页。
    engine.convert_submap_to_descendants(parent)
    assert engine.resolve_page_reference(second) is None
    assert engine.undo() and engine.resolve_page_reference(second).node_id == engine.root_id
    engine.document.validate()


def test_nested_create_and_delete_prunes_staged_reference_and_rollback_keeps_index():
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "入口")
    source = engine.add_child(engine.root_id, "来源")
    before, revision = engine.document, engine.revision
    with engine.transaction("临时页面"):
        page = engine.create_submap(owner)
        target = engine.page_document(page).root_id
        engine.set_page_reference(source, target)
        engine.remove_submap(owner)
    assert engine.document == before and engine.revision == revision
    page = engine.create_submap(owner)
    target = engine.page_document(page).root_id
    engine.set_page_reference(source, target)
    with pytest.raises(MindMapError):
        with engine.transaction("回滚删除"):
            engine.remove_submap(owner)
            engine.set_page_reference(engine.root_id, target)
    assert engine.page_reference_sources(target) == {source}
    assert engine.resolve_page_reference(source).node_id == target


def test_reference_cleanup_uses_index_instead_of_scanning_nodes():
    class NoScan(dict):
        def reject(self):
            pytest.fail("局部引用事务不能遍历整张节点表")

        __iter__ = keys = values = items = reject

    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "入口")
    source = engine.add_child(engine.root_id, "来源")
    target = engine.page_document(engine.create_detail_page(owner)).root_id
    engine.set_page_reference(source, target)
    engine._state.document.nodes = NoScan(engine._state.document.nodes)
    engine.update_node(target, text="新正文")
    engine.remove_detail_page(owner)
    assert engine.resolve_page_reference(source) is None
    assert engine.undo() and engine.page_reference_sources(target) == {source}
