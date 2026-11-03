"""独立收藏树的层级、原生展开和导航、动态路径、只读状态及订阅释放。"""

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_panels import SubmapFavoritesPanel
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication


@pytest.fixture
def favorite_panel(qtbot):
    engine = MindMapEngine()
    owner = engine.add_child(engine.root_id, "同名页面")
    first = engine.create_submap(owner)
    nested_owner = engine.add_child(engine.page_document(first).root_id, "同名页面")
    second = engine.create_submap(nested_owner)
    panel = SubmapFavoritesPanel(engine)
    qtbot.addWidget(panel)
    panel.resize(230, 280)
    panel.show()
    yield engine, panel, owner, first, second
    panel.dispose()


def child_pages(model, page_id=None):
    """读取 Qt 模型中一个父级的实际孩子，不根据路径文字反算测试期望。"""
    parent = model.index_for(page_id)
    return [model.page_id(model.index(row, 0, parent)) for row in range(model.rowCount(parent))]


def visible_pages(panel):
    """从视图逐行访问当前真正可见的页面，覆盖折叠后的布局行为。"""
    panel.tree_view.doItemsLayout()
    index, pages = panel.model.index(0, 0), []
    while index.isValid():
        pages.append(panel.model.page_id(index))
        index = panel.tree_view.indexBelow(index)
    return pages


def test_empty_and_populated_tree_emit_requests_without_mutating_engine(favorite_panel, qtbot, qtmodeltester):
    engine, panel, owner, first, second = favorite_panel
    qtmodeltester.check(panel.model)
    assert panel.empty_label.isVisible()
    for page in (first, second):
        engine.set_submap_favorite(page, True)
    engine.mark_saved()
    before, revision = engine.document, engine.revision
    assert panel.model.rowCount() == 1 and panel.tree_view.isVisible()
    assert child_pages(panel.model) == [first]
    assert child_pages(panel.model, first) == [second]
    assert panel.model.parent(panel.model.index_for(second)) == panel.model.index_for(first)
    assert not panel.model.parent(panel.model.index_for(first)).isValid()
    assert not panel.model.index(-1, 0).isValid() and not panel.model.index(1, 0).isValid()
    assert not panel.model.index(0, 1).isValid() and not panel.model.parent(QModelIndex()).isValid()
    assert visible_pages(panel) == [first, second]
    assert [row[0].page_id for row in panel.model.rows] == [first, second]
    for role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole):
        assert panel.model.index_for(second).data(role) == "同名页面\n中心主题 › 同名页面 › 同名页面"
    requested = []
    panel.navigate_requested.connect(requested.append)
    panel.set_current_page_id(second)
    QApplication.processEvents()
    index = panel.model.index_for(first)
    qtbot.mouseClick(panel.tree_view.viewport(), Qt.MouseButton.LeftButton,
                     pos=panel.tree_view.visualRect(index).center())
    assert requested == [first]
    panel.tree_view.setFocus()
    qtbot.keyClick(panel.tree_view, Qt.Key.Key_Return)
    assert requested == [first, first]
    assert engine.document == before and engine.revision == revision


def test_names_paths_and_current_page_stay_synchronized(favorite_panel):
    engine, panel, owner, first, second = favorite_panel
    engine.set_submap_favorite(second, True)
    panel.set_current_page_id(second)
    persistent = QPersistentModelIndex(panel.model.index_for(second))
    resets, updates = [], []
    panel.model.modelReset.connect(lambda: resets.append(True))
    panel.model.dataChanged.connect(lambda *args: updates.append(True))
    engine.update_node(owner, text="改名后的父页")
    assert panel.model.rows[0][1] == "中心主题 › 改名后的父页 › 同名页面"
    assert persistent.isValid() and not resets and updates
    assert panel.tree_view.currentIndex() == panel.model.index_for(second)
    engine.remove_submap(owner)
    assert panel.empty_label.isVisible() and panel.model.rowCount() == 0
    engine.undo()
    assert panel.model.rowCount() == 1 and panel.model.rows[0][0].page_id == second
    panel.set_current_page_id(None)
    assert not panel.tree_view.currentIndex().isValid()


