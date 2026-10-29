"""大纲层级的真实可见行、选择联动与结构变化验收。"""

from dataclasses import replace

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, Theme
from mindmap_qt_panels.outline import OutlinePanel
from PySide6.QtWidgets import QApplication


@pytest.fixture(params=["delta", "snapshot"])
def outline(qtbot, panel_host_factory, request):
    """构造超过菜单上限的深链和旁支，保证所有层确实能展示第10层之后的节点。"""
    engine = MindMapEngine(MindMapDocument.new("根主题"))
    chain = [engine.root_id]
    with engine.transaction("创建深层导图"):
        for depth in range(2, 13):
            chain.append(engine.add_child(chain[-1], f"第{depth}层"))
        sibling = engine.add_child(engine.root_id, "旁支")
    engine.mark_saved()
    canvas = panel_host_factory(engine)
    if request.param == "snapshot":
        # 灵感等宿主刷新整页时不提供差量；通过公开通知协议覆盖这一合法路径，
        # 确保层级初始化、手动展开保留与历史恢复不依赖某一种宿主更新方式。
        canvas.unsubscribe()
        canvas.unsubscribe = engine.subscribe(lambda event: canvas.changed(replace(event, changes=None)))
    qtbot.addWidget(canvas)
    tree = OutlinePanel(canvas)
    qtbot.addWidget(tree)
    tree.resize(300, 500)
    tree.show()
    QApplication.processEvents()
    yield tree, canvas, engine, chain, sibling
    canvas.dispose()


def visible_keys(tree):
    """从真实视图逐行读取，而非用被测代码的深度算法反算预期结果。"""
    tree.doItemsLayout()
    index = tree.model().index(0, 0)
    keys = []
    while index.isValid():
        keys.append(tree.tree_model.node_id(index))
        index = tree.indexBelow(index)
    return keys


@pytest.mark.parametrize("depth", [*range(1, 10), None])
def test_presets_show_exact_rows_without_editing_document(outline, depth):
    """每个预设控制真实可见行，只读时不改变修订号、修改标记和撤销历史。"""
    tree, canvas, engine, chain, sibling = outline
    canvas.set_editable(False)
    canvas.select_nodes([chain[-1]])
    before = engine.document, engine.revision, engine.is_dirty, engine.can_undo, engine.undo_label
    calls = []
    tree.nodes_selected.connect(calls.append)
    actions = tree.depth_menu.actions()
    assert [action.text() for action in actions] == ["显示所有层", *[f"显示{n}层" for n in range(1, 10)]]
    actions[0 if depth is None else depth].trigger()
    assert visible_keys(tree) == chain[:depth] + ([sibling] if depth is None or depth >= 2 else [])
    assert [action.data() for action in actions if action.isChecked()] == [depth]
    assert tree.depth_action.text() == ("所有层" if depth is None else f"{depth}层")
    assert canvas.selected_node_ids() == [chain[-1]]
    assert tree.selectionModel().isSelected(tree.tree_model.index_for(chain[-1]))
    assert before == (engine.document, engine.revision, engine.is_dirty, engine.can_undo, engine.undo_label)
    assert calls == []


def test_hidden_selection_and_refresh_never_expand_ancestors(outline):
    """隐藏节点仍保留多选和当前项，文本、主题刷新均不能隐式展开或反向导航。"""
    tree, canvas, engine, chain, sibling = outline
    canvas.select_nodes([chain[-1], sibling])
    for refresh in (
        lambda: engine.update_node(chain[-1], text="修改正文"),
        lambda: engine.set_theme(Theme.dark()),
        lambda: tree.set_theme(Theme()),
    ):
        refresh()
        QApplication.processEvents()
        assert visible_keys(tree) == [chain[0], chain[1], sibling]
        assert canvas.selected_node_ids() == [chain[-1], sibling]
        assert tree.currentIndex() == tree.tree_model.index_for(chain[-1])
        assert {tree.tree_model.node_id(index) for index in tree.selectionModel().selectedRows()} == {
            chain[-1], sibling,
        }


def test_manual_changes_survive_structure_edits_but_same_preset_reapplies(outline):
    """新增、移动、删除和历史恢复保留已有分支；原叶节点成为分支按预设初始化。"""
    tree, canvas, engine, chain, sibling = outline
    tree.set_display_depth(3)
    tree.collapse(tree.tree_model.index_for(chain[1]))
    tree.expand(tree.tree_model.index_for(chain[4]))
    leaf = engine.add_child(sibling, "旁支孩子")
    assert tree.isExpanded(tree.tree_model.index_for(sibling))
    assert not tree.isExpanded(tree.tree_model.index_for(chain[1]))
    assert tree.isExpanded(tree.tree_model.index_for(chain[4]))
    child = engine.add_child(leaf, "更深的孩子")
    assert not tree.isExpanded(tree.tree_model.index_for(leaf))
    engine.move_nodes([chain[4]], sibling)
    assert tree.isExpanded(tree.tree_model.index_for(chain[4]))
    engine.remove_nodes([child])
    assert engine.undo()
    assert not tree.isExpanded(tree.tree_model.index_for(leaf))
    assert engine.redo()
    assert tree.isExpanded(tree.tree_model.index_for(chain[4]))
    assert tree.display_depth == 3
    signals = []
    tree.display_depth_changed.connect(signals.append)
    tree.set_display_depth(3)
    assert signals == []
    assert tree.isExpanded(tree.tree_model.index_for(chain[1]))
    assert not tree.isExpanded(tree.tree_model.index_for(chain[4]))  # 移动后位于第3层，保持收起。


def test_replacement_with_identical_structure_reapplies_preset(outline):
    """结构相同也要清除手动展开；所有层预设下新增分支和文档替换都保持全展开。"""
    tree, canvas, engine, chain, sibling = outline
    tree.expandAll()
    engine.replace_document(engine.document)
    assert visible_keys(tree) == [chain[0], chain[1], sibling]
    tree.set_display_depth(None)
    leaf = engine.add_child(sibling, "所有层新增")
    engine.add_child(leaf, "继续新增")
    assert tree.isExpanded(tree.tree_model.index_for(leaf))
    tree.collapseAll()
    engine.replace_document(engine.document)
    assert len(visible_keys(tree)) == len(engine.document.nodes)


@pytest.mark.parametrize("value", [0, -1, 10, True, "2", 2.5])
def test_invalid_depth_preserves_view_and_preference(outline, value):
    """非法外部调用不能部分修改视图或发出持久化信号。"""
    tree, canvas, engine, chain, sibling = outline
    calls = []
    tree.display_depth_changed.connect(calls.append)
    with pytest.raises(ValueError):
        tree.set_display_depth(value)
    assert tree.display_depth == 2 and calls == []
    assert visible_keys(tree) == [chain[0], chain[1], sibling]
