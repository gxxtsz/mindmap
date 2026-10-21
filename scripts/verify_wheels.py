"""在仓库外创建八个干净环境，分别验证成员 wheel 的依赖隔离与桌面完整流程。

本脚本只使用 uv 管理环境和依赖。临时目录退出前会被清理，执行任何递归清理前
先确认其绝对路径仍是本次创建的系统临时目录子项，避免删除工作区或其他目录。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path


def run():
    """仅从本仓库 dist 读取八个成员的 wheel，在独立环境中验证依赖与运行。"""
    repository = Path(__file__).resolve().parents[1]
    distribution = repository / "dist"
    wheels = {}
    # 引擎成员比 Qt 组件多一层分组目录，使用显式映射避免根据包名推断目录深度。
    # 键继续使用 wheel 文件的下划线名称，值指向各成员包含 pyproject.toml 的真实目录。
    member_paths = {
        "mindmap_core": "packages/engine/mindmap-core",
        "mindmap_layouts": "packages/engine/mindmap-layouts",
        "mindmap_formats": "packages/engine/mindmap-formats",
        "mindmap_engine": "packages/engine/mindmap-engine",
        "mindmap_qt": "packages/mindmap-qt",
        "mindmap_docking": "packages/mindmap-docking",
        "mindmap_shell": "packages/mindmap-shell",
        "mindmap_workbench": "apps/mindmap-workbench",
    }
    for name, member_path in member_paths.items():
        metadata = tomllib.loads(
            (repository / member_path / "pyproject.toml").read_text(encoding="utf-8")
        )
        wheels[name] = distribution / f"{name}-{metadata['project']['version']}-py3-none-any.whl"
    if not all(path.is_file() for path in wheels.values()):
        raise RuntimeError("请先运行 uv build --all-packages 构建八个成员的 wheel")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("未找到 uv")
    lock = tomllib.loads((repository / "uv.lock").read_text(encoding="utf-8"))
    qt_version = next(package["version"] for package in lock["package"] if package["name"] == "pyside6")
    temp_parent = Path(tempfile.gettempdir()).resolve()
    temporary = Path(tempfile.mkdtemp(prefix="mindmap-wheel-check-")).resolve()
    if temporary.parent != temp_parent or not temporary.name.startswith("mindmap-wheel-check-"):
        raise RuntimeError("临时目录范围检查失败")
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)

    def execute(arguments):
        result = subprocess.run(
            [str(value) for value in arguments],
            cwd=temporary,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError(f"独立环境验证失败（{result.returncode}）：\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def interpreter(path):
        return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    try:
        core_env, qt_env = temporary / "core", temporary / "qt"
        # 约束只决定依赖的来源，不主动安装依赖；只有 wheel 元数据声明的包才会被解析。
        constraints = temporary / "constraints.txt"
        constraints.write_text(
            "".join(f"{name.replace('_', '-')} @ {wheel.as_uri()}\n" for name, wheel in wheels.items())
            + f"PySide6=={qt_version}\n",
            encoding="utf-8",
        )
        independent = {}
        for name in ("mindmap_core", "mindmap_layouts", "mindmap_formats"):
            # 环境目录不能与导入名相同，否则 cwd 下的目录会被识别成未安装的命名空间包。
            directory = temporary / f"env-{name}"
            execute([uv, "venv", "--python", "3.12", directory])
            execute(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    interpreter(directory),
                    "--constraint",
                    constraints,
                    wheels[name],
                ]
            )
            checks = {
                "mindmap_core": """
from mindmap_core import BranchStyle, MindMapEngine, LayoutConfig
assert importlib.util.find_spec("mindmap_layouts") is None
assert importlib.util.find_spec("mindmap_formats") is None
engine = MindMapEngine()
engine.add_child(engine.root_id, "独立核心")
engine.set_branch_style([engine.root_id], BranchStyle.ROUND)
assert engine.document.root.style["branch"] == {"kind": "round"}
before = engine.document
engine.set_layout(LayoutConfig(algorithm_id="not-installed", options={"参数": [1, 2]}))
assert engine.undo() and engine.document == before
""",
                "mindmap_layouts": """
from mindmap_core import BranchPath, MindMapEngine
from mindmap_layouts import create_default_layout_registry, route_branches
assert importlib.util.find_spec("mindmap_formats") is None
engine = MindMapEngine()
child = engine.add_child(engine.root_id, "纯 Python 路由")
engine.set_branch_style([engine.root_id], "arrow2")
layout = create_default_layout_registry().layout(engine.document)
assert isinstance(layout.branches[child], BranchPath)
assert layout.branches[child].arrow is not None
assert route_branches(engine.document, layout.nodes) == layout.branches
""",
                "mindmap_formats": """
