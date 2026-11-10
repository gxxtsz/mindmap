"""双向转换的真实动作、页内投影、共享视图、输入保护及大图验收。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.widget import VIRTUALIZATION_NODE_LIMIT
from mindmap_qt_panels import MindMapPanels
from PySide6.QtCore import QBuffer, QIODevice, QPointF
from PySide6.QtGui import QImage, QInputMethodEvent
from PySide6.QtWidgets import QApplication, QMenu


@pytest.fixture
def workspace(qtbot):
    """建立共享引擎的可见画布，确保侧栏和编辑器在控件销毁之前解除绑定。"""
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "要转换的章节")
    first = engine.add_child(owner, "原子主题")
    deep = engine.add_child(first, "字段不变的后代")
    sibling = engine.add_child(engine.root_id, "其他章节")
    engine.replace_document(engine.document)
    widgets = []

    def create():
        canvas = MindMapWidget(engine, toolbar=False)
        canvas.shared_views = True
        panels = MindMapPanels(canvas)
        qtbot.addWidget(canvas)
        canvas.resize(1000, 650)
        canvas.show()
        QApplication.processEvents()
        errors = []
        canvas.error_occurred.connect(errors.append)
        widgets.append((canvas, panels))
        return canvas, panels, errors

    yield create, engine, owner, first, deep, sibling
    for canvas, panels in widgets:
        canvas.finish_edit(commit=False)
        panels.dispose()
        canvas.dispose()


def assert_projection(canvas, panels, expected):
    """画布与大纲必须包含同一页全部节点，不能只检查可见图元。"""
    assert set(canvas.document_view.nodes) == set(expected)
    assert set(panels.outline.tree_model.document.nodes) == set(expected)
    assert set(canvas.layout_result.nodes) <= set(expected)


def test_actions_roundtrip_restore_projection_navigation_and_history(workspace):
    create, engine, owner, first, deep, sibling = workspace
    canvas, panels, errors = create()
    before = engine.document
    canvas.select_nodes([owner])
    canvas.view.zoom(1.4)
    assert canvas.action("convert_descendants_to_submap").isEnabled()
    assert not canvas.action("convert_submap_to_descendants").isEnabled()
    canvas.action("convert_descendants_to_submap").trigger()
    page = engine.get_node(owner).submap_id
    root = engine.page_document(page).root_id
    assert canvas.current_page_id == page
    assert canvas.selected_node_ids() == [root]
    assert canvas.view.transform().m11() == 1
    assert_projection(canvas, panels, [root, first, deep])
    canvas.return_to_parent_page()
    assert_projection(canvas, panels, [engine.root_id, owner, sibling])
    assert canvas.selected_node_ids() == [owner]
    assert canvas.view.transform().m11() == 1.4
    assert canvas.action("convert_submap_to_descendants").isEnabled()
    assert not canvas.action("convert_descendants_to_submap").isEnabled()
    canvas.action("convert_submap_to_descendants").trigger()
    assert canvas.current_page_id is None and canvas.selected_node_ids() == [owner]
    assert engine.document == before
    assert_projection(canvas, panels, before.nodes)
    canvas.action("undo").trigger()
    assert_projection(canvas, panels, [engine.root_id, owner, sibling])
    canvas.enter_submap(owner)
    assert_projection(canvas, panels, [root, first, deep])
    canvas.action("redo").trigger()
    assert canvas.current_page_id is None
    assert_projection(canvas, panels, before.nodes)
    assert not errors


def test_shared_views_keep_viewports_and_removed_page_falls_back(workspace):
    create, engine, owner, first, deep, sibling = workspace
    actor, actor_panels, errors = create()
    other, other_panels, other_errors = create()
    other.select_nodes([sibling])
    other.view.zoom(1.6)
    other.view.update_navigation_rect(center=QPointF(125, 85))
    before = other.capture_page_view_state()
    actor.select_nodes([owner])
    page = actor.convert_descendants_to_submap()
    assert other.current_page_id is None and other.selected_node_ids() == [sibling]
    assert_projection(other, other_panels, [engine.root_id, owner, sibling])
    after = other.capture_page_view_state()
    assert after["zoom"] == before["zoom"]
    assert (QPointF(*after["center"]) - QPointF(*before["center"])).manhattanLength() < 3
    other.enter_submap(owner)
    root = engine.page_document(page).root_id
    other.begin_edit(deep)
    actor.return_to_parent_page()
    actor.convert_submap_to_descendants()
    assert other.current_page_id is None and other.editor is None
    assert_projection(other, other_panels, [engine.root_id, owner, first, deep, sibling])
    other.action("undo").trigger()
    other.enter_submap(owner)
    assert_projection(other, other_panels, [root, first, deep])
    assert not errors and not other_errors


def test_preserved_root_and_deeper_page_keep_paths_and_content(workspace):
    create, engine, owner, first, deep, sibling = workspace
    actor, panels, errors = create()
    actor.select_nodes([owner])
    page = actor.convert_descendants_to_submap()
    root = engine.page_document(page).root_id
    nested_owner = engine.add_child(root, "嵌套入口")
    nested = engine.create_submap(nested_owner)
    engine.update_node(root, note="不能丢失的根备注")
    observer, observer_panels, observer_errors = create()
    observer.navigate_to(nested)
    QApplication.processEvents()
    observer.view.zoom(1.3)
    actor.return_to_parent_page()
    actor.convert_submap_to_descendants()
    assert engine.get_node(owner).children == [root]
    assert_projection(actor, panels, [engine.root_id, owner, root, first, deep, sibling, nested_owner])
    assert observer.current_page_id == nested and observer.view.transform().m11() == 1.3
    assert page not in observer._page_chain
    assert [entry.page_id for entry in observer.path_navigation.entries] == [None, nested]
    observer.return_to_parent_page()
    assert observer.current_page_id is None
    assert not errors and not observer_errors


def test_selection_readonly_detail_and_composition_guards(workspace):
    create, engine, owner, first, deep, sibling = workspace
    canvas, panels, errors = create()
    canvas.select_nodes([owner, sibling])
    assert not canvas.action("convert_descendants_to_submap").isEnabled()
    with pytest.raises(MindMapError, match="一个"):
        canvas.convert_descendants_to_submap()
    canvas.select_nodes([])
    assert not canvas.action("convert_descendants_to_submap").isEnabled()
    canvas.select_nodes([owner])
    canvas.set_editable(False)
    before = engine.document
    assert not canvas.action("convert_descendants_to_submap").isEnabled()
    with pytest.raises(MindMapError):
        canvas.convert_descendants_to_submap()
    canvas.set_editable(True)
    canvas.begin_edit(owner)
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    with pytest.raises(MindMapError, match="候选"):
        canvas.convert_descendants_to_submap()
    assert engine.document == before and canvas.is_composing
    canvas.finish_edit(commit=False)
    detail = engine.create_detail_page(owner)
    detail_root = engine.page_document(detail).root_id
    engine.add_child(detail_root, "详情主题")
    canvas.navigate_to(detail)
    for name in ("convert_descendants_to_submap", "convert_submap_to_descendants"):
        assert not canvas.action(name).isVisible()
        assert not canvas.action(name).isEnabled()
    assert not errors


def test_reverse_conversion_guards_and_pending_properties(workspace):
    create, engine, owner, first, deep, sibling = workspace
    canvas, panels, errors = create()
    canvas.select_nodes([owner])
    page = canvas.convert_descendants_to_submap()
    canvas.return_to_parent_page()
    canvas.select_nodes([owner])
    before = engine.document
    canvas.set_editable(False)
    assert not canvas.action("convert_submap_to_descendants").isEnabled()
    with pytest.raises(MindMapError, match="只读"):
        canvas.convert_submap_to_descendants()
    canvas.set_editable(True)
    canvas.select_nodes([owner, sibling])
    assert not canvas.action("convert_submap_to_descendants").isEnabled()
    with pytest.raises(MindMapError, match="一个"):
        canvas.convert_submap_to_descendants()
    canvas.select_nodes([owner])
    canvas.begin_edit(owner)
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    with pytest.raises(MindMapError, match="候选"):
        canvas.convert_submap_to_descendants()
    assert engine.document == before and canvas.is_composing
    canvas.finish_edit(commit=False)
    remove_guard = canvas.register_navigation_guard(lambda: False)
    with pytest.raises(MindMapError, match="候选"):
        canvas.convert_submap_to_descendants()
    assert engine.document == before
    remove_guard()
    panels.properties.fields["style.font_size"].setValue(24)
    panels.properties.timer.stop()
    canvas.convert_submap_to_descendants()
    assert engine.get_node(owner).style["font_size"] == 24
    assert not engine.document.has_page(page)
    assert engine.undo() and engine.document.has_page(page)
    assert engine.get_node(owner).style["font_size"] == 24
    assert not errors


def test_image_node_hides_conversion_actions_without_changing_draft(workspace):
    create, engine, owner, first, deep, sibling = workspace
    canvas, panels, errors = create()
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(0xFF123456)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    key = engine.add_image_child(engine.root_id, bytes(buffer.data()), width=100)
    engine.add_child(key, "图片的普通后代")
    canvas.select_nodes([key])
    before = engine.document
    for name in ("convert_descendants_to_submap", "convert_submap_to_descendants"):
        assert not canvas.action(name).isVisible()
        assert not canvas.action(name).isEnabled()
    with pytest.raises(MindMapError, match="纯图片"):
        canvas.convert_descendants_to_submap()
    assert engine.document == before and not errors


@pytest.mark.parametrize("location", ["main", "submap", "inspiration"])
def test_conversion_of_page_roots_and_inspiration_nodes(workspace, location):
    create, engine, owner, first, deep, sibling = workspace
    canvas, panels, errors = create()
    source = None
    if location == "main":
        owner = engine.root_id
    elif location == "submap":
        source = engine.create_submap(sibling)
        owner = engine.page_document(source).root_id
        engine.add_child(owner, "子图根后代")
    else:
        source = engine.inspiration_id
        owner = engine.add_inspiration("独立想法", position=(80, 160))
        engine.add_child(owner, "想法后代")
    canvas.navigate_to(source, owner)
    before = engine.document
    page = canvas.convert_descendants_to_submap()
    assert canvas.current_page_id == page
    canvas.return_to_parent_page()
    canvas.select_nodes([owner])
    canvas.convert_submap_to_descendants()
    assert canvas.current_page_id == source and canvas.selected_node_ids() == [owner]
    assert engine.document == before
    assert_projection(canvas, panels, engine.page_document(source).nodes)
    assert not errors


def test_context_menu_exposes_both_conversion_actions(workspace, monkeypatch):
    create, engine, owner, first, deep, sibling = workspace
    canvas, panels, errors = create()
    canvas.select_nodes([owner])
    captured = []

    class InspectedMenu(QMenu):
        """保留真实 Qt 菜单构建，只跳过阻塞事件循环。"""

        def exec(self, position):
            captured.extend(self.actions())

    monkeypatch.setattr("mindmap_qt_canvas.widget.QMenu", InspectedMenu)
    canvas.context_menu(canvas.mapToGlobal(canvas.rect().center()))
    for key in ("convert_descendants_to_submap", "convert_submap_to_descendants"):
        assert canvas.action(key) in captured


def test_removed_inspiration_submap_returns_observer_to_its_actual_idea(workspace):
    """其他视图曾访问另一想法，再从收藏式导航直接进入子图，回退不能串到旧想法。"""
    create, engine, owner, first, deep, sibling = workspace
    unrelated = engine.add_inspiration("另一个想法", position=(0, 0))
    idea = engine.add_inspiration("实际所属想法", position=(200, 0))
    child = engine.add_child(idea, "原有后代")
    actor, panels, errors = create()
    actor.show_inspiration(idea)
    actor.select_nodes([idea])
    page = actor.convert_descendants_to_submap()
    observer, observer_panels, observer_errors = create()
    observer.show_inspiration(unrelated)
    observer.navigate_to(page)
    actor.return_to_parent_page()
    actor.select_nodes([idea])
    actor.convert_submap_to_descendants()
    assert observer.current_page_id == engine.inspiration_id
    assert observer.current_inspiration_root_id == idea
    assert_projection(observer, observer_panels, [idea, child])
    assert not errors and not observer_errors


def test_large_conversion_rebuilds_virtualized_members_and_outline(workspace):
    create, engine, owner, first, deep, sibling = workspace
    with engine.transaction("构造超过虚拟化阈值的转换分支"):
        for index in range(VIRTUALIZATION_NODE_LIMIT + 2):
            engine.add_child(owner, f"普通主题{index}")
    canvas, panels, errors = create()
    assert canvas._virtualized
    canvas.select_nodes([owner])
    page = canvas.convert_descendants_to_submap()
    assert canvas._virtualized
    assert_projection(canvas, panels, engine.page_document(page).nodes)
    canvas.return_to_parent_page()
    assert not canvas._virtualized
    assert_projection(canvas, panels, [engine.root_id, owner, sibling])
    canvas.convert_submap_to_descendants()
    assert canvas._virtualized
    assert_projection(canvas, panels, engine.page_document().nodes)
    assert not errors


def test_leaf_submap_operations_keep_parent_projection_incremental(workspace, monkeypatch):
    """没有存量节点跨页时只更新差量，不能因新转换功能让空页创建重建整个父页。"""
    create, engine, owner, first, deep, sibling = workspace
    canvas, panels, errors = create()
    switches = []
    original = canvas._switch_page

    def record_switch(*args, **kwargs):
        switches.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(canvas, "_switch_page", record_switch)
    page = engine.create_submap(sibling)
    engine.add_child(engine.page_document(page).root_id, "其他页面的主题")
    engine.remove_submap(sibling)
    assert not switches
    assert_projection(canvas, panels, [engine.root_id, owner, first, deep, sibling])
    assert not errors
