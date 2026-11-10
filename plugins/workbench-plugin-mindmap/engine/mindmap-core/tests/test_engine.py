"""核心行为测试，关注数据不变量和操作序列，而不是内部实现细节。"""

from copy import deepcopy

import pytest
from mindmap_core import (
    LayoutConfig,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Node,
    OperationRejected,
    Theme,
)


@pytest.fixture
def tree():
    engine = MindMapEngine()
    engine.set_layout(LayoutConfig(direction="right"))
    a = engine.add_child(engine.root_id, "分支甲")
    b = engine.add_child(engine.root_id, "分支乙")
    x = engine.add_child(a, "第一项")
    y = engine.add_child(a, "第二项")
    engine.replace_document(engine.document)
    return engine, a, b, x, y


def test_snapshots_cannot_bypass_history(tree):
    engine, a, *_ = tree
    snapshot = engine.document
    snapshot.nodes[a].text = "篡改"
    engine.get_node(a).children.clear()
    assert engine.get_node(a).text == "分支甲"
    assert len(engine.get_node(a).children) == 2
    assert not engine.can_undo


def test_node_copy_keeps_flat_aliases_and_nested_cycles():
    """扁平容器快速复制仍保持跨字段别名、对象状态回指及嵌套自引用。"""
    node = Node("容器复制", children=["甲", "乙"])
    node.alias = node.children
    node.metadata["对象状态"] = node.__dict__
    cycle = []
    cycle.append(cycle)
    node.metadata["循环"] = cycle
    copied = deepcopy(node)
    assert copied.children is copied.alias and copied.children is not node.children
    assert copied.metadata["对象状态"] is copied.__dict__
    assert copied.metadata["循环"][0] is copied.metadata["循环"]
    copied.children.append("丙")
    assert node.children == ["甲", "乙"]


def test_adopt_document_transfers_exclusive_input_without_copy(monkeypatch):
    """受信任读取结果可以转移所有权，结构检查及干净历史仍与普通替换一致。"""
    engine = MindMapEngine()
    document = MindMapDocument.new("独占读取结果")

    def reject_copy(*args):
        raise AssertionError("所有权转移不应复制节点")

    with monkeypatch.context() as patch:
        patch.setattr(Node, "__deepcopy__", reject_copy)
        engine.adopt_document(document)
    assert engine.get_node(engine.root_id).text == "独占读取结果"
    assert not engine.can_undo and not engine.is_dirty
    invalid = MindMapDocument.new("损坏的来源")
    invalid.root.children.append("不存在")
    before, revision = engine.document, engine.revision
    with pytest.raises(MindMapError):
        engine.adopt_document(invalid)
    assert engine.document == before and engine.revision == revision


def test_node_copy_preserves_aliases_and_extended_slot_fields():
    """快照加速仍要隔离嵌套内容，保留同一副本内的共享引用及扩展字段。"""
    class ExtendedNode(Node):
        __slots__ = ("extra",)

    node = ExtendedNode("扩展节点")
    shared = {"嵌套列表": [1, 2]}
    node.style["额外数据"] = shared
    node.metadata["同一对象"] = shared
    node.extra = {"扩展内容": [3]}
    copied = deepcopy(node)
    assert copied.style["额外数据"] is copied.metadata["同一对象"]
    assert copied.style["额外数据"] is not shared
    copied.metadata["同一对象"]["嵌套列表"].append(9)
    copied.extra["扩展内容"].append(4)
    assert shared["嵌套列表"] == [1, 2] and node.extra["扩展内容"] == [3]
    container = deepcopy({"先复制的状态": node.__dict__, "节点": node})
    assert container["先复制的状态"] is container["节点"].__dict__


def test_node_count_follows_history_without_exposing_document(tree):
    engine, a, *_ = tree
    before = engine.node_count
    engine.remove_nodes([a])
    assert engine.node_count == before - 3
    assert engine.undo() and engine.node_count == before


