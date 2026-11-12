"""通用插件发现、失败隔离、回滚和依赖逆序释放。"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QLabel
from workbench_api import EditorProvider, PluginManifest
from workbench_api.qt import CommandSpec, MenuContribution, PanelSpec, StatusItemSpec
from workbench_runtime import WorkbenchRuntimeWindow


def test_empty_runtime_and_disabled_plugins_do_not_import_business_modules(tmp_path):
    code = """
import sys
import json
from pathlib import Path
from PySide6.QtWidgets import QApplication
from workbench_runtime import WorkbenchRuntimeWindow
app = QApplication([])
disabled = Path(sys.argv[1]) / "disabled"
disabled.mkdir()
(disabled / "workbench-settings-v1.json").write_text(json.dumps({"schema_version": 1,
    "values": {"plugins": {"enabled/mindmap": False, "enabled/software-structure": False}}}), encoding="utf-8")
window = WorkbenchRuntimeWindow(storage_dir=disabled, restore=False)
assert not window.providers and all(record.status == "已禁用" for record in window.plugins.records)
assert "workbench_plugin_mindmap.controller" not in sys.modules
assert "mindmap_core" not in sys.modules
assert "mindmap_qt_editor" not in sys.modules
assert "mindmap_qt_render" not in sys.modules
assert "workbench_plugin_software_structure.viewer" not in sys.modules
window.dispose()
window = WorkbenchRuntimeWindow(storage_dir=sys.argv[1], manifests=[], restore=False)
assert not window.providers
window.dispose()
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=40)
    assert result.returncode == 0, result.stderr


def test_plugin_registration_failure_rolls_back_and_other_plugin_starts(qtbot, tmp_path, sample_manifest):
    released = []

    def broken(context):
        context.own(lambda: released.append("已清理"))
        context.editors.register(EditorProvider("broken.editor", "示例", (".broken",), lambda p: None,
                                               lambda p, i, s: None))
        raise RuntimeError("激活失败")

    manifest = PluginManifest("broken", "失败插件", "1", broken)
    window = WorkbenchRuntimeWindow(storage_dir=tmp_path, restore=False, manifests=[manifest, sample_manifest])
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert released == ["已清理"]
    assert set(window.providers) == {"sample.editor"}
    assert window.plugins.records[0].error == "激活失败"


@pytest.mark.parametrize("kind", ["duplicate", "version", "missing", "cycle"])
def test_bad_plugin_metadata_does_not_activate(qtbot, tmp_path, kind):
    activated = []
    def activate(ctx):
        activated.append(ctx.plugin_id)
    if kind == "duplicate":
        manifests = [PluginManifest("a", "甲", "1", activate), PluginManifest("a", "乙", "1", activate)]
    elif kind == "version":
        manifests = [PluginManifest("a", "甲", "1", activate, api_version=1)]
    elif kind == "missing":
        manifests = [PluginManifest("a", "甲", "1", activate, requires=("missing",))]
    else:
        manifests = [PluginManifest("a", "甲", "1", activate, requires=("b",)),
                     PluginManifest("b", "乙", "1", activate, requires=("a",))]
    window = WorkbenchRuntimeWindow(storage_dir=tmp_path, restore=False, manifests=manifests)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert not activated
    assert all(record.status == "加载失败" for record in window.plugins.records)


def test_plugin_dependency_activation_failure_and_reverse_release(qtbot, tmp_path):
    events = []

    def activate(context):
        events.append("启动 " + context.plugin_id)
        context.own(lambda: events.append("注销 " + context.plugin_id))
        return SimpleNamespace(dispose=lambda: events.append("释放 " + context.plugin_id))

    manifests = [PluginManifest("b", "乙", "1", activate, requires=("a",)),
                 PluginManifest("a", "甲", "1", activate)]
    window = WorkbenchRuntimeWindow(storage_dir=tmp_path, restore=False, manifests=manifests)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert events == ["启动 a", "启动 b"]
    window.dispose()
    assert events == ["启动 a", "启动 b", "释放 b", "注销 b", "释放 a", "注销 a"]


def test_activation_failure_removes_ui_commands_and_blocks_dependents(qtbot, tmp_path, sample_manifest):
    calls = []

    def fail(context):
        context.commands.register(CommandSpec("broken.command", "测试动作", lambda: None, "Ctrl+Alt+J"))
        context.ui.register_panel(PanelSpec("broken.panel", "测试面板", QLabel, default_visible=True))
        context.ui.register_status(StatusItemSpec("broken.status", QLabel("状态")))
        context.ui.contribute_menu(MenuContribution("视图", lambda menu: menu.addAction("不应存在")))
        context.ui.subscribe_active(lambda view: calls.append("残留订阅"))
        raise ValueError("测试回滚")

    manifests = [PluginManifest("broken", "失败插件", "1", fail),
                 PluginManifest("dependent", "依赖插件", "1", lambda ctx: calls.append("依赖启动"),
                                requires=("broken",)), sample_manifest]
    window = WorkbenchRuntimeWindow(storage_dir=tmp_path, restore=False, manifests=manifests)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert "broken.command" not in window.manager.actions
    assert "broken.panel" not in window.manager.specs
    assert "broken.panel" not in window.default_panel_ids
    assert not window.status_items and not window.menu_contributions
    assert window.new_document(provider_id="sample.editor")
    assert calls == [] and window.plugins.records[1].status == "加载失败"


def test_panel_service_suspension_keeps_layout_and_validates_entire_batch(qtbot, tmp_path):
    """公开面板服务保留布局偏好，并在批量标识无效时保留原有动作状态。"""
    panel_id = "sample.panel"

    def activate(context):
        action = context.ui.register_panel(PanelSpec(
            panel_id, "测试面板", QLabel, shortcut="Ctrl+Alt+J", default_visible=True))
        return SimpleNamespace(dispose=lambda: None, action=action)

    manifest = PluginManifest("sample", "面板示例", "1", activate)
    window = WorkbenchRuntimeWindow(storage_dir=tmp_path, restore=False, manifests=[manifest])
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    ui = window.plugins.records[0].context.ui
    action = window.plugins.instance("sample").action
    assert not hasattr(ui, "docking")
    assert ui.panel_visible(panel_id) and action.isEnabled()

    with pytest.raises(KeyError):
        ui.set_panels_suspended((panel_id, "missing"), True)
    assert ui.panel_visible(panel_id) and action.isEnabled()

    ui.set_panels_suspended((panel_id,), True)
    assert ui.panel_visible(panel_id) and not action.isEnabled()
    assert ui.activate_panel(panel_id, focus=False) is False

    ui.set_panels_suspended((panel_id,), False)
    assert ui.panel_visible(panel_id) and action.isEnabled()
    assert ui.activate_panel(panel_id, focus=False) is True
