"""详情页入口、视图恢复、受限粘贴及页面隔离的真实 Qt 交互验收。"""

import pytest
from mindmap_core import MindMapEngine, MindMapError, Theme
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_panels import MindMapPanels
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QMessageBox


@pytest.fixture
def details(qtbot):
    engine = MindMapEngine()
    engine.update_node(engine.root_id, text="产品规划")
    branch = engine.add_child(engine.root_id, "用户系统")
    leaf = engine.add_child(branch, "权限管理")
    canvas = MindMapWidget(engine, toolbar=False)
    panels = MindMapPanels(canvas)
    qtbot.addWidget(canvas)
    canvas.resize(1000, 650)
    canvas.show()
    QApplication.processEvents()
    errors = []
    canvas.error_occurred.connect(errors.append)
    yield canvas, panels, branch, leaf, errors
    canvas.finish_edit(commit=False)
    panels.dispose()
    canvas.dispose()


def test_nonleaf_detail_navigation_restores_views_and_scopes_panels_and_export(details, tmp_path):
    canvas, panels, branch, _, errors = details
    canvas.select_nodes([branch])
    canvas.view.zoom(1.3)
    canvas.view.update_navigation_rect(center=QPointF(180, 90))
    center = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
    page = canvas.create_detail_page()
    QApplication.processEvents()
    root = canvas.document_view.root_id
    assert canvas.current_page_kind == "detail" and canvas.view.transform().m11() == 1
    assert canvas.path_navigation.full_path == "产品规划 › 用户系统 · 详情"
    child = canvas.engine.add_child(root, "详情专属内容")
    canvas.engine.add_child(child, "普通分支")
    assert panels.outline.tree_model.document.root_id == root
    panels.settings._commit("theme", "dark")
    panels.settings._commit("direction", "left")
    assert canvas.document_view.theme == Theme.dark()
    assert canvas.engine.document.theme != Theme.dark()
    path = tmp_path / "仅详情.svg"
    canvas.export(path)
    exported = path.read_text(encoding="utf-8")
    assert "详情专属内容" in exported and "权限管理" not in exported and " · 详情" not in exported
    canvas.view.zoom(1.5)
    canvas.select_nodes([child])
    canvas.engine.mark_saved()
    revision = canvas.engine.revision
    canvas.return_to_parent_page()
    restored = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
    assert canvas.selected_node_ids() == [branch] and canvas.view.transform().m11() == 1.3
    assert (center - restored).manhattanLength() < 3
    assert not canvas.node_items[branch].fold_control.button_rect().intersects(
        canvas.node_items[branch].detail_page_control.boundingRect())
    canvas.enter_detail_page(branch)
    assert canvas.current_page_id == page and canvas.selected_node_ids() == [child]
    assert canvas.view.transform().m11() == 1.5
    assert canvas.engine.revision == revision and not canvas.engine.is_dirty and not errors


@pytest.mark.parametrize("readonly", [False, True])
def test_coexisting_icons_enter_the_correct_page_and_details_hide_creation(details, qtbot, readonly):
    canvas, _, _, leaf, errors = details
    submap = canvas.engine.create_submap(leaf)
    detail = canvas.engine.create_detail_page(leaf)
    canvas.select_nodes([leaf])
    canvas.set_editable(not readonly)
    item = canvas.node_items[leaf]
    assert not item.detail_page_control.boundingRect().intersects(item.submap_control.boundingRect())
    assert item.detail_page_control.boundingRect().left() < item.submap_control.boundingRect().left()
    for name, page in (("submap_control", submap), ("detail_page_control", detail)):
        control = getattr(canvas.node_items[leaf], name)
        position = canvas.view.mapFromScene(control.mapToScene(control.boundingRect().center()))
        qtbot.mouseClick(canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=position)
        assert canvas.current_page_id == page
        canvas.return_to_parent_page()
    canvas.enter_detail_page(leaf)
    assert not canvas.action("create_submap").isVisible()
    assert not canvas.action("create_detail_page").isVisible()
    canvas.return_to_parent_page()
    assert canvas.action("create_submap").isVisible()
    assert canvas.action("enter_detail_page").isEnabled()
    QApplication.processEvents()
    assert not errors


def test_page_titles_history_and_input_composition_are_preserved(details):
    canvas, _, branch, _, errors = details
    canvas.select_nodes([branch])
    page = canvas.create_detail_page()
    root = canvas.document_view.root_id
    canvas.engine.update_node(root, text="账户模块")
    assert canvas.engine.get_node(branch).text == "账户模块"
    canvas.return_to_parent_page()
    canvas.engine.undo()
    assert canvas.current_page_id == page and canvas.document_view.root.text == "用户系统"
    canvas.engine.undo()
    assert canvas.current_page_id is None
    canvas.engine.redo()
    canvas.enter_detail_page(branch)
    canvas.begin_edit(root)
    canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    with pytest.raises(MindMapError, match="候选"):
        canvas.return_to_parent_page()
    assert canvas.current_page_id == page and canvas.is_composing
    canvas.finish_edit(commit=False)
    assert not errors


@pytest.mark.parametrize("accept", [False, True])
def test_pasting_owned_pages_into_detail_requires_confirmation_and_keeps_clipboard(details, monkeypatch, accept):
    canvas, _, branch, leaf, errors = details
    canvas.engine.create_detail_page(leaf)
    canvas.engine.create_submap(leaf)
    canvas.select_nodes([branch])
    page = canvas.create_detail_page()
    root = canvas.document_view.root_id
    canvas.return_to_parent_page()
    canvas.select_nodes([branch])
    canvas.copy()
    original = bytes(QApplication.clipboard().mimeData().data("application/x-python-mindmap+json"))
    canvas.enter_detail_page(branch)
    before, revision = canvas.engine.document, canvas.engine.revision
    prompts = []

    def confirm(parent, title, text, *args):
        prompts.append(text)
        return QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", confirm)
    inserted = canvas.paste()
    assert prompts and "子导图" in prompts[0] and "详情页" in prompts[0]
    assert original == bytes(QApplication.clipboard().mimeData().data("application/x-python-mindmap+json"))
    if accept:
        assert inserted and canvas.current_page_id == page
        assert all(not node.submap_id and not node.detail_page_id for node in canvas.document_view.nodes.values())
        assert canvas.engine.undo() and canvas.engine.document == before
    else:
        assert not inserted and canvas.engine.document == before and canvas.engine.revision == revision
        assert canvas.selected_node_ids() == [root]
    assert not errors


def test_deletion_confirmation_preserves_other_page_and_undo_restores_all(details, monkeypatch):
    canvas, _, branch, leaf, errors = details
    detail = canvas.engine.create_detail_page(leaf)
    submap = canvas.engine.create_submap(leaf)
    canvas.select_nodes([leaf])
    before = canvas.engine.document
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    canvas.delete_detail_page()
    assert canvas.engine.document == before
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    canvas.delete_detail_page()
    assert canvas.engine.get_node(leaf).submap_id == submap
    assert canvas.engine.undo() and canvas.engine.document == before
    canvas.select_nodes([branch])
    canvas.delete_selected()
    assert not canvas.engine.document.detail_pages and not canvas.engine.document.submaps
    assert canvas.engine.undo() and canvas.engine.document == before
    canvas.enter_detail_page(leaf)
    assert canvas.current_page_id == detail and not errors
