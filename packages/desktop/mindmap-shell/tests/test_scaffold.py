"""模板生成器的文件边界、中文标题和独立项目结构验收。"""

import ast
import tomllib
from pathlib import Path

import pytest
from mindmap_shell.scaffold import generate_project


def test_generated_project_is_independent_and_has_mit_license(tmp_path):
    output = generate_project(tmp_path / "中文目录", project_name="example-desktop", title='示例 "桌面" 应用')
    project = tomllib.loads((output / "pyproject.toml").read_text("utf-8"))
    assert project["project"]["name"] == "example-desktop"
    assert project["project"]["license"] == "MIT"
    assert project["project"]["scripts"] == {"example-desktop": "example_desktop.app:main"}
    assert "workspace" not in project.get("tool", {}).get("uv", {})
    assert set(project["tool"]["uv"]["sources"]) == {"mindmap-shell", "mindmap-docking"}
    assert all(source["editable"] is True for source in project["tool"]["uv"]["sources"].values())
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "uv.lock").is_file())
    assert {name: Path(source["path"]).resolve()
            for name, source in project["tool"]["uv"]["sources"].items()} == {
        name: root / "packages/desktop" / name for name in ("mindmap-shell", "mindmap-docking")
    }
    source = (output / "src/example_desktop/app.py").read_text("utf-8")
    ast.parse(source)
    assert "my_desktop_app" not in source
    assert "MIT License" in (output / "LICENSE").read_text("utf-8")
    assert (output / ".python-version").is_file()


def test_generator_never_overwrites_existing_directory(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "保留.txt"
    sentinel.write_text("原有内容", "utf-8")
    with pytest.raises(FileExistsError):
        generate_project(output)
    assert sentinel.read_text("utf-8") == "原有内容"


@pytest.mark.parametrize("name,module", [("../outside", None), ("valid-name", "class"), ("valid-name", "../escape")])
def test_generator_rejects_invalid_names_before_writing(tmp_path, name, module):
    with pytest.raises(ValueError):
        generate_project(tmp_path / "output", project_name=name, module_name=module)
    assert not (tmp_path / "output").exists()


def test_packaged_template_declares_current_modules_and_entrypoint():
    """直接验收唯一的随包模板，避免依赖本仓库之外的演示工程或模板副本。"""
    import mindmap_shell.scaffold
    package = Path(mindmap_shell.scaffold.__file__).parent
    template = package / "project_template"
    project = tomllib.loads((template / "pyproject.toml").read_text("utf-8"))
    assert set(project["project"]["dependencies"]) == {"mindmap-shell>=0.1.0,<0.2.0", "mindmap-docking>=0.1.0,<0.2.0", "PySide6>=6.12,<6.13"}
    source = (template / "src/my_desktop_app/app.py").read_text("utf-8")
    imports = {node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)}
    assert {"mindmap_docking", "mindmap_shell"} <= imports
    assert project["project"]["scripts"] == {"my-desktop-app": "my_desktop_app.app:main"}
