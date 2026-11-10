"""工作区成员、显式依赖、公开边界及删除入口的验收。"""

import ast
import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "mindmap-core", "mindmap-layouts", "mindmap-formats", "mindmap-qt-render",
    "mindmap-qt-ui", "mindmap-qt-canvas", "mindmap-qt-panels", "workbench-docking",
    "workbench-shell", "personal-workbench", "workbench-api", "workbench-runtime",
    "workbench-plugin-mindmap", "workbench-plugin-text",
}
REMOVED = {"mindmap_engine", "mindmap_qt", "mindmap_qt_workspace", "mindmap_workbench",
           "mindmap_docking", "mindmap_shell"}
DEPENDENCIES = {
    "mindmap-core": set(), "mindmap-layouts": {"mindmap-core"}, "mindmap-formats": {"mindmap-core"},
    "mindmap-qt-render": {"mindmap-core"},
    "mindmap-qt-ui": {"mindmap-core", "mindmap-layouts", "mindmap-qt-render"},
    "mindmap-qt-canvas": {"mindmap-core", "mindmap-layouts", "mindmap-formats", "mindmap-qt-render", "mindmap-qt-ui"},
    "mindmap-qt-panels": {"mindmap-core", "mindmap-layouts", "mindmap-qt-ui"},
    "workbench-docking": set(), "workbench-shell": {"workbench-docking"},
    "workbench-api": set(),
    "workbench-runtime": {"workbench-api", "workbench-shell", "workbench-docking"},
    "workbench-plugin-mindmap": {"workbench-api", "mindmap-core", "mindmap-layouts", "mindmap-formats",
                               "mindmap-qt-canvas", "mindmap-qt-panels", "mindmap-qt-ui", "mindmap-qt-render",
                               "workbench-docking"},
    "workbench-plugin-text": {"workbench-api"},
    "personal-workbench": {"workbench-api", "workbench-runtime", "workbench-plugin-mindmap",
                           "workbench-plugin-text", "workbench-docking", "workbench-shell"},
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
    """全部成员从当前仓库解析，已删除的入口既不安装也不进入锁文件。"""
    packages = members()
    assert set(packages) == EXPECTED
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert set(config["tool"]["uv"]["sources"]) == EXPECTED
    assert all(value == {"workspace": True} for value in config["tool"]["uv"]["sources"].values())
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    assert set(lock["manifest"]["members"]) == EXPECTED | {"personal-workbench-workspace"}
    installed = {p["name"]: p for p in lock["package"] if "editable" in p["source"]}
    assert set(installed) == EXPECTED
    for name, package in installed.items():
        assert (ROOT / package["source"]["editable"]).resolve() == packages[name][0].resolve()
    for module in REMOVED:
        assert importlib.util.find_spec(module) is None
    assert not (ROOT / "packages/desktop/mindmap-docking").exists()
    assert not (ROOT / "packages/desktop/mindmap-shell").exists()
    assert not (ROOT / "plugins/workbench-plugin-mindmap/workbench").exists()


def test_mindmap_components_are_owned_by_plugin_directory():
    """导图业务包与适配入口同目录归属，通用基础设施仍可独立于导图安装。

    目录聚合不合并分发包：七个组件须保持各自元数据、源码和测试，
    平台及文本插件不得放入导图插件目录，也不能重新出现顶层业务分组。
    """
    packages = members()
    plugin_root = ROOT / "plugins/workbench-plugin-mindmap"
    assert packages["workbench-plugin-mindmap"][0].is_relative_to(plugin_root)
    assert not (plugin_root / "pyproject.toml").exists()
    domain = {"mindmap-core", "mindmap-layouts", "mindmap-formats", "mindmap-qt-render",
              "mindmap-qt-ui", "mindmap-qt-canvas", "mindmap-qt-panels"}
    for name in domain:
        directory, _ = packages[name]
        assert directory.is_relative_to(plugin_root), name
        assert (directory / "src" / name.replace("-", "_")).is_dir(), name
        assert (directory / "tests").is_dir(), name
    for name in EXPECTED - domain - {"workbench-plugin-mindmap"}:
        assert not packages[name][0].is_relative_to(plugin_root), name
    assert not (ROOT / "packages/engine").exists()
    assert not (ROOT / "packages/qt").exists()


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
            if top.startswith(("mindmap_", "workbench_", "personal_workbench")) and top != own_module:
                assert top.replace("_", "-") in declared, (file, top)
                assert all(not part.startswith("_") for part in parts[1:]), (file, module)
                if isinstance(node, ast.ImportFrom):
                    assert all(not alias.name.startswith("_") for alias in node.names), file
        if isinstance(node, ast.Attribute):
            forbidden = {"_capture_save_snapshot", "_mark_saved_snapshot", "_preview_operation", "_require_native_save_format"}
            if name in {"personal-workbench", "workbench-plugin-mindmap"}:
                forbidden.update({"_empty_widget", "fields", "depth_menu", "__dict__"})
                assert not (node.attr == "editor" and isinstance(node.value, ast.Attribute)
                            and node.value.attr == "canvas"), file
            assert node.attr not in forbidden, file


def test_platform_and_plugins_obey_business_boundaries():
    """平台不能导入业务包；插件不能反向导入运行时或应用装配层。"""
    for name in ("workbench-api", "workbench-runtime", "workbench-plugin-mindmap", "workbench-plugin-text"):
        path, _ = members()[name]
        forbidden = ({"mindmap_core", "mindmap_layouts", "mindmap_formats", "mindmap_qt_canvas",
                      "mindmap_qt_panels", "mindmap_qt_render", "mindmap_qt_ui", "workbench_plugin_mindmap",
                      "workbench_plugin_text"} if name in {"workbench-api", "workbench-runtime"} else
                     {"workbench_runtime", "personal_workbench", "mindmap_workbench"})
        for file in runtime_sources(path / "src"):
            for node in ast.walk(ast.parse(file.read_text("utf-8"))):
                imported = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                            [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])
                assert all(module.split(".")[0] not in forbidden for module in imported), file


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
    (template / "app.py").write_text("import workbench_shell", "utf-8")
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
    directory, _ = members()["mindmap-qt-canvas"]
    source = (directory / "src/mindmap_qt_canvas/dragging.py").read_text("utf-8")
    assert "query_with_sibling_neighbors" in source
    for forbidden in ("geometry._transform", "geometry.groups", "geometry.locations", "geometry.visited_parent_rects"):
        assert forbidden not in source
