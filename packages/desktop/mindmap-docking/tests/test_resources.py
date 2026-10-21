"""按真实能力归属迁移的组件与行为回归。"""


import hashlib
import json
from pathlib import Path

import pytest
from mindmap_docking import Theme
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtSvg import QSvgRenderer

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "uv.lock").is_file())


def test_desktop_resource_manifest_and_project_license_are_complete():
    """主题与图标完整随包交付，资源摘要和本项目许可保持一致。"""
    resources = ROOT / "packages/desktop/mindmap-docking/src/mindmap_docking/resources"
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
        license_text = (ROOT / "packages/desktop" / package / "LICENSE").read_text("utf-8")
        assert "MIT License" in license_text and "mindmap contributors" in license_text


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_packaged_icons_render_at_small_sizes_and_multiple_dpi(qapp, theme_name):
    """所有随包 SVG 都能解析，并在菜单、侧栏与高分屏尺寸生成非空像素。"""
    theme = Theme(theme_name)
    resources = ROOT / "packages/desktop/mindmap-docking/src/mindmap_docking/resources/icons"
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
