"""工作区成员、显式依赖、公开边界及删除入口的验收。"""

import ast
import importlib.util
import re
import tomllib
from pathlib import Path

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


def test_runtime_dependencies_are_explicit_and_point_downward():
    """真实导入必须声明直接依赖；下层不能借助聚合入口安装上层实现。"""
    packages = members()
    for name, (path, metadata) in packages.items():
        declared = {re.split(r"[<>=!~ ;\[]", item)[0].lower() for item in metadata["dependencies"]}
        assert declared & EXPECTED == DEPENDENCIES[name]
        for file in (path / "src" / name.replace("-", "_")).glob("*.py"):
            for node in ast.walk(ast.parse(file.read_text("utf-8"))):
                imports = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                           [node.module] if isinstance(node, ast.ImportFrom) and not node.level else [])
                for module in imports:
                    top = module.split(".")[0]
                    assert top not in REMOVED, file
                    if top == "PySide6":
                        assert "pyside6" in declared, file
                    if top.startswith("mindmap_") and top != name.replace("-", "_"):
                        assert top.replace("_", "-") in declared, (file, top)
                if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").startswith("mindmap_"):
                    assert all(not alias.name.startswith("_") for alias in node.names), file
                if isinstance(node, ast.Attribute):
                    forbidden = {"_capture_save_snapshot", "_mark_saved_snapshot", "_preview_operation", "_require_native_save_format"}
                    if name == "mindmap-workbench":
                        forbidden.add("_empty_widget")
                    assert node.attr not in forbidden, file


def test_canvas_does_not_use_layout_implementation_indexes():
    """画布只查询公开几何能力，不依赖布局服务内部的同级索引或坐标转换。"""
    source = (ROOT / "packages/qt/mindmap-qt-canvas/src/mindmap_qt_canvas/dragging.py").read_text("utf-8")
    assert "query_with_sibling_neighbors" in source
    for forbidden in ("geometry._transform", "geometry.groups", "geometry.locations", "geometry.visited_parent_rects"):
        assert forbidden not in source
