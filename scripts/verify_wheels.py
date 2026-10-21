"""在仓库外逐个安装工作区 wheel，验证显式依赖、资源及桌面完整流程。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from workspace_members import workspace_members

# 业务验收按包名声明，真实成员位置与版本均由根工作区元数据发现。
CHECKS = {
    "mindmap-qt-render": r'''

from mindmap_core import LayoutConfig, LayoutInfo, LayoutRegistry, LayoutResult, MindMapDocument, Rect
from mindmap_qt_render import export_document
class SingleNodeLayout:
    """受控的单节点测试布局，不借用任何内置算法。"""

    info = LayoutInfo("single", "单节点测试", LayoutConfig(algorithm_id="single"))

    def validate_config(self, config):
        pass

    def layout(self, document, measurements=None, **options):
        width, height = measurements[document.root_id]
        rect = Rect(0, 0, width, height)
        return LayoutResult(nodes={document.root_id: rect}, bounds=rect.padded(30))
document = MindMapDocument.new("仅核心协议的渲染")
document.layout = LayoutConfig(algorithm_id="single")
registry = LayoutRegistry()
registry.register(SingleNodeLayout())
for extension in ("png", "svg", "pdf"):
    target = Path("render." + extension)
    export_document(document, target, layout_registry=registry)
    assert target.stat().st_size > 100

''',
    "mindmap-qt-ui": r'''

from mindmap_core import MindMapDocument
from mindmap_qt_ui import NodePropertiesForm, UiColors
form = NodePropertiesForm()
document = MindMapDocument.new("独立属性表单")
form.load_nodes([document.root], root_id=document.root_id, parents={}, editable=True)
assert form.fields["style.font_size"].value() > 0
assert UiColors.__module__ == "mindmap_qt_ui.ui_style"
form.close()

''',
    "mindmap-qt-canvas": r'''

from mindmap_qt_canvas import MindMapFileController, MindMapWidget
class Inputs:
    def flush_pending_edits(self):
        pass
canvas = MindMapWidget()
node = canvas.engine.add_child(canvas.engine.root_id, "独立交互画布")
canvas.reveal_nodes([node])
assert canvas.selected_node_ids() == [node]
files = MindMapFileController(canvas, Inputs())
files.path = Path("canvas.mindmap")
assert files.save()
assert files.open_path("canvas.mindmap")
canvas.export("canvas.png")
canvas.dispose()
canvas.close()

''',
    "mindmap-qt-panels": r'''

from mindmap_core import MindMapEngine
from mindmap_qt_panels import OutlinePanel, PanelHost
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget
class Host(QWidget):
    selection_changed = Signal(list)
    document_changed = Signal(object)
    def __init__(self):
        super().__init__()
        self.engine = MindMapEngine()
        self.document_view = self.engine.document
    def selected_node_ids(self):
        return [self.engine.root_id]
host = Host()
outline = OutlinePanel(host, host)
outline.set_display_depth(1)
assert outline.tree_model.document.root_id == host.engine.root_id
assert PanelHost.__module__ == "mindmap_qt_panels.contracts"
host.close()

''',
    "mindmap-docking": r'''

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

''',
    "mindmap-shell": r'''

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
assert {item.split(">", 1)[0] for item in metadata["project"]["dependencies"]} == {"mindmap-shell", "mindmap-docking", "PySide6"}
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

''',
    "mindmap-workbench": r'''

import importlib.util, json, sys
from importlib.metadata import distributions
from pathlib import Path
import mindmap_docking, mindmap_shell, mindmap_workbench, mindmap_core
import mindmap_qt_canvas, mindmap_qt_panels, mindmap_qt_ui, mindmap_qt_render
assert importlib.util.find_spec("mindmap_qt") is None
assert importlib.util.find_spec("mindmap_qt_workspace") is None
from mindmap_docking import DockPosition, DockTarget
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from mindmap_workbench.shell_adapter import build_window
from mindmap_core import MindMapError
from mindmap_formats import load_document
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
for module in (mindmap_docking, mindmap_shell, mindmap_workbench, mindmap_core,
               mindmap_qt_canvas, mindmap_qt_panels, mindmap_qt_ui, mindmap_qt_render):
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
assert primary.disposed and secondary.disposed and not len(window.sessions)
print(json.dumps({"工作台独立安装与框架依赖解析": True, "工作台文件与导出": True, "多导图与自动保存边界": True,
                  "仅打开和保存原生导图": True,
                  "编辑与撤销重做": True, "图片与分支样式": True, "侧栏属性与工作区": True,
                  "已验证运行依赖": {item.metadata["Name"]: item.version for item in distributions()}}))

''',
    "mindmap-core": r'''

from mindmap_core import MindMapEngine, SaveSnapshot
engine = MindMapEngine()
child = engine.add_child(engine.root_id, "核心独立安装")
saved = engine.capture_save_snapshot()
assert isinstance(saved, SaveSnapshot)
engine.update_node(child, text="后续修改")
assert engine.mark_saved_snapshot(saved) and engine.is_dirty
assert engine.undo() and not engine.is_dirty
before = engine.document
with engine.preview_operation() as (candidate, touched):
    engine.add_child(child, "只试算")
    assert child in touched
assert engine.document == before

''',
    "mindmap-layouts": r'''

from mindmap_core import MindMapEngine
from mindmap_layouts import create_default_layout_registry
engine = MindMapEngine()
engine.add_child(engine.root_id, "独立布局")
result = create_default_layout_registry().layout(engine.document)
assert len(result.nodes) == 2

''',
    "mindmap-formats": r'''

from mindmap_core import MindMapDocument
from mindmap_formats import load_document, save_document, dumps, loads
document = MindMapDocument.new("独立文件格式")
save_document(document, "独立.mindmap")
assert load_document("独立.mindmap") == document
assert loads(dumps(document)) == document

''',
}


def run():
    """只安装当前成员的构建结果，独立环境不会引用源码或相邻工程。"""
    repository = Path(__file__).resolve().parents[1]
    members = workspace_members(repository)
    if set(members) != set(CHECKS):
        raise RuntimeError("工作区成员与发行验收场景不一致")
    wheels = {
        name: repository / "dist" / f"{name.replace('-', '_')}-{metadata['version']}-py3-none-any.whl"
        for name, (_, metadata) in members.items()
    }
    if not all(wheel.is_file() for wheel in wheels.values()):
        raise RuntimeError("请先运行 uv build --all-packages 构建全部成员")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("未找到 uv")
    lock = tomllib.loads((repository / "uv.lock").read_text("utf-8"))
    qt_version = next(package["version"] for package in lock["package"] if package["name"] == "pyside6")
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    temporary = Path(tempfile.mkdtemp(prefix="mindmap-wheel-check-")).resolve()
    if temporary.parent != temporary_parent or not temporary.name.startswith("mindmap-wheel-check-"):
        raise RuntimeError("临时环境路径检查失败")
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)

    def execute(arguments):
        result = subprocess.run([str(item) for item in arguments], cwd=temporary, env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=180,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise RuntimeError(f"独立安装验收失败：\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def dependencies(name):
        found, pending = {name}, [name]
        while pending:
            current = pending.pop()
            for value in members[current][1]["dependencies"]:
                dependency = re.split(r"[<>=!~ ;\[]", value)[0]
                if dependency in members and dependency not in found:
                    found.add(dependency)
                    pending.append(dependency)
        return found

    try:
        constraints = temporary / "constraints.txt"
        constraints.write_text("".join(f"{name} @ {wheel.as_uri()}\n" for name, wheel in wheels.items())
                               + f"PySide6=={qt_version}\n", "utf-8")
        independent, workbench = {}, {}
        for name, (_, metadata) in members.items():
            directory = temporary / ("env-" + name)
            execute([uv, "venv", "--python", "3.12", directory])
            interpreter = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            execute([uv, "pip", "install", "--python", interpreter, "--constraint", constraints, wheels[name]])
            closure = dependencies(name)
            forbidden = {item.replace("-", "_") for item in set(members) - closure}
            forbidden.update({"mindmap_engine", "mindmap_qt", "mindmap_qt_workspace"})
            module = name.replace("-", "_")
            prelude = ("import importlib.util, sys, json\nfrom pathlib import Path\n"
                       + f"for module in {sorted(forbidden)!r}:\n"
                       + "    assert importlib.util.find_spec(module) is None, module\n"
                       + f"import {module} as installed\n"
                       + "assert Path(installed.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())\n")
            has_qt = any(any(value.startswith("PySide6") for value in members[item][1]["dependencies"]) for item in closure)
            if has_qt and name not in {"mindmap-docking", "mindmap-shell", "mindmap-workbench"}:
                prelude += "from PySide6.QtWidgets import QApplication\napp = QApplication([])\n"
            elif not has_qt:
                prelude += 'assert importlib.util.find_spec("PySide6") is None\n'
            result = execute([interpreter, "-c", prelude + CHECKS[name]])
            independent[name] = True
            if name == "mindmap-workbench":
                workbench = json.loads(result)
            print(f"{name}：独立安装、依赖隔离及功能验收通过", flush=True)
        pins = workbench.pop("已验证运行依赖", {})
        pins = {name.lower().replace("_", "-"): version for name, version in pins.items()}
        pins.update({name: metadata["version"] for name, (_, metadata) in members.items()})
        artifacts = repository / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "release-constraints.txt").write_text(
            "# 本次通过独立安装验收的固定版本。\n"
            + "".join(f"{name}=={version}\n" for name, version in sorted(pins.items())), "utf-8")
        report = {"独立能力包": independent, "成员数量": len(members), **workbench}
        (artifacts / "wheel-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        return 0
    finally:
        # 只清理本次创建的临时根，删除前重新检查解析后的绝对路径。
        resolved = temporary.resolve()
        if resolved.parent == temporary_parent and resolved.name.startswith("mindmap-wheel-check-"):
            shutil.rmtree(resolved)


if __name__ == "__main__":
    raise SystemExit(run())
