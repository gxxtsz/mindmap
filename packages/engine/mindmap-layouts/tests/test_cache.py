"""布局缓存回归：结果映射隔离，尺寸、分支样式和聚焦所属一级分支参与失效。"""

from mindmap_core import LayoutConfig, MindMapDocument, Node
from mindmap_layouts import TreeLayout


def test_cached_layout_does_not_expose_mutable_result_maps(monkeypatch):
    import mindmap_layouts.tree as tree_module

    document = MindMapDocument.new("根")
    child = Node("孩子")
    document.nodes[child.id] = child
    document.root.children.append(child.id)
    calls = []
    original = tree_module.route_branches

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(tree_module, "route_branches", counted)
    layout = TreeLayout()
    first = layout.layout(document)
    first.nodes.clear()
    first.branches.clear()
    second = layout.layout(document)
    assert len(calls) == 1 and child.id in second.nodes and child.id in second.branches
    third = layout.layout(document, {child.id: (160, 80)})
    assert len(calls) == 2 and third.nodes[child.id].width == 160
    document.root.style["branch"] = {"kind": "line"}
    layout.layout(document, {child.id: (160, 80)})
    assert len(calls) == 3


def test_focus_cache_changes_color_after_moving_to_another_root_branch():
    document = MindMapDocument.new("根")
    document.layout = LayoutConfig(direction="right")
    a, b, focus, child = [Node(text) for text in ("一级甲", "一级乙", "聚焦", "后代")]
    document.nodes.update({node.id: node for node in (a, b, focus, child)})
    document.root.children = [a.id, b.id]
    a.children = [focus.id]
    focus.children = [child.id]
    layout = TreeLayout()
    first = layout.layout(document, focus_id=focus.id)
    a.children.clear()
    b.children.append(focus.id)
    second = layout.layout(document, focus_id=focus.id)
    assert first.nodes == second.nodes
    assert first.branches[child.id].color != second.branches[child.id].color
