"""资源完整性、跨平台检出及图标渲染回归。"""


import hashlib
import json
import os
import shutil
import subprocess
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
        # 清单描述实际交付的原始字节；校验时不能转换换行符来掩盖检出或打包问题。
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], entry["path"]
        if path.suffix == ".json":
            assert json.loads(path.read_text("utf-8"))["author"] == "mindmap contributors"
        else:
            assert "SPDX-License-Identifier: MIT; Copyright (c) 2026 mindmap contributors" in path.read_text("utf-8")
    for package in ("mindmap-docking", "mindmap-shell"):
        license_text = (ROOT / "packages/desktop" / package / "LICENSE").read_text("utf-8")
        assert "MIT License" in license_text and "mindmap contributors" in license_text


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_resource_hashes_survive_git_checkout(tmp_path, autocrlf):
    """真实 Git 检出在两种自动换行配置下均保留 LF 和原始字节摘要。

    只把属性规则和资源复制到临时仓库，通过独立索引检出，不提交、不改动当前
    工作区或全局配置；缺少 Git 时仅跳过此用例，常规资源完整性校验仍然执行。
    """
    git = shutil.which("git")
    if git is None:
        pytest.skip("跨平台检出回归需要 Git")
    resources_path = Path("packages/desktop/mindmap-docking/src/mindmap_docking/resources")
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copyfile(ROOT / ".gitattributes", repository / ".gitattributes")
    shutil.copytree(ROOT / resources_path, repository / resources_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def run_git(*arguments):
        """配置只对本次命令生效；隐藏 Windows 子进程窗口并保留失败输出。"""
        return subprocess.run(
            [git, "-c", f"core.autocrlf={autocrlf}", "-c", "core.safecrlf=false", *arguments],
            cwd=repository, check=True, capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    run_git("init", "--quiet", "--template=")
    run_git("add", "--", ".gitattributes", resources_path.as_posix())
    # 强制写出索引中的资源，确保检验实际检出结果，而不是未经 Git 转换的原始副本。
    run_git("checkout-index", "--all", "--force", f"--prefix={checkout.as_posix()}/")
    resources = checkout / resources_path
    manifest_path = resources / "manifest.json"
    assert b"\r" not in manifest_path.read_bytes(), "检出的资源清单必须使用 LF"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for entry in manifest["files"]:
        content = (resources / entry["path"]).read_bytes()
        assert b"\r" not in content, f"检出的资源必须使用 LF：{entry['path']}"
        assert hashlib.sha256(content).hexdigest() == entry["sha256"], entry["path"]


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
