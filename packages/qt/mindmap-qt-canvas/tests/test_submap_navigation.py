"""子导图路径、页内编辑隔离、视图恢复与原生交互验收。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError, Theme
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.widget import VIRTUALIZATION_NODE_LIMIT
from mindmap_qt_panels import MindMapPanels
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QToolButton


@pytest.fixture
def workspace(qtbot):
    engine = MindMapEngine()
    branch = engine.add_child(engine.root_id, "基础知识")
    owner = engine.add_child(branch, "整数")
    engine.replace_document(engine.document)
    canvas = MindMapWidget(engine, toolbar=False)
    panels = MindMapPanels(canvas)
    qtbot.addWidget(canvas)
    canvas.resize(1000, 650)
    canvas.show()
    QApplication.processEvents()
    errors = []
    canvas.error_occurred.connect(errors.append)
    yield canvas, panels, branch, owner, errors
    canvas.finish_edit(commit=False)
    panels.dispose()
    canvas.dispose()


def test_nested_page_navigation_restores_zoom_center_selection_and_focus(workspace):
    canvas, panels, branch, owner, errors = workspace
    canvas.select_nodes([owner])
    canvas.view.zoom(1.3)
    canvas.view.update_navigation_rect(center=QPointF(200, 100))
    center = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
    scale = canvas.view.transform().m11()
    first = canvas.create_submap()
    QApplication.processEvents()
    assert canvas.current_page_id == first
    assert canvas.view.transform().m11() == 1
    assert set(canvas.document_view.nodes) == {canvas.document_view.root_id}
    root = canvas.document_view.root_id
    child = canvas.engine.add_child(root, "十进制")
    canvas.select_nodes([child])
    canvas.focus_node(child)
    canvas.view.zoom(1.5)
    first_scale = canvas.view.transform().m11()
    second = canvas.create_submap()
    QApplication.processEvents()
    assert canvas.current_page_id == second
    assert canvas.path_navigation.full_path == "中心主题 › 整数 › 十进制"
    canvas.engine.mark_saved()
    revision = canvas.engine.revision
    canvas.return_to_parent_page()
    assert canvas.current_page_id == first
    assert canvas.focus_id == child
    assert canvas.selected_node_ids() == [child]
    assert canvas.view.transform().m11() == first_scale
    canvas.return_to_parent_page()
    assert canvas.current_page_id is None
    assert canvas.selected_node_ids() == [owner]
    assert canvas.view.transform().m11() == scale
    restored = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
    assert (center - restored).manhattanLength() < 3 / scale
    assert canvas.engine.revision == revision and not canvas.engine.is_dirty
    assert panels.outline.tree_model.document.nodes.keys() == canvas.document_view.nodes.keys()
    assert not errors


def test_breadcrumb_click_and_global_undo_switch_to_operation_page(workspace, qtbot):
    canvas, panels, branch, owner, errors = workspace
    canvas.select_nodes([owner])
    first = canvas.create_submap()
    QApplication.processEvents()
    root = canvas.document_view.root_id
    child = canvas.engine.add_child(root, "页内节点")
    canvas.engine.update_node(root, text="同步后的入口")
    assert canvas.engine.get_node(owner).text == "同步后的入口"
    canvas.return_to_parent_page()
    canvas.engine.undo()
    assert canvas.current_page_id == first
    assert canvas.document_view.root.text == "整数"
    canvas.engine.undo()
    assert child not in canvas.document_view.nodes
    canvas.engine.undo()
    assert canvas.current_page_id is None
    assert canvas.engine.get_node(owner).submap_id is None
    canvas.engine.redo()
    canvas.enter_submap(owner)
    canvas.engine.redo()
    assert canvas.current_page_id == first and child in canvas.document_view.nodes
    QApplication.processEvents()
    buttons = canvas.path_navigation.findChildren(QToolButton)
    button = next(item for item in buttons if item.isVisible() and item.accessibleName() == "中心主题")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert canvas.current_page_id is None and canvas.selected_node_ids() == [canvas.engine.root_id]
    assert panels.outline.tree_model.document.root_id == canvas.engine.root_id
    assert not errors


@pytest.mark.parametrize("existing_page", [False, True], ids=["创建子页", "点击已有子页"])
def test_virtualized_pages_switch_after_zoom_and_scroll(qtbot, existing_page):
    """覆盖真实大图阈值：滚动到入口后切页，不能把旧布局节点用于新页图元同步。"""
    engine = MindMapEngine()
    with engine.transaction("构造超过虚拟化阈值的主导图"):
        nodes = [engine.add_child(engine.root_id, f"主图节点{index}")
                 for index in range(VIRTUALIZATION_NODE_LIMIT + 2)]
        owner = nodes[len(nodes) // 4]
        first = engine.create_submap(owner) if existing_page else None
    canvas = MindMapWidget(engine, toolbar=False)
    panels = MindMapPanels(canvas)
    qtbot.addWidget(canvas)
    errors = []
    canvas.error_occurred.connect(errors.append)
    try:
        canvas.resize(620, 750)
        canvas.show()
        QApplication.processEvents()
        assert canvas._virtualized
        canvas.view.zoom(0.7)
        canvas.select_nodes([owner])
        canvas.view.ensureVisible(canvas.node_items[owner].sceneBoundingRect().adjusted(-35, -35, 35, 35))
        main_center = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
        main_scale = canvas.view.transform().m11()
        if existing_page:
            control = canvas.node_items[owner].submap_control
            position = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
            qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
        else:
            first = canvas.create_submap()
        QApplication.processEvents()
        assert not errors
        assert canvas.current_page_id == first and not canvas._virtualized
        assert set(canvas.layout_result.nodes) == {canvas.document_view.root_id}
        assert canvas.view.transform().m11() == 1

        with engine.transaction("构造第二层大子导图"):
            child = engine.add_child(canvas.document_view.root_id, "第二层入口")
            second = engine.create_submap(child)
            second_root = engine.page_document(second).root_id
            grandchildren = [engine.add_child(second_root, f"子图节点{index}")
                             for index in range(VIRTUALIZATION_NODE_LIMIT + 2)]
        canvas.select_nodes([child])
        canvas.view.zoom(1.25)
        first_center = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
        canvas.enter_submap(child)
        QApplication.processEvents()
        assert canvas.current_page_id == second and canvas._virtualized
        canvas.view.zoom(0.8)
        canvas.select_nodes([grandchildren[-1]])
        canvas.view.ensureVisible(canvas.node_items[grandchildren[-1]])
        engine.mark_saved()
        revision = engine.revision

        canvas.return_to_parent_page()
        restored = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
        assert canvas.current_page_id == first and not canvas._virtualized
        assert canvas.selected_node_ids() == [child] and canvas.view.transform().m11() == 1.25
        assert (restored - first_center).manhattanLength() < 3 / 1.25
        canvas.return_to_parent_page()
        restored = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
        assert canvas.current_page_id is None and canvas._virtualized
        assert canvas.selected_node_ids() == [owner] and canvas.view.transform().m11() == main_scale
        assert (restored - main_center).manhattanLength() < 3 / main_scale

        canvas.enter_submap(owner)
        canvas.enter_submap(child)
        assert canvas.selected_node_ids() == [grandchildren[-1]] and canvas.view.transform().m11() == 0.8
        canvas.set_editable(False)
        # 从大子图点击主图路径，覆盖两张虚拟化页面之间的跳转与只读导航。
        button = next(button for button in canvas.path_navigation.findChildren(QToolButton)
                      if button.isVisible() and button.accessibleName() == "中心主题")
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert canvas.current_page_id is None and canvas._virtualized
        assert canvas.selected_node_ids() == [engine.root_id]
        assert panels.outline.tree_model.document.root_id == engine.root_id
        assert engine.revision == revision and not engine.is_dirty and not errors
    finally:
        panels.dispose()
        canvas.dispose()


def test_page_settings_export_clipboard_and_readonly_are_scoped(workspace, monkeypatch, tmp_path):
    canvas, panels, branch, owner, errors = workspace
    canvas.select_nodes([owner])
    page = canvas.create_submap()
    QApplication.processEvents()
    root = canvas.document_view.root_id
    canvas.engine.add_child(root, "子图专属内容")
    panels.settings._commit("theme", "dark")
    panels.settings._commit("direction", "left")
    assert canvas.document_view.theme == Theme.dark()
    assert canvas.document_view.layout.direction == "left"
    assert canvas.engine.document.theme != Theme.dark()
    assert canvas.engine.document.layout.direction == "right"
    target = tmp_path / "当前页.svg"
    canvas.export(target)
    exported = target.read_text(encoding="utf-8")
    assert "子图专属内容" in exported and "基础知识" not in exported
    canvas.return_to_parent_page()
    canvas.select_nodes([owner])
    canvas.copy()
    canvas.select_nodes([canvas.document_view.root_id])
    copied = canvas.paste()[0]
    copied_page = canvas.engine.get_node(copied).submap_id
    assert copied_page != page
    assert any(node.text == "子图专属内容" for node in canvas.engine.page_document(copied_page).nodes.values())
    canvas.set_editable(False)
    assert not canvas.action("create_submap").isEnabled()
    canvas.enter_submap(owner)
    assert canvas.current_page_id == page
    canvas.navigate_to(None, branch)
    assert canvas.current_page_id is None
    assert not errors


def test_page_icon_and_input_composition_guard(workspace, qtbot):
    canvas, panels, branch, owner, errors = workspace
    canvas.select_nodes([owner])
    page = canvas.create_submap()
    QApplication.processEvents()
    canvas.return_to_parent_page()
    control = canvas.node_items[owner].submap_control
    assert control.isVisible()
    position = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
    qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert canvas.current_page_id == page
    root = canvas.document_view.root_id
    canvas.begin_edit(root)
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    with pytest.raises(MindMapError, match="候选"):
        canvas.return_to_parent_page()
    assert canvas.current_page_id == page and canvas.is_composing
    canvas.finish_edit(commit=False)
    canvas.return_to_parent_page()
    canvas.select_nodes([branch])
    assert not canvas.action("create_submap").isEnabled()
    assert not errors


@pytest.mark.parametrize("inside_submap", [False, True], ids=["主导图", "子导图"])
def test_page_path_stays_short_for_deep_branches_and_selection(workspace, monkeypatch, inside_submap):
    """普通祖先不进入页面栏；选择、完整提示、复制与多选数量遵循同一页面路径。"""
    canvas, panels, branch, owner, errors = workspace
    expected_ids = [(None, canvas.engine.root_id)]
    expected_path = "中心主题"
    if inside_submap:
        canvas.select_nodes([owner])
        page = canvas.create_submap()
        expected_ids.append((page, canvas.document_view.root_id))
        expected_path += " › 整数"
    root = canvas.document_view.root_id
    current = root
    with canvas.engine.transaction("构造深层普通分支"):
        for index in range(16):
            current = canvas.engine.add_child(current, f"普通祖先{index}")
        sibling = canvas.engine.add_child(root, "另一分支")
    canvas.engine.mark_saved()
    revision = canvas.engine.revision
    for selection in ([], [root], [current], [current, sibling]):
        canvas.select_nodes(selection)
        QApplication.processEvents()
        bar = canvas.path_navigation
        assert [(entry.page_id, entry.node_id) for entry in bar.entries] == expected_ids
        assert all(entry.page_root for entry in bar.entries)
        assert bar.full_path == bar.toolTip() == bar.accessibleDescription() == expected_path
        assert bar.selection_count == len(selection)
        buttons = [button for button in bar.findChildren(QToolButton) if button.isVisible()]
        assert not any(button.text() == "…" for button in buttons)
        pages = [button for button in buttons if button is not bar.back_button]
        assert len(pages) == len(expected_ids) and all(button.icon().isNull() for button in pages)
        assert not bar.back_button.icon().isNull()
        assert [bool(button.property("last")) for button in pages] == [False] * (len(pages) - 1) + [True]
        badges = [label.text() for label in bar.findChildren(QLabel) if label.isVisible()]
        assert ("已选 2 项" in badges) == (len(selection) == 2)
    # 复制的是未省略的页面链，而引擎查询仍完整保留普通祖先，避免改变公开接口含义。
    menu = canvas.path_navigation._menu()
    monkeypatch.setattr(canvas.path_navigation, "_menu", lambda: menu)

    def copy_path():
        menu.actions()[0].trigger()
        menu.close()

    QTimer.singleShot(0, copy_path)
    canvas.path_navigation.customContextMenuRequested.emit(QPoint())
    assert QApplication.clipboard().text() == expected_path
    assert "普通祖先0" in [entry.text for entry in canvas.engine.node_path(current)]
    assert canvas.engine.revision == revision and not canvas.engine.is_dirty
    assert not errors


def test_same_name_page_roots_remain_distinct_and_clickable(workspace, qtbot):
    """连续从根节点下钻时，重名页面也按各自 ID 跳转，不能按标题去重。"""
    canvas, panels, branch, owner, errors = workspace
    canvas.engine.update_node(canvas.engine.root_id, text="同名页面")
    canvas.engine.update_node(owner, text="同名页面")
    canvas.select_nodes([owner])
    first = canvas.create_submap()
    first_root = canvas.document_view.root_id
    second = canvas.create_submap()
    second_root = canvas.document_view.root_id
    QApplication.processEvents()
    assert [(entry.page_id, entry.node_id) for entry in canvas.path_navigation.entries] == [
        (None, canvas.engine.root_id), (first, first_root), (second, second_root)]
    assert canvas.path_navigation.full_path == "同名页面 › 同名页面 › 同名页面"
    canvas.engine.mark_saved()
    revision = canvas.engine.revision
    buttons = [button for button in canvas.path_navigation.findChildren(QToolButton)
               if button.isVisible() and button.accessibleName() == "同名页面"]
    qtbot.mouseClick(buttons[1], Qt.MouseButton.LeftButton)
    assert canvas.current_page_id == first and canvas.selected_node_ids() == [first_root]
    canvas.enter_submap(first_root)
    assert canvas.current_page_id == second
    canvas.set_editable(False)
    buttons = [button for button in canvas.path_navigation.findChildren(QToolButton)
               if button.isVisible() and button.accessibleName() == "同名页面"]
    qtbot.mouseClick(buttons[0], Qt.MouseButton.LeftButton)
    assert canvas.current_page_id is None and canvas.selected_node_ids() == [canvas.engine.root_id]
    assert canvas.engine.revision == revision and not canvas.engine.is_dirty
    assert not errors


def test_long_page_path_menu_keeps_every_hidden_page_clickable(workspace):
    """页面链过长时仅收起页面，中间菜单的每一项都能定位该页根节点。"""
    canvas, panels, branch, owner, errors = workspace
    canvas.select_nodes([owner])
    first = canvas.create_submap()
    expected = [(None, canvas.engine.root_id, "中心主题"),
                (first, canvas.document_view.root_id, "整数")]
    for index in range(12):
        child = canvas.engine.add_child(canvas.document_view.root_id, f"很长的页面名称{index}")
        canvas.select_nodes([child])
        page = canvas.create_submap()
        expected.append((page, canvas.document_view.root_id, f"很长的页面名称{index}"))
    last = canvas.current_page_id
    canvas.engine.mark_saved()
    revision = canvas.engine.revision

    def hidden_actions():
        QApplication.processEvents()
        overflow = next(button for button in canvas.path_navigation.findChildren(QToolButton)
                        if button.isVisible() and button.text() == "…")
        return overflow.menu().actions()

    assert [(entry.page_id, entry.node_id, entry.text) for entry in canvas.path_navigation.entries] == expected
    assert canvas.path_navigation.full_path == " › ".join(text for _, _, text in expected)
    hidden = [(page, root, text) for page, root, text in expected if text in
              [action.text() for action in hidden_actions()]]
    assert hidden
    for page, root, text in hidden:
        canvas.navigate_to(last)
        action = next(action for action in hidden_actions() if action.text() == text)
        assert action.icon().isNull()
        action.trigger()
        assert canvas.current_page_id == page and canvas.selected_node_ids() == [root]
    assert canvas.engine.revision == revision and not canvas.engine.is_dirty
    assert not errors


def test_delete_submap_confirmation_preserves_entry_and_undo(workspace, monkeypatch):
    canvas, panels, branch, owner, errors = workspace
    canvas.select_nodes([owner])
    canvas.create_submap()
    QApplication.processEvents()
    root = canvas.document_view.root_id
    current = root
    for index in range(16):
        current = canvas.engine.add_child(current, f"很长的层级名称{index}")
    canvas.select_nodes([current])
    QApplication.processEvents()
    assert canvas.path_navigation.entries[-1].node_id == root
    canvas.return_to_parent_page()
    before = canvas.engine.document
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    canvas.delete_submap()
    assert canvas.engine.document == before
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    canvas.delete_submap()
    assert not canvas.engine.document.submaps
    assert canvas.engine.undo() and canvas.engine.document == before
    assert not errors


def test_drag_over_submap_owner_rejects_without_nearby_sibling_fallback(workspace):
    """指针进入入口正文时显示明确提示，不能退化成附近同级插入或修改文件。"""
    canvas, panels, branch, owner, errors = workspace
    canvas.select_nodes([owner])
    canvas.create_submap()
    QApplication.processEvents()
    canvas.return_to_parent_page()
    moving = canvas.engine.add_child(canvas.engine.root_id, "被拖动节点")
    canvas.select_nodes([moving])
    before, revision = canvas.engine.document, canvas.engine.revision
    controller = canvas.view.node_drag
    origin = canvas.node_items[moving].sceneBoundingRect().center()
    position = canvas.view.mapFromScene(canvas.node_items[owner].sceneBoundingRect().center())
    for attempt in range(2):
        assert controller.begin(origin)
        controller.update(position)
        assert controller.target is None
        assert errors == ["此节点已有子导图，请进入子导图添加内容"] * (attempt + 1)
        assert controller.finish(position) is None
    assert canvas.engine.document == before and canvas.engine.revision == revision