from mindmap_core import MindMapDocument, LayoutConfig, apply_branch_style
from mindmap_formats import save_document, load_document
assert importlib.util.find_spec("mindmap_layouts") is None
document = MindMapDocument.new("未知布局可交换")
document.layout = LayoutConfig(algorithm_id="not-installed", options={"间距": 12})
apply_branch_style(document, [document.root_id], "round")
for suffix in ("mindmap", "json"):
    path = "independent." + suffix
    save_document(document, path)
    assert load_document(path) == document
""",
            }
            execute(
                [
                    interpreter(directory),
                    "-c",
                    "import importlib.util\n"
                    'assert importlib.util.find_spec("mindmap_engine") is None\n'
                    'assert importlib.util.find_spec("PySide6") is None\n' + checks[name],
                ]
            )
            independent[name] = True
            print(f"{name}：独立安装与依赖隔离通过", flush=True)
        execute([uv, "venv", "--python", "3.12", core_env])
        execute(
            [
                uv,
                "pip",
                "install",
                "--python",
                interpreter(core_env),
                "--constraint",
                constraints,
                wheels["mindmap_engine"],
            ]
        )
        core = execute(
            [
                interpreter(core_env),
                "-c",
                """
import importlib.util, json
from pathlib import Path
import mindmap_engine
import mindmap_core
from mindmap_engine import LayoutConfig, MindMapEngine, create_default_layout_registry, load_document, save_document
assert importlib.util.find_spec("PySide6") is None
assert importlib.util.find_spec("mindmap_docking") is None
assert importlib.util.find_spec("mindmap_shell") is None
assert mindmap_engine.MindMapEngine is mindmap_core.MindMapEngine
assert mindmap_engine.MindMapDocument is mindmap_core.MindMapDocument
assert mindmap_engine.BranchStyle is mindmap_core.BranchStyle
assert mindmap_engine.BranchPath is mindmap_core.BranchPath
assert mindmap_engine.PathCommand is mindmap_core.PathCommand
for removed in ("engine", "models", "layout", "storage"):
    assert importlib.util.find_spec("mindmap_engine." + removed) is None
engine = MindMapEngine()
a = engine.add_child(engine.root_id, "独立核心")
engine.add_child(a, "子项")
engine.set_layout(LayoutConfig(direction="down"))
engine.set_branch_style([engine.root_id], "round")
assert len(create_default_layout_registry().layout(engine.document).nodes) == 3
save_document(engine.document, "core.mindmap")
assert load_document("core.mindmap") == engine.document
assert "site-packages" in str(Path(mindmap_engine.__file__))
print(json.dumps({"核心包无Qt": True, "核心文件往返": True}))
""",
            ]
        )
        execute([uv, "venv", "--python", "3.12", qt_env])
        execute(
            [
                uv,
                "pip",
                "install",
                "--python",
                interpreter(qt_env),
                "--constraint",
                constraints,
                wheels["mindmap_qt"],
            ]
        )
        qt = execute(
            [
                interpreter(qt_env),
                "-c",
                """
import importlib.util, json
from PySide6.QtWidgets import QApplication
from mindmap_engine import MindMapEngine
from mindmap_qt import MindMapWidget, MindMapWorkspace
assert importlib.util.find_spec("mindmap_workbench") is None
assert importlib.util.find_spec("mindmap_docking") is None
assert importlib.util.find_spec("mindmap_shell") is None
app = QApplication([])
engine = MindMapEngine()
widget = MindMapWidget(engine)
engine.add_child(engine.root_id, "独立组件")
widget.set_branch_style("arrow")
before = engine.document
widget.preview_branch_style("round")
assert engine.document == before
widget.clear_branch_preview()
assert len(widget.node_items) == 2
widget.dispose()
widget.close()
workspace = MindMapWorkspace(engine)
workspace.resize(1100, 750)
workspace.show()
app.processEvents()
workspace.show_tool_window("outline", focus=False)
workspace._move_tool("outline", "bottom")
workspace._move_tool("properties", "left", floating=True)
state = workspace.save_state()
workspace.restore_state(state)
assert workspace.tools["properties"].floating
workspace.properties.fields["style.font_size"].setValue(22)
workspace.flush_pending_edits()
assert engine.document.root.style["font_size"] == 22
workspace.dispose()
workspace.close()
print(json.dumps({"Qt包独立安装与依赖解析": True, "停靠工作区独立安装": True}))
""",
            ]
        )
        # 基础包各用一个环境安装，验证资源、模板和依赖方向，避免完整工作台
        # 环境中的其他成员掩盖漏声明依赖。运行目录位于仓库外，不使用 PYTHONPATH。
        docking_env = temporary / "docking"
        execute([uv, "venv", "--python", "3.12", docking_env])
        execute([uv, "pip", "install", "--python", interpreter(docking_env),
                 "--constraint", constraints, wheels["mindmap_docking"]])
        docking = execute([interpreter(docking_env), "-c", """
