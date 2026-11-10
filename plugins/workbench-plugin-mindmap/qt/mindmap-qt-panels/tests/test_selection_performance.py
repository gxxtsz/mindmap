"""大纲批量范围与属性快照回归；用调用次数约束复杂度，不断言机器耗时。"""

import pytest
from mindmap_core import MindMapDocument, MindMapEngine, Node
from mindmap_qt_panels.inspector import NodePropertiesPanel
from mindmap_qt_panels.outline import OutlinePanel
from PySide6.QtCore import QItemSelection, QItemSelectionModel


@pytest.mark.parametrize("wide", [False, True])
def test_ten_thousand_outline_selection_uses_one_range_commit(panel_host_factory, qtbot, monkeypatch, wide):
    document = MindMapDocument.new("批量范围")
    keys = [document.root_id]
    for index in range(1, 10000):
        node = Node("主题")
        document.nodes[keys[0 if wide else (index - 1) // 5]].children.append(node.id)
        document.nodes[node.id] = node
        keys.append(node.id)
    host = panel_host_factory(MindMapEngine(document))
    tree = OutlinePanel(host)
    qtbot.addWidget(tree)
    model = tree.selectionModel()
    original = model.select
    commits = []

    def select(selection, flags):
        assert isinstance(selection, QItemSelection)
        assert flags == QItemSelectionModel.SelectionFlag.ClearAndSelect
        commits.append(selection.count())
        return original(selection, flags)

    def forbidden(*args, **kwargs):
        raise AssertionError("反向同步不得通过 selectedRows 展开全部既有选择")

    monkeypatch.setattr(model, "select", select)
    monkeypatch.setattr(model, "selectedRows", forbidden)
    ids = list(document.ordered_nodes())
    expanded = set(tree._expanded_ids)
    host.select_nodes(ids)
    assert commits == [2 if wide else 2001]
    assert model.isSelected(tree.tree_model.index_for(keys[-1]))
    assert tree._expanded_ids == expanded
    assert tree.currentIndex() == tree.tree_model.index_for(ids[0])
    host.select_nodes(ids)
    assert len(commits) == 1
    changes = []
    tree.nodes_selected.connect(changes.append)
    original(tree.tree_model.index_for(keys[-1]), QItemSelectionModel.SelectionFlag.Deselect)
    assert changes == [[key for key in ids if key != keys[-1]]]
    original(tree.tree_model.index_for(keys[-1]), QItemSelectionModel.SelectionFlag.Select)
    assert changes[-1] == ids
    assert tree._expanded_ids == expanded


def test_properties_use_selection_snapshot_and_ignore_unrelated_content(panel_host_factory, qtbot, monkeypatch):
    host = panel_host_factory()
    root = host.engine.root_id
    first = host.engine.add_child(root, "选中主题")
    other = host.engine.add_child(root, "其他主题")
    nested = host.engine.add_child(first, "后代分支")
    panel = NodePropertiesPanel(host)
    qtbot.addWidget(panel)
    calls = []
    original = panel.load_nodes

    def record(nodes, **kwargs):
        calls.append([node.id for node in nodes])
        original(nodes, **kwargs)

    def forbidden(*args):
        raise AssertionError("选择通知已有快照，属性栏不应重新查询完整选择")

    monkeypatch.setattr(panel, "load_nodes", record)
    monkeypatch.setattr(host, "selected_node_ids", forbidden)
    host.select_nodes([first])
    assert calls == [[first]]
    host.select_nodes([first])
    host.engine.update_node(other, text="不影响选中节点")
    assert calls == [[first]]
    host.engine.update_node(first, text="应更新属性")
    assert calls == [[first], [first]]
    host.engine.set_branch_style([nested], "arc", scope="level")
    assert calls == [[first], [first], [first]]
    panel.dispose()
