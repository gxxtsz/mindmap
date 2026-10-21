"""功能包依赖方向、旧模块对象一致性与新增宿主接口验收。"""

import ast
import importlib
import tomllib
from pathlib import Path

import pytest
from mindmap_engine import MindMapEngine
from mindmap_qt_canvas import MindMapWidget

QT_PACKAGES = Path(__file__).resolve().parents[2]
MODULES = {
    "render": ("rendering", "exporting"),
    "ui": ("ui_style", "icons", "property_controls", "properties", "branch_styles", "dialogs", "settings_menu"),
    "canvas": ("widget", "view", "dragging", "clipboard", "items", "view_controls", "files"),
    "panels": ("outline", "inspector", "settings", "panels"),
    "workspace": ("workspace",),
}


@pytest.mark.parametrize("group,module", [(group, module) for group, modules in MODULES.items() for module in modules])
def test_original_modules_are_the_real_implementation_objects(group, module):
    """旧路径转发模块本体，宿主对模块变量或对话框入口的替换仍影响真实实现。"""
    old = importlib.import_module(f"mindmap_qt.{module}")
    actual = importlib.import_module(f"mindmap_qt_{group}.{module}")
    assert old is actual


def test_original_public_classes_preserve_identity():
    """统一入口与按需入口返回同一类型，不能通过复制定义破坏 isinstance 判断。"""
    import mindmap_qt
    from mindmap_qt_canvas import MindMapFileController
    from mindmap_qt_panels import MindMapPanels
    from mindmap_qt_ui import UiColors
    from mindmap_qt_workspace import MindMapWorkspace

    assert mindmap_qt.MindMapWidget is MindMapWidget
    assert mindmap_qt.MindMapFileController is MindMapFileController
    assert mindmap_qt.MindMapPanels is MindMapPanels
    assert mindmap_qt.MindMapWorkspace is MindMapWorkspace
    assert mindmap_qt.UiColors is UiColors


def test_capability_dependencies_are_declared_and_point_downward():
    """真实源码只引用声明的下层包，面板不得借助兼容门面反向安装画布。"""
    allowed = {
        "render": set(),
        "ui": {"render"},
        "canvas": {"render", "ui"},
        "panels": {"ui"},
        "workspace": {"canvas", "panels", "ui"},
    }
    for group, dependencies in allowed.items():
        package = QT_PACKAGES / f"mindmap-qt-{group}"
        metadata = tomllib.loads((package / "pyproject.toml").read_text("utf-8"))
        declared = {item.split(">", 1)[0] for item in metadata["project"]["dependencies"]}
        assert {f"mindmap-qt-{item}" for item in dependencies} <= declared
        for path in (package / "src").rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                           [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for module in modules:
                    top = module.split(".")[0]
                    if top.startswith("mindmap_qt_"):
                        target = top.removeprefix("mindmap_qt_")
                        assert target == group or target in dependencies, (path, target)
                    assert top not in {"mindmap_qt", "mindmap_shell", "mindmap_docking", "mindmap_workbench"}
                if group == "panels" and isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                    assert not (node.value.attr == "canvas" and node.attr.startswith("_")), (path, node.attr)


def test_public_read_view_and_navigation_preserve_history(qtbot):
    """公开查询不复制文档；大纲定位合并祖先展开，并保持一次撤销即可恢复。"""
    engine = MindMapEngine()
    parent = engine.add_child(engine.root_id, "折叠父节点")
    child = engine.add_child(parent, "需要定位的子节点")
    engine.set_expanded(parent, False)
    canvas = MindMapWidget(engine)
    qtbot.addWidget(canvas)
    try:
        assert canvas.document_view is canvas._document
        assert canvas.parent_index[child] == parent
        with pytest.raises(TypeError):
            canvas.parent_index[child] = engine.root_id
        before = engine.document
        canvas.reveal_nodes([child])
        assert canvas.selected_node_ids() == [child]
        assert engine.get_node(parent).expanded
        assert engine.undo() and engine.document == before
        canvas.set_branch_scope("level")
        assert canvas.branch_scope == "level"
        with pytest.raises(ValueError):
            canvas.set_branch_scope("invalid")
    finally:
        canvas.dispose()
