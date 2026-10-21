"""工作区成员、显式依赖、公开边界及删除入口的验收。"""

import ast
import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "mindmap-core", "mindmap-layouts", "mindmap-formats", "mindmap-qt-render",
    "mindmap-qt-ui", "mindmap-qt-canvas", "mindmap-qt-panels", "mindmap-docking",
    "mindmap-shell", "mindmap-workbench",
}
REMOVED = {"mindmap_engine", "mindmap_qt", "mindmap_qt_workspace"}
DEPENDENCIES = {
    "mindmap-core": set(), "mindmap-layouts": {"mindmap-core"}, "mindmap-formats": {"mindmap-core"},
    "mindmap-qt-render": {"mindmap-core"},
    "mindmap-qt-ui": {"mindmap-core", "mindmap-layouts", "mindmap-qt-render"},
    "mindmap-qt-canvas": {"mindmap-core", "mindmap-layouts", "mindmap-formats", "mindmap-qt-render", "mindmap-qt-ui"},
    "mindmap-qt-panels": {"mindmap-core", "mindmap-qt-ui"},
    "mindmap-docking": set(), "mindmap-shell": {"mindmap-docking"},
    "mindmap-workbench": EXPECTED - {"mindmap-workbench"},
}


def members():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return {
        data["name"]: (path, data)
        for pattern in config["tool"]["uv"]["workspace"]["members"]
        for path in ROOT.glob(pattern)
        if (metadata := path / "pyproject.toml").is_file()
        for data in [tomllib.loads(metadata.read_text("utf-8"))["project"]]
    }


def test_workspace_and_lock_only_reference_current_repository():
    """十个成员全部从当前仓库解析，已删除的入口既不安装也不进入锁文件。"""
    packages = members()
    assert set(packages) == EXPECTED
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert set(config["tool"]["uv"]["sources"]) == EXPECTED - {"mindmap-workbench"}
    assert all(value == {"workspace": True} for value in config["tool"]["uv"]["sources"].values())
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    assert set(lock["manifest"]["members"]) == EXPECTED | {"mindmap"}
    installed = {p["name"]: p for p in lock["package"] if "editable" in p["source"]}
    assert set(installed) == EXPECTED
    for name, package in installed.items():
        assert (ROOT / package["source"]["editable"]).resolve() == packages[name][0].resolve()
    for module in REMOVED:
        assert importlib.util.find_spec(module) is None


def runtime_sources(source_root):
    """递归收集实际运行模块；随包交付、另有元数据的独立项目模板单独验收。"""
    templates = [metadata.parent for metadata in source_root.rglob("pyproject.toml")
                 if metadata.parent.name == "project_template"]
    return (file for file in source_root.rglob("*.py")
            if not any(file.is_relative_to(template) for template in templates))


def assert_source_boundary(file, name, declared):
    """同时检查依赖声明、跨包私有模块和已明确收束的实现细节访问。"""
    own_module = name.replace("-", "_")
    for node in ast.walk(ast.parse(file.read_text("utf-8"))):
        imports = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                   [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])
        for module in imports:
            parts = module.split(".")
            top = parts[0]
            assert top not in REMOVED, file
            if top == "PySide6":
                assert "pyside6" in declared, file
            if top.startswith("mindmap_") and top != own_module:
                assert top.replace("_", "-") in declared, (file, top)
                assert all(not part.startswith("_") for part in parts[1:]), (file, module)
                if isinstance(node, ast.ImportFrom):
                    assert all(not alias.name.startswith("_") for alias in node.names), file
        if isinstance(node, ast.Attribute):
            forbidden = {"_capture_save_snapshot", "_mark_saved_snapshot", "_preview_operation", "_require_native_save_format"}
            if name == "mindmap-workbench":
                forbidden.update({"_empty_widget", "fields", "depth_menu", "__dict__"})
                assert not (node.attr == "editor" and isinstance(node.value, ast.Attribute)
                            and node.value.attr == "canvas"), file
            assert node.attr not in forbidden, file


def test_runtime_dependencies_are_explicit_and_point_downward():
    """真实导入必须声明直接依赖；下层不能借助聚合入口安装上层实现。"""
    for name, (path, metadata) in members().items():
        declared = {re.split(r"[<>=!~ ;\[]", item)[0].lower() for item in metadata["dependencies"]}
        assert declared & EXPECTED == DEPENDENCIES[name]
        for file in runtime_sources(path / "src" / name.replace("-", "_")):
            assert_source_boundary(file, name, declared)


@pytest.mark.parametrize("source", [
    "import mindmap_qt_canvas", "from mindmap_core._private import helper",
    "import mindmap_core._private", "from mindmap_core import _helper",
])
def test_nested_module_cannot_hide_dependency_or_private_import(tmp_path, source):
    """新增子目录中的反向依赖和两种私有模块导入写法都必须被发现。"""
    nested = tmp_path / "nested"
    nested.mkdir()
    file = nested / "module.py"
    file.write_text(source, "utf-8")
    assert list(runtime_sources(tmp_path)) == [file]
    with pytest.raises(AssertionError):
        assert_source_boundary(file, "mindmap-layouts", {"mindmap-core"})


def test_own_private_module_is_allowed_and_project_template_is_separate(tmp_path):
    """包内私有协作保持可用；独立模板不按外层包的运行依赖检查。"""
    own = tmp_path / "module.py"
    own.write_text("from mindmap_layouts._internal import _helper", "utf-8")
    template = tmp_path / "project_template" / "src"
    template.mkdir(parents=True)
    (template.parent / "pyproject.toml").write_text('[project]\nname = "example-desktop"\n', "utf-8")
    (template / "app.py").write_text("import mindmap_shell", "utf-8")
    assert list(runtime_sources(tmp_path)) == [own]
    assert_source_boundary(own, "mindmap-layouts", set())


def test_folder_name_alone_cannot_exclude_runtime_sources(tmp_path):
    """只有名称而没有独立元数据的目录仍属于运行代码，不能绕过递归边界检查。"""
    directory = tmp_path / "project_template"
    directory.mkdir()
    file = directory / "module.py"
    file.write_text("import mindmap_qt_canvas", "utf-8")
    assert list(runtime_sources(tmp_path)) == [file]
    with pytest.raises(AssertionError):
        assert_source_boundary(file, "mindmap-layouts", {"mindmap-core"})


def test_canvas_does_not_use_layout_implementation_indexes():
    """画布只查询公开几何能力，不依赖布局服务内部的同级索引或坐标转换。"""
    source = (ROOT / "packages/qt/mindmap-qt-canvas/src/mindmap_qt_canvas/dragging.py").read_text("utf-8")
    assert "query_with_sibling_neighbors" in source
    for forbidden in ("geometry._transform", "geometry.groups", "geometry.locations", "geometry.visited_parent_rects"):
        assert forbidden not in source