def test_tree_skips_unfavorited_ancestors_and_reparents_after_unfavorite(favorite_panel, qtmodeltester):
    """同名页面始终按 ID 区分，取消收藏只移除该项，不丢失仍收藏的后代。"""
    engine, panel, owner, first, second = favorite_panel
    qtmodeltester.check(panel.model)
    group = engine.add_child(engine.page_document(second).root_id, "普通分组")
    deep = engine.create_submap(engine.add_child(group, "同名页面"))
    sibling = engine.create_submap(engine.add_child(engine.page_document(first).root_id, "同名页面"))
    top = engine.create_submap(engine.add_child(engine.root_id, "同名页面"))
    for page in (top, sibling, deep, first):
        engine.set_submap_favorite(page, True)
    assert child_pages(panel.model) == [first, top]
    assert child_pages(panel.model, first) == [deep, sibling]
    assert not panel.model.index_for(second).isValid()
    assert visible_pages(panel) == [first, deep, sibling, top]

    engine.set_submap_favorite(first, False)
    assert child_pages(panel.model) == [deep, sibling, top]
    engine.set_submap_favorite(second, True)
    assert child_pages(panel.model) == [second, sibling, top]
    assert child_pages(panel.model, second) == [deep]
    engine.set_submap_favorite(first, True)
    assert child_pages(panel.model) == [first, top]
    assert child_pages(panel.model, first) == [second, sibling]
    engine.set_submap_favorite(second, False)
    assert child_pages(panel.model, first) == [deep, sibling]

    panel.set_current_page_id(deep)
    engine.remove_nodes([group])
    assert child_pages(panel.model, first) == [sibling]
    assert not panel.tree_view.currentIndex().isValid()
    assert engine.undo()
    assert child_pages(panel.model, first) == [deep, sibling]
    assert panel.tree_view.currentIndex() == panel.model.index_for(deep)
    assert engine.redo()
    assert not panel.model.index_for(deep).isValid()


def test_branch_order_tracks_moves_and_undo_with_unfavorited_parent(favorite_panel):
    """未收藏祖先下的收藏项提升到顶层后，重排、撤销和重做仍使用原分支顺序。"""
    engine, panel, owner, first, second = favorite_panel
    sibling_owner = engine.add_child(engine.root_id, "同名页面")
    sibling = engine.create_submap(sibling_owner)
    for page in (second, sibling):
        engine.set_submap_favorite(page, True)
    panel.set_current_page_id(second)
    assert child_pages(panel.model) == [second, sibling]
    engine.move_nodes([owner], sibling_owner, "after")
    assert child_pages(panel.model) == [sibling, second]
    assert panel.tree_view.currentIndex() == panel.model.index_for(second)
    assert engine.undo()
    assert child_pages(panel.model) == [second, sibling]
    assert engine.redo()
    assert child_pages(panel.model) == [sibling, second]


@pytest.mark.parametrize("rtl", [False, True])
def test_native_expansion_and_hidden_current_page_never_navigate(favorite_panel, qtbot, rtl):
    """鼠标分支标记与左右键只展开；同步隐藏当前项、改名和刷新均不展开祖先。"""
    engine, panel, owner, first, second = favorite_panel
    for page in (first, second):
        engine.set_submap_favorite(page, True)
    tree, model = panel.tree_view, panel.model
    tree.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
    tree.setFocus()
    QApplication.processEvents()
    requested = []
    panel.navigate_requested.connect(requested.append)
    before = engine.document, engine.revision, engine.is_dirty, engine.undo_label
    index = model.index_for(first)
    rect = tree.visualRect(index)
    marker = QPoint(rect.right() + 1 + tree.indentation() // 2 if rtl
                    else rect.left() - tree.indentation() // 2, rect.center().y())
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=marker)
    assert not tree.isExpanded(index) and visible_pages(panel) == [first]
    tree.setCurrentIndex(index)
    qtbot.keyClick(tree, Qt.Key.Key_Left if rtl else Qt.Key.Key_Right)
    assert tree.isExpanded(index)
    qtbot.keyClick(tree, Qt.Key.Key_Right if rtl else Qt.Key.Key_Left)
    assert not tree.isExpanded(index)
    panel.set_current_page_id(second)
    QApplication.processEvents()
    assert tree.currentIndex() == model.index_for(second)
    assert not tree.isExpanded(index) and visible_pages(panel) == [first]
    assert not requested
    assert before == (engine.document, engine.revision, engine.is_dirty, engine.undo_label)

    engine.update_node(owner, text="父页改名")
    panel.set_ui_colors(ui_colors(Theme.dark()))
    third = engine.create_submap(engine.add_child(engine.page_document(second).root_id, "新的嵌套收藏"))
    engine.set_submap_favorite(third, True)
    assert not tree.isExpanded(model.index_for(first))
    assert tree.isExpanded(model.index_for(second))
    assert visible_pages(panel) == [first] and not requested
    assert engine.undo()
    assert not tree.isExpanded(model.index_for(first))
    assert engine.redo()
    assert not tree.isExpanded(model.index_for(first)) and tree.isExpanded(model.index_for(second))