import hashlib, importlib.util, json, sys
from pathlib import Path
import mindmap_docking
from mindmap_docking import DockHost, DockManager, DocumentSpec
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
assert importlib.util.find_spec("mindmap_shell") is None
assert importlib.util.find_spec("mindmap_engine") is None
assert importlib.util.find_spec("mindmap_qt") is None
assert Path(mindmap_docking.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
resources = Path(mindmap_docking.__file__).parent / "resources"
manifest = json.loads((resources / "manifest.json").read_text("utf-8"))
expected = {entry["path"] for entry in manifest["files"]}
actual = {path.relative_to(resources).as_posix() for path in resources.rglob("*")
          if path.is_file() and path.name != "manifest.json"}
assert len(expected) == len(manifest["files"])
assert expected == actual and {"dark.json", "light.json", "icons/mindmap.svg"} <= expected
for entry in manifest["files"]:
    assert hashlib.sha256((resources / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
app = QApplication([])
window = QMainWindow()
manager = DockManager(storage_dir=Path("docking-state"))
window.setCentralWidget(DockHost(manager, "main", window))
manager.register_document(DocumentSpec("document", "独立停靠组件", lambda: QLabel("内容")))
assert not manager.theme.icon("mindmap").isNull()
manager.set_theme("dark")
assert not manager.theme.icon("mindmap").isNull()
manager.shutdown()
window.close()
print(json.dumps({"停靠包独立安装与资源完整性": True, "停靠包依赖隔离": True}))
"""])
        shell_env = temporary / "shell"
        execute([uv, "venv", "--python", "3.12", shell_env])
        execute([uv, "pip", "install", "--python", interpreter(shell_env),
                 "--constraint", constraints, wheels["mindmap_shell"]])
        shell = execute([interpreter(shell_env), "-c", """
import importlib.util, json, sys, tomllib
from pathlib import Path
import mindmap_shell
from mindmap_shell.scaffold import generate_project
from PySide6.QtWidgets import QApplication
assert importlib.util.find_spec("mindmap_engine") is None
assert importlib.util.find_spec("mindmap_qt") is None
assert importlib.util.find_spec("mindmap_workbench") is None
assert Path(mindmap_shell.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
generated = generate_project(Path("generated-desktop"), project_name="example-desktop", title="安装模板验收")
metadata = tomllib.loads((generated / "pyproject.toml").read_text("utf-8"))
assert metadata["project"]["dependencies"] == ["mindmap-shell>=0.1.0,<0.2.0"]
assert "sources" not in metadata.get("tool", {}).get("uv", {})
assert (generated / ".python-version").is_file() and (generated / "LICENSE").is_file()
source = generated / "src/example_desktop/app.py"
namespace = {"__name__": "template_acceptance"}
exec(compile(source.read_text("utf-8"), str(source), "exec"), namespace)
app = QApplication([])
window = namespace["build_window"](storage_dir=Path("shell-state"), restore=False)
window.show()
app.processEvents()
assert window.spec.title == "安装模板验收" and len(window.manager.documents) == 1
window.manager.set_theme("dark")
window.close()
print(json.dumps({"外壳包独立安装与依赖隔离": True, "随包模板生成与运行": True}))
"""])
        workbench_env = temporary / "workbench"
        execute([uv, "venv", "--python", "3.12", workbench_env])
        execute([uv, "pip", "install", "--python", interpreter(workbench_env),
                 "--constraint", constraints, wheels["mindmap_workbench"]])
        workbench = execute([interpreter(workbench_env), "-c", """
import importlib.util, json, sys
from importlib.metadata import distributions
from pathlib import Path
import mindmap_docking, mindmap_shell, mindmap_workbench, mindmap_qt, mindmap_engine
from mindmap_docking import DockPosition, DockTarget
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from mindmap_workbench.shell_adapter import build_window
from mindmap_engine import MindMapError, load_document
app = QApplication([])
window = build_window(storage_dir=Path("workbench-state").absolute(), restore=False)
assert window.canvas is None and not len(window.sessions)
assert window.new_document(Path("workbench.mindmap").absolute())
primary = window.active_session
window.show()
app.processEvents()
assert window.panels.properties.fields["style.font_size"].isVisible()
original = window.engine.document.root.text
window.engine.update_node(window.engine.root_id, text="安装包工作台")
assert window.engine.undo() and window.engine.document.root.text == original
assert window.engine.redo() and window.engine.document.root.text == "安装包工作台"
window.panels.properties.fields["style.font_size"].setValue(23)
window.panels.flush_pending_edits()
node = window.engine.add_child(window.engine.root_id, "完整流程")
window.canvas.select_nodes([node])
window.panels.properties.fields["style.font_size"].setValue(24)
window.panels.flush_pending_edits()
assert window.engine.get_node(node).style["font_size"] == 24
window.manager.move("outline", DockTarget(DockPosition.BOTTOM_LEFT))
window.manager.activate("outline", focus=False)
assert window.manager.states["outline"].position == DockPosition.BOTTOM_LEFT
image = QImage(64, 40, QImage.Format.Format_ARGB32)
image.fill(QColor("#ee8044"))
buffer = QBuffer()
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
assert image.save(buffer, "PNG")
window.engine.attach_image(node, bytes(buffer.data()))
window.canvas.select_nodes([window.engine.root_id])
window.canvas.set_branch_style("round")
window.canvas.select_nodes([node])
window.canvas.set_branch_style("arrow2", scope="level")
assert window.save()
assert load_document(window.files.path) == window.engine.document
assert window.engine.document.root.style["font_size"] == 23
assert window.open_path(window.files.path)
before = window.engine.document
window.manager.set_theme("dark")
assert window.engine.document == before
for suffix in ("png", "svg", "pdf"):
    target = Path("workbench." + suffix)
    window.canvas.export(target)
    assert target.stat().st_size > 500
for module in (mindmap_docking, mindmap_shell, mindmap_workbench, mindmap_qt, mindmap_engine):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert window.new_document(Path("second.mindmap").absolute())
secondary = window.active_session
secondary.engine.update_node(secondary.engine.root_id, text="独立保存")
assert window.save_all() and not secondary.files.is_dirty
assert load_document(secondary.files.path).root.text == "独立保存"
assert window.files.format_filters("open") == "思维导图 (*.mindmap)"
assert window.files.format_filters("save") == "思维导图 (*.mindmap)"
unsupported = Path("unsupported.json").absolute()
assert not window.new_document(unsupported) and not unsupported.exists()
# 安装后的保存边界同样拒绝手工绑定的错误后缀，不调用可注入写入器。
original_path = secondary.files.path
secondary.files.path = unsupported
assert not window.save() and not unsupported.exists()
secondary.files.path = original_path
try:
    window.open_path(unsupported)
except MindMapError as error:
    assert "仅支持打开 .mindmap" in str(error)
else:
    raise AssertionError("安装后的工作台不能打开非原生导图")
assert window.active_session is secondary
assert window.open_path(primary.files.path) and window.active_session is primary
assert len(window.sessions) == 2
window.manager.restore_default_layout()
assert len(window.sessions) == 2 and window.active_session is primary
window.close()
assert primary.canvas._disposed and secondary.canvas._disposed and not len(window.sessions)
print(json.dumps({"工作台独立安装与框架依赖解析": True, "工作台文件与导出": True, "多导图与自动保存边界": True,
                  "仅打开和保存原生导图": True,
                  "编辑与撤销重做": True, "图片与分支样式": True, "侧栏属性与工作区": True,
                  "已验证运行依赖": {item.metadata["Name"]: item.version for item in distributions()}}))
"""])
        workbench_result = json.loads(workbench)
        pins = {name.lower().replace("_", "-"): version
                for name, version in workbench_result.pop("已验证运行依赖").items()}
        # 发行约束来自真正通过验收的干净环境，避免交付时重新解析出其他版本。
        # 八个成员全部来自当前仓库，完整保留本次验证的固定版本。
        for name, wheel in wheels.items():
            pins[name.replace("_", "-")] = wheel.name.split("-")[1]
        release_constraints = repository / ".artifacts/release-constraints.txt"
        release_constraints.parent.mkdir(exist_ok=True)
        release_constraints.write_text(
            "# 自动生成：本次通过独立安装验收的完整版本约束。\n"
            + "".join(f"{name}=={version}\n" for name, version in sorted(pins.items())), "utf-8",
        )
        report = {
            "独立能力包": independent,
            **json.loads(core),
            **json.loads(qt),
            **json.loads(docking),
            **json.loads(shell),
            **workbench_result,
            "Python": "3.12",
            "PySide6": qt_version,
        }
        output = repository / ".artifacts" / "wheel-verification.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        resolved = temporary.resolve()
        if resolved.parent != temp_parent or not resolved.name.startswith("mindmap-wheel-check-"):
            raise RuntimeError("拒绝清理越界目录")
        shutil.rmtree(resolved)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="验证本仓库全部独立安装包和桌面应用")
    parser.parse_args()
    run()
