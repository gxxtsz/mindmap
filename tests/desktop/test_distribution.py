"""项目工作区、包边界、资源完整性及应用配置隔离验收。"""

import ast
import hashlib
import json
import tomllib
from pathlib import Path

import pytest
from mindmap_docking import DockManager, Theme
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QSettings, QSize, QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[2]


def test_workspace_and_lock_only_reference_current_repository():
    """八个工作区成员全部从仓库内部解析，锁文件没有相邻目录依赖。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    sources = project["tool"]["uv"]["sources"]
    expected = {
        "mindmap-core", "mindmap-layouts", "mindmap-formats", "mindmap-engine",
        "mindmap-qt", "mindmap-docking", "mindmap-shell", "mindmap-workbench",
    }
    assert set(sources) == expected - {"mindmap-workbench"}
    assert all(source == {"workspace": True} for source in sources.values())
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    assert set(lock["manifest"]["members"]) == expected | {"mindmap"}
    assert {package["name"] for package in lock["package"] if "editable" in package["source"]} == expected
    members = {package["name"]: package for package in lock["package"] if package["name"] in expected}
    assert set(members) == expected
    for member in members.values():
        source = (ROOT / member["source"]["editable"]).resolve()
        assert source.is_relative_to(ROOT) and (source / "pyproject.toml").is_file()
        metadata = tomllib.loads((source / "pyproject.toml").read_text("utf-8"))
        assert all(dependency.startswith(("mindmap-", "PySide6"))
                   for dependency in metadata["project"]["dependencies"])


def test_package_imports_keep_engine_canvas_and_shell_boundaries():
    """迁入停靠和外壳后，纯引擎与独立画布不会反向依赖桌面框架。"""
    boundaries = (
        ("packages/mindmap-docking/src", {"mindmap_shell", "mindmap_workbench", "mindmap_qt", "mindmap_engine"}),
        ("packages/mindmap-shell/src", {"mindmap_workbench", "mindmap_qt", "mindmap_engine"}),
        ("packages/mindmap-qt/src", {"mindmap_docking", "mindmap_shell", "mindmap_workbench"}),
        ("packages/engine", {"mindmap_docking", "mindmap_shell", "mindmap_workbench", "mindmap_qt", "PySide6"}),
    )
    for directory, forbidden in boundaries:
        for path in (ROOT / directory).rglob("*.py"):
            if "tests" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                modules = (
                    [alias.name for alias in node.names] if isinstance(node, ast.Import) else
                    [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
                )
                assert all(module.split(".")[0] not in forbidden
                           for module in modules), str(path)


def test_desktop_resource_manifest_and_project_license_are_complete():
    """主题与图标完整随包交付，资源摘要和本项目许可保持一致。"""
    resources = ROOT / "packages/mindmap-docking/src/mindmap_docking/resources"
    manifest = json.loads((resources / "manifest.json").read_text("utf-8"))
    assert manifest["license"] == "MIT"
    expected = {entry["path"] for entry in manifest["files"]}
    actual = {path.relative_to(resources).as_posix() for path in resources.rglob("*")
              if path.is_file() and path.name != "manifest.json"}
    assert len(expected) == len(manifest["files"])
    assert expected == actual and {"dark.json", "light.json", "icons/mindmap.svg"} <= expected
    for entry in manifest["files"]:
        path = resources / entry["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        if path.suffix == ".json":
            assert json.loads(path.read_text("utf-8"))["author"] == "mindmap contributors"
        else:
            assert "SPDX-License-Identifier: MIT; Copyright (c) 2026 mindmap contributors" in path.read_text("utf-8")
    for package in ("mindmap-docking", "mindmap-shell"):
        license_text = (ROOT / "packages" / package / "LICENSE").read_text("utf-8")
        assert "MIT License" in license_text and "mindmap contributors" in license_text


def test_project_configuration_namespace_and_preferences_are_isolated(qtbot, tmp_path, monkeypatch):
    """默认配置位于项目命名空间，应用读取自己的布局位置与滚轮偏好。"""
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: str(tmp_path))
    configuration = tmp_path / "mindmap/mindmap-workbench"
    configuration.mkdir(parents=True)
    preferences = QSettings(str(configuration / "preferences.ini"), QSettings.Format.IniFormat)
    preferences.setValue("view/wheel_mode", "zoom")
    preferences.sync()
    manager = DockManager(application_id="mindmap-workbench")
    assert manager.storage_dir == configuration and manager.storage_file == configuration / "layouts-v1.json"
    manager.shutdown()
    window = build_window(restore=False)
    qtbot.addWidget(window)
    assert window.manager.storage_dir == configuration and window.canvas.wheel_mode == "zoom"
    window.close()


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_packaged_icons_render_at_small_sizes_and_multiple_dpi(qapp, theme_name):
    """所有随包 SVG 都能解析，并在菜单、侧栏与高分屏尺寸生成非空像素。"""
    theme = Theme(theme_name)
    resources = ROOT / "packages/mindmap-docking/src/mindmap_docking/resources/icons"
    for path in resources.glob("*.svg"):
        assert QSvgRenderer(str(path)).isValid(), path.name
        icon = theme.icon(path.stem)
        assert not icon.isNull(), path.name
        for size in (16, 20):
            for ratio in (1.0, 1.5, 2.0):
                for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
                    image = icon.pixmap(QSize(size, size), ratio, mode).toImage()
                    assert not image.isNull(), path.name
                    assert any(image.pixelColor(x, y).alpha()
                               for y in range(image.height()) for x in range(image.width())), path.name