def test_insert_move_and_exact_history(tree):
    engine, a, b, x, y = tree
    before = engine.document
    sibling = engine.add_sibling(x, "中间项")
    assert engine.get_node(a).children == [x, sibling, y]
    parent = engine.insert_parent(y)
    engine.move_nodes([x, sibling], b)
    assert engine.get_node(b).children == [x, sibling]
    assert engine.get_node(parent).children == [y]
    after = engine.document
    for _ in range(3):
        assert engine.undo()
    assert engine.document == before
    for _ in range(3):
        assert engine.redo()
    assert engine.document == after


@pytest.mark.parametrize("operation", ["remove", "move", "sibling", "parent"])
def test_root_constraints(tree, operation):
    engine, a, *_ = tree
    before = engine.document
    with pytest.raises(MindMapError):
        if operation == "remove":
            engine.remove_nodes([engine.root_id])
        elif operation == "move":
            engine.move_nodes([engine.root_id], a)
        elif operation == "sibling":
            engine.add_sibling(engine.root_id)
        else:
            engine.insert_parent(engine.root_id)
    assert engine.document == before
    assert not engine.can_undo


def test_cycle_and_batch_normalization(tree):
    engine, a, b, x, y = tree
    before = engine.document
    with pytest.raises(MindMapError):
        engine.move_nodes([a], x)
    assert engine.document == before
    engine.move_nodes([a, x, y], b)
    assert engine.get_node(b).children == [a]
    assert engine.get_node(a).children == [x, y]
    engine.undo()
    assert engine.document == before


def test_transaction_rollback_even_when_inner_failure_caught(tree):
    engine, a, *_ = tree
    before = engine.document
    with pytest.raises(MindMapError):
        with engine.transaction("一组修改"):
            engine.update_node(a, text="临时")
            try:
                engine.remove_nodes([engine.root_id])
            except MindMapError:
                pass
    assert engine.document == before
    assert not engine.can_undo


def test_guard_and_transaction_history(tree):
    engine, a, b, *_ = tree
    seen = []
    unsubscribe = engine.subscribe(seen.append)
    engine.before_operation = lambda name, args: name != "remove_nodes"
    with pytest.raises(OperationRejected):
        engine.remove_nodes([a])
    assert not engine.can_undo and not seen
    with engine.transaction("批量改名"):
        engine.update_node(a, text="甲")
        engine.update_node(b, text="乙")
    assert len(seen) == 1 and engine.undo_label == "批量改名"
    engine.undo()
    assert engine.get_node(a).text == "分支甲"
    unsubscribe()
    engine.redo()
    assert len(seen) == 2


@pytest.mark.parametrize("operation", ["order", "paste", "copy", "undo", "redo", "replace", "save"])
def test_all_mutating_failures_abort_outer_transaction(tree, operation):
    engine, a, *_ = tree
    before = engine.document
    invalid = MindMapDocument.new()
    invalid.root.children.append("缺失节点")
    operations = {
        "order": lambda: engine.move_up(engine.root_id),
        "paste": lambda: engine.paste_fragment(a, invalid),
        "copy": lambda: engine.copy_nodes(["缺失节点"], a),
        "undo": engine.undo,
        "redo": engine.redo,
        "replace": lambda: engine.replace_document(before),
        "save": engine.mark_saved,
    }
    with pytest.raises(MindMapError, match="整体回滚"):
        with engine.transaction():
            engine.update_node(a, text="尚未提交")
            try:
                operations[operation]()
            except MindMapError:
                pass
    assert engine.document == before
    assert not engine.can_undo and not engine.is_dirty


def test_dirty_tokens_branching_and_capacity():
    engine = MindMapEngine(history_limit=2)
    a = engine.add_child(engine.root_id, "甲")
    engine.mark_saved()
    engine.update_node(a, text="乙")
    assert engine.is_dirty
    engine.undo()
    assert not engine.is_dirty
    engine.update_node(a, text="丙")
    assert not engine.can_redo
    engine.update_node(a, text="丁")
    assert engine.undo() and engine.undo()
    assert not engine.undo()
    assert not engine.is_dirty


