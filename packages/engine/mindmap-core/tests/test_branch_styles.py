"""分支设置的作用范围、历史、结构操作与公共几何契约。"""

from copy import deepcopy

import pytest
from mindmap_core import (
    BranchPath,
    BranchStyle,
    Curve,
    LayoutInfo,
    LayoutRegistry,
    LayoutResult,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    OperationRejected,
    PathCommand,
    Rect,
    apply_branch_style,
)


def tree():
    engine = MindMapEngine()
    # 本组验证旧文档的局部作用范围，显式使用未配置分支的原始曲线。
    engine.set_branch_style([engine.root_id], None)
    first = engine.add_child(engine.root_id, "分支甲")
    second = engine.add_child(engine.root_id, "分支乙")
    leaf = engine.add_child(first, "末级")
    return engine, first, second, leaf


def test_new_document_defaults_to_round_and_new_nodes_inherit_without_initial_history():
    """新文档从创建起使用圆角正交；多级新增及同级、父级操作保持继承规则。"""
    document = MindMapDocument.new()
    engine = MindMapEngine(document)
    assert document.root.style["branch"] == {"kind": "round"}
    assert not engine.can_undo and not engine.is_dirty and engine.revision == 0
    child = engine.add_child(engine.root_id)
    leaf = engine.add_child(child)
    sibling = engine.add_sibling(child)
    parent = engine.insert_parent(leaf)
    assert all(engine.get_node(key).style["branch"] == {"kind": "round"}
               for key in (engine.root_id, child, leaf, sibling, parent))
    engine.set_branch_style([child], "line", scope="level")
    assert engine.get_node(engine.root_id).style["branch"] == {"kind": "round"}
    assert MindMapDocument.new().root.style["branch"] == {"kind": "round"}


def test_subtree_scope_history_and_hidden_nodes():
    engine, first, second, leaf = tree()
    engine.set_expanded(first, False)
    engine.update_node(first, style={"font_size": 18, "自定义": "保留"})
    engine.mark_saved()
    before, revision = engine.document, engine.revision
    engine.set_branch_style([first, leaf, first], BranchStyle.ROUND)
    after = engine.document
    assert after.nodes[first].style == {"font_size": 18, "自定义": "保留", "branch": {"kind": "round"}}
    assert after.nodes[leaf].style["branch"] == {"kind": "round"}
    assert "branch" not in after.nodes[second].style
    assert engine.revision == revision + 1 and engine.is_dirty
    assert engine.undo() and engine.document == before and not engine.is_dirty
    assert engine.redo() and engine.document == after


def test_level_scope_does_not_inherit_or_erase_descendant_overrides():
    engine, first, _, leaf = tree()
    engine.set_branch_style([leaf], "line")
    engine.set_branch_style([engine.root_id], "round", scope="level")
    assert "branch" not in engine.get_node(first).style
    assert engine.get_node(leaf).style["branch"]["kind"] == "line"
    engine.set_branch_style([engine.root_id], None, scope="level")
    assert "branch" not in engine.get_node(engine.root_id).style
    assert engine.get_node(leaf).style["branch"]["kind"] == "line"


@pytest.mark.parametrize(
    "ids,kind,scope", [(None, "unknown", "subtree"), (["missing"], "round", "level"), (None, "line", "all")]
)
def test_invalid_arguments_are_atomic(ids, kind, scope):
    engine, first, _, _ = tree()
    before, revision = engine.document, engine.revision
    with pytest.raises(MindMapError):
        engine.set_branch_style(ids or [first], kind, scope=scope)
    assert engine.document == before and engine.revision == revision


@pytest.mark.parametrize("branch", [None, "line", {}, {"kind": True}, {"kind": "unknown"}])
def test_invalid_persisted_style_rolls_back(branch):
    engine = MindMapEngine()
    before = engine.document
    with pytest.raises(MindMapError):
        engine.update_node(engine.root_id, style={"branch": branch})
    assert engine.document == before


def test_new_nodes_copy_parent_branch_and_keep_independent_data():
    engine, first, second, leaf = tree()
    engine.set_branch_style([engine.root_id], "arc", scope="level")
    engine.set_branch_style([first], "round", scope="level")
    child = engine.add_child(first)
    sibling = engine.add_sibling(first)
    parent = engine.insert_parent(leaf)
    assert engine.get_node(child).style["branch"]["kind"] == "round"
    assert engine.get_node(sibling).style["branch"]["kind"] == "arc"
    assert engine.get_node(parent).style["branch"]["kind"] == "round"
    engine.set_branch_style([child], "arrow", scope="level")
    assert engine.get_node(first).style["branch"]["kind"] == "round"
    fragment = engine.copy_fragment([first])
    pasted = engine.paste_fragment(second, fragment)[0]
    engine.move_nodes([first], second)
    assert engine.get_node(first).style["branch"] == engine.get_node(pasted).style["branch"]


def test_preview_operation_isolated_and_guard_respected():
    engine, first, _, _ = tree()
    candidate = engine.document
    original = deepcopy(candidate)
    apply_branch_style(candidate, [first], "arrow")
    assert engine.document == original and candidate != original
    engine.before_operation = lambda operation, arguments: operation != "set_branch_style"
    with pytest.raises(OperationRejected):
        engine.set_branch_style([first], "arrow")
    assert engine.document == original


class GeometryLayout:
    info = LayoutInfo("geometry", "几何测试")

    def __init__(self, branch):
        self.branch = branch

    def validate_config(self, config):
        pass

    def layout(self, document, measurements=None, **kwargs):
        child = document.root.children[0]
        return LayoutResult(
            nodes={document.root_id: Rect(0, 0, 20, 20), child: Rect(50, 0, 20, 20)},
            branches={child: self.branch},
            bounds=Rect(-20, -20, 110, 60),
        )


@pytest.mark.parametrize(
    "branch",
    [
        BranchPath((20, 10), (50, 10), (PathCommand("L", ((50, 10),)),), "#123456"),
        BranchPath((20, 10), (50, 10), (PathCommand("M", ((20, 10),)),), "#123456", float("nan")),
        BranchPath((20, 10), (50, 10), (PathCommand("M", ((float("inf"), 10),)),), "#123456"),
        BranchPath((20, 10), (50, 10), (PathCommand("M", ((20, 10),)), PathCommand("C", ())), "#123456"),
    ],
)
def test_invalid_path_contract_rejected(branch):
    engine = MindMapEngine()
    engine.add_child(engine.root_id)
    document = engine.document
    registry = LayoutRegistry()
    layout = GeometryLayout(branch)
    layout.info = LayoutInfo("tree", "几何测试")
    registry.register(layout)
    with pytest.raises(MindMapError):
        registry.layout(document)


@pytest.mark.parametrize(
    "branch",
    [
        Curve(((20, 10), (30, 10), (40, 10), (50, 10)), "#123456"),
        BranchPath(
            (20, 10), (50, 10), (PathCommand("M", ((20, 10),)), PathCommand("L", ((50, 10),))), "#123456"
        ),
    ],
)
def test_old_and_new_geometry_contracts_accepted(branch):
    engine = MindMapEngine()
    engine.add_child(engine.root_id)
    layout = GeometryLayout(branch)
    layout.info = LayoutInfo("tree", "几何测试")
    registry = LayoutRegistry()
    registry.register(layout)
    assert next(iter(registry.layout(engine.document).branches.values())) == branch
