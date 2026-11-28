"""以真实树视口验证收藏路径、选择联动和两种大纲之间的现场恢复。"""

from dataclasses import replace

import pytest
from mindmap_qt_editor.panels import MindMapPanels
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def visible_ids(tree):
    """从 Qt 当前展开的行读取节点标识。"""
    tree.doItemsLayout()
    index = tree.model().index(0, 0)
    visible = []
    while index.isValid():
        visible.append(tree.tree_model.node_id(index))
        index = tree.indexBelow(index)
    return visible


@pytest.mark.parametrize("snapshot", [False, True])
def test_favorite_paths_restore_full_outline_and_follow_document(qtbot, panel_host_factory, snapshot):
    host = panel_host_factory()
    engine = host.engine
    if snapshot:
        host.unsubscribe()
        host.unsubscribe = engine.subscribe(lambda event: host.changed(replace(event, changes=None)))
    root = engine.root_id
    with engine.transaction("建立大纲"):
        first = engine.add_child(root, "第一分支")
        via = engine.add_child(first, "经过该节点")
        deep = engine.add_child(via, "深层收藏")
        excluded = engine.add_child(via, "同级普通节点")
        engine.add_child(deep, "收藏节点的普通后代")
        second = engine.add_child(root, "第二分支")
        side = engine.add_child(second, "旁支收藏")
        for index in range(30):
            engine.add_child(root, f"用于检查滚动的位置{index}")
        engine.set_node_favorite(second, True)
        engine.set_node_favorite(deep, True)
    panels = MindMapPanels(host)
    tree = panels.outline
    tree.setParent(None)
    qtbot.addWidget(tree)
    tree.resize(260, 190)
    tree.show()
    tree.expand(tree.tree_model.index_for(first))
    host.select_nodes([excluded])
    tree.doItemsLayout()
    tree.verticalScrollBar().setValue(190)
    full, position = visible_ids(tree), tree.verticalScrollBar().value()
    assert position > 0 and deep not in full
    before = engine.revision
    tree.favorites_action.trigger()
    assert tree.favorites_only and tree.favorites_action.isChecked() and not tree.depth_action.isEnabled()
    assert engine.revision == before and host.selected_node_ids() == [excluded]
    assert not tree.currentIndex().isValid()
    assert visible_ids(tree) == [root, first, via, deep, second]
    assert tree.tree_model.index_for(deep).data(Qt.ItemDataRole.DecorationRole) is None
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                     pos=tree.visualRect(tree.tree_model.index_for(deep)).center())
    assert host.revealed == [deep]
    tree.collapse(tree.tree_model.index_for(via))
    engine.update_node(deep, text="正文更新")
    assert tree.tree_model.index_for(deep).data() == "正文更新"
    assert visible_ids(tree) == [root, first, via, second]

    engine.set_node_favorite(side, True)
    assert visible_ids(tree) == [root, first, via, deep, second, side]
    engine.set_node_favorite(second, False)
    assert visible_ids(tree) == [root, first, via, deep, second, side]
    engine.move_nodes([deep], second)
    assert visible_ids(tree) == [root, second, side, deep]
    assert engine.undo()
    assert visible_ids(tree) == [root, first, via, deep, second, side]
    tree.favorites_action.trigger()
    QApplication.processEvents()
    assert tree.depth_action.isEnabled()
    assert visible_ids(tree) == full and tree.verticalScrollBar().value() == position
    assert host.selected_node_ids() == [deep]
    panels.dispose()


def test_root_favorite_and_empty_state_in_read_only_view(qtbot, panel_host_factory):
    host = panel_host_factory()
    engine = host.engine
    root = engine.root_id
    engine.add_child(root, "普通分支")
    panels = MindMapPanels(host)
    tree = panels.outline
    tree.setParent(None)
    qtbot.addWidget(tree)
    tree.show()
    host.set_editable(False)
    tree.set_favorites_only(True)
    assert visible_ids(tree) == [] and tree.empty_label.isVisible()
    assert tree.empty_label.text() == "本页暂无收藏节点"
    assert tree.accessibleDescription() == tree.empty_label.text()
    engine.set_node_favorite(root, True)
    assert visible_ids(tree) == [root] and not tree.empty_label.isVisible()
    assert engine.undo()
    assert visible_ids(tree) == [] and tree.empty_label.isVisible()
    tree.set_favorites_only(False)
    assert len(visible_ids(tree)) == 2 and not tree.empty_label.isVisible()
    panels.dispose()
