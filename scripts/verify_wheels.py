"""在仓库外逐个安装工作区 wheel，验证显式依赖、资源及桌面完整流程。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZipFile

from workspace_members import workspace_members

# 业务验收按包名声明，真实成员位置与版本均由根工作区元数据发现。
CHECKS = {
    "workbench-api": r'''
from workbench_api import OperationResult, PluginManifest, API_VERSION
assert OperationResult() and not OperationResult.cancelled()
assert PluginManifest("sample", "示例", "1", lambda context: None).api_version == API_VERSION
''',
    "workbench-runtime": r'''
from workbench_runtime import WorkbenchRuntimeWindow
window = WorkbenchRuntimeWindow(storage_dir=Path("runtime-state"), manifests=[], restore=False)
assert not window.providers
assert "mindmap_core" not in sys.modules and "mindmap_qt_canvas" not in sys.modules
window.dispose()
''',
    "workbench-plugin-mindmap": r'''
from importlib.metadata import entry_points
from workbench_plugin_mindmap import manifest
assert manifest.id == "mindmap"
assert "mindmap_qt_canvas" not in sys.modules
assert any(item.name == "mindmap" for item in entry_points(group="personal_workbench.plugins"))
''',
    "workbench-plugin-text": r'''
from importlib.metadata import entry_points
from workbench_plugin_text import manifest
assert manifest.id == "text"
assert any(item.name == "text" for item in entry_points(group="personal_workbench.plugins"))
assert "workbench_runtime" not in sys.modules
''',
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
canvas.select_nodes([node])
page = canvas.create_submap()
assert canvas.current_page_id == page
canvas.engine.add_child(canvas.document_view.root_id, "安装后的子导图内容")
assert files.save()
canvas.return_to_parent_page()
assert canvas.current_page_id is None
canvas.enter_submap(node)
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
    "workbench-docking": r'''

import hashlib, importlib.util, json, sys
from pathlib import Path
import workbench_docking
from workbench_docking import DockHost, DockManager, DocumentSpec
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
assert importlib.util.find_spec("workbench_shell") is None
assert importlib.util.find_spec("mindmap_engine") is None
assert importlib.util.find_spec("mindmap_qt") is None
assert Path(workbench_docking.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
resources = Path(workbench_docking.__file__).parent / "resources"
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
    "workbench-shell": r'''

import importlib.util, json, sys
from pathlib import Path
import workbench_shell
from workbench_docking import DocumentSpec
from workbench_shell import AppSpec, WorkbenchWindow
from PySide6.QtWidgets import QApplication, QLabel
assert importlib.util.find_spec("mindmap_engine") is None
assert importlib.util.find_spec("mindmap_qt") is None
assert importlib.util.find_spec("mindmap_workbench") is None
assert Path(workbench_shell.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
app = QApplication([])
window = WorkbenchWindow(AppSpec("shell-check", "安装外壳验收", storage_dir=Path("shell-state")))
window.manager.register_document(DocumentSpec("document", "测试文档", lambda: QLabel("内容")))
window.show()
app.processEvents()
assert window.spec.title == "安装外壳验收" and len(window.manager.documents) == 1
window.manager.set_theme("dark")
assert window.close()
print(json.dumps({"外壳包独立安装与依赖隔离": True}))

''',
    "personal-workbench": r'''

import importlib.util, json, sys
from importlib.metadata import distributions
from pathlib import Path
import workbench_docking, workbench_shell, personal_workbench, mindmap_core
import mindmap_qt_canvas, mindmap_qt_panels, mindmap_qt_ui, mindmap_qt_render
assert importlib.util.find_spec("mindmap_qt") is None
assert importlib.util.find_spec("mindmap_qt_workspace") is None
from workbench_docking import DockPosition, DockTarget
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from personal_workbench.app import build_window
from mindmap_formats import load_document
app = QApplication([])
window = build_window(storage_dir=Path("workbench-state").absolute(), restore=False)
assert window.plugins.instance("mindmap").canvas is None and not len(window.plugins.instance("mindmap").sessions)
assert window.new_document(Path("workbench.mindmap").absolute())
primary = window.plugins.instance("mindmap").active_session
window.show()
app.processEvents()
assert window.plugins.instance("mindmap").panels.properties.fields["style.font_size"].isVisible()
original = window.plugins.instance("mindmap").engine.document.root.text
window.plugins.instance("mindmap").engine.update_node(window.plugins.instance("mindmap").engine.root_id, text="安装包工作台")
assert window.plugins.instance("mindmap").engine.undo() and window.plugins.instance("mindmap").engine.document.root.text == original
assert window.plugins.instance("mindmap").engine.redo() and window.plugins.instance("mindmap").engine.document.root.text == "安装包工作台"
window.plugins.instance("mindmap").panels.properties.fields["style.font_size"].setValue(23)
window.plugins.instance("mindmap").panels.flush_pending_edits()
node = window.plugins.instance("mindmap").engine.add_child(window.plugins.instance("mindmap").engine.root_id, "完整流程")
window.plugins.instance("mindmap").canvas.select_nodes([node])
window.plugins.instance("mindmap").panels.properties.fields["style.font_size"].setValue(24)
window.plugins.instance("mindmap").panels.flush_pending_edits()
assert window.plugins.instance("mindmap").engine.get_node(node).style["font_size"] == 24
window.manager.move("outline", DockTarget(DockPosition.BOTTOM_LEFT))
window.manager.activate("outline", focus=False)
assert window.manager.states["outline"].position == DockPosition.BOTTOM_LEFT
image = QImage(64, 40, QImage.Format.Format_ARGB32)
image.fill(QColor("#ee8044"))
buffer = QBuffer()
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
assert image.save(buffer, "PNG")
window.plugins.instance("mindmap").engine.attach_image(node, bytes(buffer.data()))
window.plugins.instance("mindmap").canvas.select_nodes([window.plugins.instance("mindmap").engine.root_id])
window.plugins.instance("mindmap").canvas.set_branch_style("round")
window.plugins.instance("mindmap").canvas.select_nodes([node])
window.plugins.instance("mindmap").canvas.set_branch_style("arrow2", scope="level")
assert window.save()
assert load_document(window.plugins.instance("mindmap").files.path) == window.plugins.instance("mindmap").engine.document
assert window.plugins.instance("mindmap").engine.document.root.style["font_size"] == 23
assert window.open_path(window.plugins.instance("mindmap").files.path)
before = window.plugins.instance("mindmap").engine.document
window.manager.set_theme("dark")
assert window.plugins.instance("mindmap").engine.document == before
for suffix in ("png", "svg", "pdf"):
    target = Path("workbench." + suffix)
    window.plugins.instance("mindmap").canvas.export(target)
    assert target.stat().st_size > 500
for module in (workbench_docking, workbench_shell, personal_workbench, mindmap_core,
               mindmap_qt_canvas, mindmap_qt_panels, mindmap_qt_ui, mindmap_qt_render):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert window.new_document(Path("second.mindmap").absolute())
secondary = window.plugins.instance("mindmap").active_session
secondary.engine.update_node(secondary.engine.root_id, text="独立保存")
assert window.save_all() and not secondary.files.is_dirty
assert load_document(secondary.files.path).root.text == "独立保存"
assert window.plugins.instance("mindmap").files.format_filters("open") == "思维导图 (*.mindmap)"
assert window.plugins.instance("mindmap").files.format_filters("save") == "思维导图 (*.mindmap)"
unsupported = Path("unsupported.json").absolute()
assert not window.new_document(unsupported) and not unsupported.exists()
# 安装后的保存边界同样拒绝手工绑定的错误后缀，不调用可注入写入器。
original_path = secondary.files.path
secondary.files.path = unsupported
assert not window.save() and not unsupported.exists()
secondary.files.path = original_path
try:
    window.open_path(unsupported)
except ValueError as error:
    assert "没有启用的编辑器支持" in str(error)
else:
    raise AssertionError("安装后的工作台不能打开非原生导图")
assert window.plugins.instance("mindmap").active_session is secondary
assert window.open_path(primary.files.path) and window.plugins.instance("mindmap").active_session is primary
assert len(window.plugins.instance("mindmap").sessions) == 2
window.manager.restore_default_layout()
assert len(window.plugins.instance("mindmap").sessions) == 2 and window.plugins.instance("mindmap").active_session is primary
owner = primary.engine.add_child(primary.engine.root_id, "发行子导图入口")
primary.canvas.select_nodes([owner])
page = primary.canvas.create_submap()
key = primary.engine.add_child(primary.canvas.document_view.root_id, "发行子导图内容")
primary.canvas.select_nodes([key])
assert [(entry.page_id, entry.node_id) for entry in primary.canvas.path_navigation.entries] == [
    (None, primary.engine.root_id), (page, primary.canvas.document_view.root_id)]
assert all(entry.page_root for entry in primary.canvas.path_navigation.entries)
assert window.save_all()
assert page in load_document(primary.files.path).submaps
primary.canvas.return_to_parent_page()
assert primary.canvas.current_page_id is None
window.close()
assert primary.disposed and secondary.disposed and not len(window.plugins.instance("mindmap").sessions)
from personal_workbench.app import build_window
window = build_window(storage_dir=Path("personal-state"), restore=False)
assert set(window.providers) == {"mindmap.editor", "text.editor"}
assert window.new_document(Path("personal.mindmap").absolute())
path = Path("text.txt").absolute()
path.write_text("独立安装文本", "utf-8")
assert window.open_path(path)
assert window.documents.active.widget.toPlainText() == "独立安装文本"
assert window.documents.active.duplicate()
assert window.save_all()
assert window.close()
print(json.dumps({"混合编辑器独立运行": True, "工作台独立安装与框架依赖解析": True, "工作台文件与导出": True, "多导图与自动保存边界": True,
                  "仅打开和保存原生导图": True, "子导图与页面路径": True,
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
page = engine.create_submap(child)
root = engine.page_document(page).root_id
engine.update_node(root, text="入口根节点同步")
assert engine.get_node(child).text == "入口根节点同步"
engine.document.validate()
assert engine.undo()
engine.remove_submap(child)
assert engine.get_node(child).submap_id is None
assert engine.undo() and page in engine.document.submaps

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

from mindmap_core import MindMapDocument, MindMapEngine
from mindmap_formats import load_document, save_document, dumps, loads
document = MindMapDocument.new("独立文件格式")
engine = MindMapEngine(document)
page = engine.create_submap(engine.root_id)
engine.add_child(engine.page_document(page).root_id, "独立页面保存")
document = engine.document
save_document(document, "独立.mindmap")
assert load_document("独立.mindmap") == document
assert loads(dumps(document)) == document

''',
}


def verify_archive_contents(repository: Path, members: dict, wheels: dict) -> dict:
    """检查实际发行文件与当前源码一致，防止目录迁移后误用旧构建产物。

    每个 wheel 的 Python 模块必须恰好来自该成员自己的 src 目录，逐字节比较，
    因而既能发现漏包和多包，也能发现仍沿用旧位置或旧内容的构建结果。
    导图适配包的源码发行包另行检查：同组业务成员通过依赖安装，不能重复发行。
    """
    # uv build 不会自动删除已移除成员的旧包。发行目录必须与当前清单一致，
    # 防止发布时用通配符把旧引擎、旧桌面包或其他历史产物重新安装进用户环境。
    expected = {path.name for path in wheels.values()} | {
        f"{name.replace('-', '_')}-{metadata['version']}.tar.gz" for name, (_, metadata) in members.items()
    }
    dist = repository / "dist"
    actual = {path.name for pattern in ("*.whl", "*.tar.gz") for path in dist.glob(pattern)}
    if actual != expected:
        raise RuntimeError(f"发行目录与当前成员不一致：缺失 {sorted(expected - actual)}，过期 {sorted(actual - expected)}")
    module_counts = {}
    for name, (directory, _) in members.items():
        source_root = directory / "src"
        source = {file.relative_to(source_root).as_posix(): file.read_bytes()
                  for file in source_root.rglob("*.py")}
        with ZipFile(wheels[name]) as archive:
            modules = [entry for entry in archive.infolist() if entry.filename.endswith(".py")]
            packed = {entry.filename: archive.read(entry) for entry in modules}
        if len(packed) != len(modules):
            raise RuntimeError(f"{name} 的 wheel 存在重复 Python 模块")
        missing = sorted(source.keys() - packed.keys())
        extra = sorted(packed.keys() - source.keys())
        changed = sorted(path for path in source.keys() & packed.keys() if source[path] != packed[path])
        if missing or extra or changed:
            raise RuntimeError(f"{name} 的 wheel 与源码不一致：缺失 {missing}，多余 {extra}，内容变化 {changed}")
        module_counts[name] = len(source)

    _, metadata = members["workbench-plugin-mindmap"]
    sdist = repository / "dist" / f"workbench_plugin_mindmap-{metadata['version']}.tar.gz"
    if not sdist.is_file():
        raise RuntimeError("缺少导图插件源码发行包，请运行 uv build --all-packages")
    with tarfile.open(sdist, "r:gz") as archive:
        # 源码发行包首层是名称与版本；下一层只允许本包源码、测试和发行元数据。
        # Hatch 会自动附带 .gitignore；它属于构建元数据，不包含业务实现。
        # 额外限制源码命名空间，防止把同组业务包复制到 src 后绕过目录检查。
        allowed = {"src", "tests", "README.md", "LICENSE", "pyproject.toml", "PKG-INFO", ".gitignore"}
        unexpected = [entry.name for entry in archive.getmembers()
                      if len(parts := Path(entry.name).parts) > 1
                      and (parts[1] not in allowed or
                           (parts[1] == "src" and len(parts) > 2 and parts[2] != "workbench_plugin_mindmap"))]
    if unexpected:
        raise RuntimeError(f"导图适配包收录了其他成员的内容：{unexpected}")
    print(f"发行包内容检查通过：{len(module_counts)} 个 wheel，{sum(module_counts.values())} 个 Python 模块", flush=True)
    return {"发行目录仅含当前成员": True, "源码与wheel一致": True,
            "逐包源码模块数": module_counts, "导图源码包仅含适配包": True}


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
    archive_report = verify_archive_contents(repository, members, wheels)
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
            forbidden.update({"mindmap_engine", "mindmap_qt", "mindmap_qt_workspace", "mindmap_workbench",
                              "mindmap_docking", "mindmap_shell"})
            module = name.replace("-", "_")
            prelude = ("import importlib.util, sys, json\nfrom pathlib import Path\n"
                       + f"for module in {sorted(forbidden)!r}:\n"
                       + "    assert importlib.util.find_spec(module) is None, module\n"
                       + f"import {module} as installed\n"
                       + "assert Path(installed.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())\n")
            has_qt = any(any(value.startswith("PySide6") for value in members[item][1]["dependencies"]) for item in closure)
            if has_qt and name not in {"workbench-docking", "workbench-shell", "personal-workbench"}:
                prelude += "from PySide6.QtWidgets import QApplication\napp = QApplication([])\n"
            elif not has_qt:
                prelude += 'assert importlib.util.find_spec("PySide6") is None\n'
            result = execute([interpreter, "-c", prelude + CHECKS[name]])
            if name == "workbench-runtime":
                # 真实最小安装组合：安装平台和文本插件后，导图包仍不可导入。
                execute([uv, "pip", "install", "--python", interpreter, "--constraint", constraints,
                         wheels["workbench-plugin-text"]])
                execute([interpreter, "-c", r'''
import importlib.util
from pathlib import Path
from PySide6.QtWidgets import QApplication
from workbench_runtime import WorkbenchRuntimeWindow
app = QApplication([])
assert importlib.util.find_spec("mindmap_core") is None
assert importlib.util.find_spec("workbench_plugin_mindmap") is None
window = WorkbenchRuntimeWindow(storage_dir=Path("text-only-state"), restore=False)
assert set(window.providers) == {"text.editor"}
path = Path("text-only.txt").absolute()
path.write_text("最小安装组合", encoding="utf-8")
assert window.open_path(path)
view = window.documents.active
view.widget.insertPlainText("文本编辑：")
assert view.duplicate()
assert window.save_all()
assert path.read_text("utf-8") == "文本编辑：最小安装组合"
assert window.close()
'''])
            independent[name] = True
            if name == "personal-workbench":
                workbench = json.loads(result)
                # 同时验证模块入口与已安装命令，旧命令不能因残留构建产物继续出现。
                execute([interpreter, "-m", "personal_workbench", "--help"])
                command = interpreter.parent / ("personal-workbench.exe" if os.name == "nt" else "personal-workbench")
                assert command.is_file() and not list(interpreter.parent.glob("mindmap-workbench*"))
                execute([command, "--help"])
            print(f"{name}：独立安装、依赖隔离及功能验收通过", flush=True)
        pins = workbench.pop("已验证运行依赖", {})
        pins = {name.lower().replace("_", "-"): version for name, version in pins.items()}
        pins.update({name: metadata["version"] for name, (_, metadata) in members.items()})
        artifacts = repository / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "release-constraints.txt").write_text(
            "# 本次通过独立安装验收的固定版本。\n"
            + "".join(f"{name}=={version}\n" for name, version in sorted(pins.items())), "utf-8")
        report = {"独立能力包": independent, "成员数量": len(members), **archive_report, **workbench}
        (artifacts / "wheel-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        return 0
    finally:
        # 只清理本次创建的临时根，删除前重新检查解析后的绝对路径。
        resolved = temporary.resolve()
        if resolved.parent == temporary_parent and resolved.name.startswith("mindmap-wheel-check-"):
            # PySide6 的 QML 附带文件可能超过 Windows 的传统路径长度限制。
            # 先检查逻辑绝对路径仍是本次临时根，再添加仅改变路径解析方式的长路径前缀。
            delete_target = str(resolved)
            if os.name == "nt":
                if delete_target.startswith("\\\\"):
                    delete_target = "\\\\?\\UNC\\" + delete_target[2:]
                else:
                    delete_target = "\\\\?\\" + delete_target
            shutil.rmtree(delete_target)


if __name__ == "__main__":
    raise SystemExit(run())