def test_refresh_preserves_scroll_and_document_reset_expands_new_tree(favorite_panel):
    """普通编辑保留滚动及收起状态，整份文档替换则按首次展示初始化。"""
    engine, panel, owner, first, second = favorite_panel
    with engine.transaction("准备可滚动收藏树"):
        for page in (first, second):
            engine.set_submap_favorite(page, True)
        for index in range(16):
            page = engine.create_submap(engine.add_child(engine.root_id, f"其他页面 {index}"))
            engine.set_submap_favorite(page, True)
    tree = panel.tree_view
    tree.collapse(panel.model.index_for(first))
    QApplication.processEvents()
    # 以实际滚动范围的中段为基线，单行行高变化后仍验证保持位置，而非误测
    # 删除末尾行导致最大值缩小时 Qt 必须执行的边界裁剪。
    tree.verticalScrollBar().setValue(tree.verticalScrollBar().maximum() // 2)
    scroll = tree.verticalScrollBar().value()
    assert scroll > 0
    panel.set_current_page_id(second)
    engine.update_node(owner, text="改名后保持滚动")
    assert tree.verticalScrollBar().value() == scroll
    assert not tree.isExpanded(panel.model.index_for(first))
    # 收藏项移除使树重置，原有分支及滚动位置仍保持。
    engine.set_submap_favorite(page, False)
    assert tree.verticalScrollBar().value() == min(scroll, tree.verticalScrollBar().maximum())
    assert not tree.isExpanded(panel.model.index_for(first))
    engine.replace_document(engine.document)
    assert tree.isExpanded(panel.model.index_for(first))
    assert tree.verticalScrollBar().value() == 0


def test_nested_context_menu_uses_hit_page_and_rejects_stale_target(favorite_panel):
    """嵌套项右键按实际命中 ID 操作；菜单不导航，修改后清除迟到目标。"""
    engine, panel, owner, first, second = favorite_panel
    for page in (first, second):
        engine.set_submap_favorite(page, True)
    panel.set_current_page_id(first)
    removed, navigated = [], []
    panel.unfavorite_requested.connect(removed.append)
    panel.navigate_requested.connect(navigated.append)
    position = panel.tree_view.visualRect(panel.model.index_for(second)).center()
    panel.tree_view.customContextMenuRequested.emit(position)
    assert panel.remove_action.data() == second and not navigated
    assert panel.tree_view.currentIndex() == panel.model.index_for(first)
    engine.mark_saved()
    assert panel.remove_action.data() == second
    panel.remove_action.trigger()
    assert removed == [second] and engine.is_submap_favorite(second)
    engine.update_node(owner, text="菜单期间修改父页面")
    assert panel.remove_action.data() is None and not panel._menu.isVisible()
    panel.remove_action.trigger()
    assert removed == [second]
    panel.set_editable(False)
    panel.tree_view.customContextMenuRequested.emit(position)
    assert panel.remove_action.data() == second and not panel.remove_action.isEnabled()
    panel.remove_action.trigger()
    assert removed == [second] and not navigated


@pytest.mark.parametrize("theme", [Theme(), Theme.dark()])
def test_tree_keeps_single_line_geometry_and_selection_out_of_branch_area(favorite_panel, theme):
    """检查真实绘制结果，防止 Qt 将选中底色另画成缩进区的一块直角色块。"""
    engine, panel, owner, first, second = favorite_panel
    for page in (first, second):
        engine.set_submap_favorite(page, True)
    colors = ui_colors(theme)
    panel.set_ui_colors(colors)
    panel.set_current_page_id(second)
    QApplication.processEvents()
    tree = panel.tree_view
    rect = tree.visualRect(panel.model.index_for(second))
    image = tree.viewport().grab().toImage()
    ratio = image.devicePixelRatio()

    def color_at(x, y):
        """把 Qt 逻辑坐标转换成实际设备像素，兼容系统高 DPI 缩放。"""
        return image.pixelColor(round(x * ratio), round(y * ratio))

    assert rect.height() == 32 and tree.indentation() == 16
    assert color_at(rect.left() - 8, rect.top() + 4) == QColor(colors.surface)
    assert color_at(rect.left() + 50, rect.top() + 4) == QColor(colors.selected)
    root_rect = tree.visualRect(panel.model.index_for(first))
    marker_x = root_rect.left() - tree.indentation() // 2
    marker_y = root_rect.top() + root_rect.height() // 2
    assert any(color_at(marker_x + dx, marker_y + dy) != QColor(colors.surface)
               for dx in range(-3, 4) for dy in range(-3, 4))


def test_readonly_and_dispose_reject_stale_menu_callbacks(favorite_panel):
    engine, panel, owner, first, second = favorite_panel
    engine.set_submap_favorite(first, True)
    requested = []
    panel.unfavorite_requested.connect(requested.append)
    panel.remove_action.setData(first)
    panel.set_editable(False)
    panel.remove_action.trigger()
    assert not requested and not panel.remove_action.isEnabled()
    panel.set_editable(True)
    panel.remove_action.trigger()
    assert requested == [first] and engine.is_submap_favorite(first)
    panel.close_popups()
    panel.remove_action.trigger()
    assert requested == [first]
    panel.set_ui_colors(ui_colors(Theme.dark()))
    panel.dispose()
    panel.dispose()
    engine.set_submap_favorite(second, True)
    assert panel.model.rowCount() == 1 and len(panel.model.rows) == 1
    assert not panel.model.index_for(second).isValid()
    panel.remove_action.setData(second)
    panel.remove_action.trigger()
    assert requested == [first]
