"""核对工作区成员、显式依赖和公开接口边界。"""

import ast
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import distributions
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MINDMAP_DIRECTORIES = {
    "mindmap-core": "core",
    "mindmap-qt-render": "qt-render",
    "mindmap-qt-editor": "qt-editor",
    "mindmap-application": "application",
}
EXPECTED = set(MINDMAP_DIRECTORIES) | {
    "mindmap-app", "mindmap-desktop-api", "mindmap-desktop-runtime",
}
DEPENDENCIES = {
    "mindmap-core": set(),
    "mindmap-qt-render": {"mindmap-core", "pyside6"},
    "mindmap-qt-editor": {"mindmap-core", "mindmap-qt-render", "pyside6"},
    "mindmap-desktop-api": {"pyside6"},
    "mindmap-desktop-runtime": {"mindmap-desktop-api", "pyside6"},
    "mindmap-application": {"mindmap-desktop-api", "mindmap-core", "mindmap-qt-editor", "mindmap-qt-render", "pyside6"},
    "mindmap-app": {"mindmap-desktop-runtime", "mindmap-application", "pyside6"},
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
    """两个导图桌面包直接位于 packages，全部成员及其锁定路径均来自当前仓库。"""
    packages = members()
    assert set(packages) == EXPECTED
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert set(config["tool"]["uv"]["sources"]) == EXPECTED
    assert all(value == {"workspace": True} for value in config["tool"]["uv"]["sources"].values())
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    assert set(lock["manifest"]["members"]) == EXPECTED | {"mindmap"}
    installed = {p["name"]: p for p in lock["package"] if "editable" in p["source"]}
    assert set(installed) == EXPECTED
    for name, package in installed.items():
        assert (ROOT / package["source"]["editable"]).resolve() == packages[name][0].resolve()
    infrastructure = {ROOT / "packages" / name for name in ("mindmap-desktop-api", "mindmap-desktop-runtime")}
    assert {path.parent for path in (ROOT / "packages").rglob("pyproject.toml")} == infrastructure
    for directory in infrastructure:
        assert packages[directory.name][0] == directory
    names = {item.metadata["Name"].lower().replace("_", "-") for item in distributions()}
    assert {name for name in names if name.startswith(("mindmap-", "workbench-", "personal-workbench"))} == EXPECTED


def test_mindmap_components_form_the_product_core():
    """四个导图成员按职责平铺，通用基础设施及产品入口独立归属。"""
    packages = members()
    core_root = ROOT / "mindmap"
    assert {path.parent for path in core_root.rglob("pyproject.toml")} == {
        core_root / relative_path for relative_path in MINDMAP_DIRECTORIES.values()
    }
    for name, relative_path in MINDMAP_DIRECTORIES.items():
        directory, _ = packages[name]
        assert directory == core_root / relative_path, name
        assert (directory / "src" / name.replace("-", "_")).is_dir(), name
        assert (directory / "tests").is_dir(), name
    for name in EXPECTED - MINDMAP_DIRECTORIES.keys():
        assert not packages[name][0].is_relative_to(core_root), name


def test_workspace_has_no_discoverable_business_entry_points():
    """图作为导图的一种页面内置，产品固定构造主体。"""
    for name, (_path, project) in members().items():
        assert not project.get("entry-points"), name
    assert members()["mindmap-app"][1]["gui-scripts"] == {"mindmap": "mindmap_app:main"}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_member_discovers_repository_workspace(name):
    """从任意包目录调用 uv 都须发现根工作区，不能误建私有锁文件或环境。

    这是实际命令行回归：仅在根目录解析 members 无法发现父级包元数据
    截断向上查找的问题；读取 workspace dir 不触发安装或修改任何用户数据。
    """
    uv = shutil.which("uv")
    assert uv is not None, "工作区验收需要安装 uv"
    directory, _ = members()[name]
    result = subprocess.run(
        [uv, "workspace", "dir"], cwd=directory,
        capture_output=True, text=True, encoding="utf-8", timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == ROOT, (name, result.stdout)


def runtime_sources(source_root):
    """递归收集源码目录中的 Python 模块，包括子目录中的模块。"""
    return source_root.rglob("*.py")


def assert_source_boundary(file, name, declared):
    """同时检查依赖声明、跨包私有模块和已明确收束的实现细节访问。"""
    own_module = name.replace("-", "_")
    for node in ast.walk(ast.parse(file.read_text("utf-8"))):
        imports = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                   [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])
        for module in imports:
            parts = module.split(".")
            top = parts[0]
            if name == "mindmap-core":
                assert top == own_module or top in sys.stdlib_module_names, (file, module)
            if top == "PySide6":
                assert "pyside6" in declared, file
            if top.startswith(("mindmap_", "workbench_", "mindmap_app")) and top != own_module:
                assert top.replace("_", "-") in declared, (file, top)
                assert all(not part.startswith("_") for part in parts[1:]), (file, module)
                if isinstance(node, ast.ImportFrom):
                    assert all(not alias.name.startswith("_") for alias in node.names), file
        if isinstance(node, ast.Attribute):
            forbidden = {"_capture_save_snapshot", "_mark_saved_snapshot", "_preview_operation", "_require_native_save_format"}
            if name in {"mindmap-app", "mindmap-application"}:
                forbidden.update({"_empty_widget", "fields", "depth_menu", "__dict__"})
                assert not (node.attr == "editor" and isinstance(node.value, ast.Attribute)
                            and node.value.attr == "canvas"), file
            assert node.attr not in forbidden, file


def test_runtime_dependencies_are_explicit_and_point_downward():
    """核对声明的依赖与实际导入，确保桌面、导图主体和节点扩展按层依赖。"""
    for name, (path, metadata) in members().items():
        declared = {re.split(r"[<>=!~ ;\[]", item)[0].lower() for item in metadata["dependencies"]}
        assert declared == DEPENDENCIES[name]
        source_root = path / "src"
        files = list(runtime_sources(source_root))
        assert {file.relative_to(source_root).parts[0] for file in files} == {name.replace("-", "_")}
        for file in files:
            assert_source_boundary(file, name, declared)


@pytest.mark.parametrize("source", [
    "import mindmap_qt_editor.canvas", "from mindmap_core._private import helper",
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
        assert_source_boundary(file, "mindmap-qt-render", {"mindmap-core"})


def test_own_private_module_is_allowed(tmp_path):
    """包内模块可以导入同一包的私有实现。"""
    own = tmp_path / "module.py"
    own.write_text("from mindmap_core.layouts._internal import _helper", "utf-8")
    assert_source_boundary(own, "mindmap-core", set())


def assert_no_internal_imports(file, package, forbidden):
    """将相对导入解析为绝对路径，同时识别从父包直接导入子包的写法。"""
    for node in ast.walk(ast.parse(file.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            imports = [f"{module}.{alias.name}" for alias in node.names]
        else:
            continue
        for target in imports:
            assert not any(target == prefix or target.startswith(prefix + ".") for prefix in forbidden), (file, target)


def test_bundled_components_keep_internal_boundaries():
    """同一分发包内仍保持单向依赖，面板与画布各自通过公开协议接入。"""
    rules = {
        "mindmap_core": {
            "": ("mindmap_core.layouts", "mindmap_core.formats"),
            "layouts": ("mindmap_core.formats",),
            "formats": ("mindmap_core.layouts",),
        },
        "mindmap_qt_render": {"": ("mindmap_core.layouts", "mindmap_core.formats")},
        "mindmap_qt_editor": {
            "": ("mindmap_qt_editor.panels", "mindmap_qt_editor.controls"),
            "panels": ("mindmap_qt_editor.canvas",),
            "canvas": ("mindmap_qt_editor.panels",),
            "controls": ("mindmap_qt_editor.canvas", "mindmap_qt_editor.panels"),
        },
    }
    packages = members()
    for module, restrictions in rules.items():
        directory, _ = packages[module.replace("_", "-")]
        root = directory / "src" / module
        for file in runtime_sources(root):
            parents = file.relative_to(root).parts[:-1]
            scope = parents[0] if parents else ""
            package = ".".join((module, *parents))
            assert_no_internal_imports(file, package, restrictions.get(scope, ()))


def test_canvas_does_not_use_layout_implementation_indexes():
    """画布只查询公开几何能力，不依赖布局服务内部的同级索引或坐标转换。"""
    directory, _ = members()["mindmap-qt-editor"]
    source = (directory / "src/mindmap_qt_editor/canvas/drop_targets.py").read_text("utf-8")
    assert "query_with_sibling_neighbors" in source
    for forbidden in ("geometry._transform", "geometry.groups", "geometry.locations", "geometry.visited_parent_rects"):
        assert forbidden not in source