def test_deleted_subtree_assets_and_move_restore_with_history(tree):
    """删除子树清理独占图片，保留仍被其他节点引用的图片；撤销重做恢复完整状态。"""
    engine, a, b, x, y = tree
    unique = engine.attach_image(x, b"unique")
    shared = engine.attach_image(y, b"shared")
    engine.attach_image(b, b"shared")
    before = engine.document
    engine.remove_nodes([a])
    deleted = engine.document
    assert not {a, x, y} & deleted.nodes.keys()
    assert unique not in deleted.assets and shared in deleted.assets
    engine.undo()
    assert engine.document == before
    engine.redo()
    assert engine.document == deleted
    engine.undo()
    engine.move_nodes([y], b)
    moved = engine.document
    assert engine.get_node(b).children == [y]
    assert engine.document.assets == before.assets
    engine.undo()
    assert engine.document == before
    engine.redo()
    assert engine.document == moved


def test_copy_remaps_complete_subgraph_and_assets(tree):
    engine, a, b, x, y = tree
    asset = engine.attach_image(x, "图片字节".encode())
    engine.attach_image(y, "图片字节".encode())
    before = engine.document
    copied = engine.copy_nodes([a, x], b)
    assert len(copied) == 1
    first, second = engine.get_node(copied[0]).children
    doc = engine.document
    assert first not in before.nodes and second not in before.nodes
    assert len(doc.assets) == 1 and doc.nodes[first].image_id == asset
    assert doc.nodes[second].image_id == asset
    assert doc.nodes[first].text == before.nodes[x].text
    assert doc.nodes[second].text == before.nodes[y].text
    assert doc.nodes[copied[0]].children == [first, second]
    engine.undo()
    assert engine.document == before
    engine.redo()
    assert engine.document == doc


def test_copy_partial_branch_preserves_direction(tree):
    engine, a, b, x, y = tree
    engine.set_layout(LayoutConfig(direction="both"))
    fragment = engine.copy_fragment([x, y])
    fragment.validate()
    copied = engine.paste_fragment(b, fragment)
    assert len(copied) == 2
    side = engine.document.branch_sides()[a]
    assert all(fragment.nodes[key].direction == side for key in (x, y))
    assert all(engine.get_node(key).direction == side for key in copied)
    assert [engine.get_node(key).text for key in copied] == [engine.get_node(key).text for key in (x, y)]


def test_resources_cleanup_reversible(tree):
    engine, a, *_ = tree
    resource = engine.attach_image(a, "同一图片".encode())
    engine.update_node(a, image_id=None)
    assert not engine.document.assets
    engine.undo()
    assert engine.document.assets[resource].data == "同一图片".encode()


@pytest.mark.parametrize(
    "patch",
    [
        {"expanded": "否"},
        {"text": 42},
        {"style": {"width": -5}},
        {"metadata": {"value": float("nan")}},
        {"image_id": "缺失"},
    ],
)
def test_invalid_edits_are_atomic(tree, patch):
    engine, a, *_ = tree
    before = engine.document
    with pytest.raises(MindMapError):
        engine.update_node(a, **patch)
    assert engine.document == before


def test_document_replacement_failure_preserves_history(tree):
    engine, a, *_ = tree
    engine.set_theme(Theme.dark())
    before = engine.document
    invalid = deepcopy(before)
    invalid.nodes[a].children.append(invalid.root_id)
    with pytest.raises(MindMapError):
        engine.replace_document(invalid)
    assert engine.document == before and engine.can_undo


def test_deep_tree_model_no_recursion():
    document = MindMapDocument.new()
    current = document.root
    for i in range(1500):
        node = Node(str(i))
        document.nodes[node.id] = node
        current.children.append(node.id)
        current = node
    document.validate()
    assert len(document.descendants(document.root_id)) == 1501
