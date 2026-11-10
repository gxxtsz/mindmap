"""用两种真实编辑器验收插件边界、文件生命周期和历史数据只读迁移。"""

import json

import pytest
from personal_workbench.app import build_window
from PySide6.QtCore import QSettings
from workbench_plugin_text import manifest as text_manifest


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.show()
    yield result
    result.dispose()


def test_two_plugins_register_and_route_mixed_editors(window, tmp_path):
    assert set(window.providers) == {"mindmap.editor", "text.editor"}
    assert all(item.status == "已启用" for item in window.plugins.records)
    assert window.new_document(tmp_path / "导图.mindmap")
    mindmap = window.documents.active
    mindmap.engine.update_node(mindmap.engine.root_id, text="导图内容")
    assert window.new_document(provider_id="text.editor")
    text = window.documents.active
    text.widget.insertPlainText("独立文本")
    assert mindmap.engine.get_node(mindmap.engine.root_id).text == "导图内容"
    assert text.document.is_dirty
    assert window.plugins.instance("mindmap").active_session is None
    assert window.manager.is_tool_available("properties") is False
    plugin = window.plugins.instance("mindmap")
    with pytest.raises(ValueError, match="文档尚未打开"):
        plugin.context.documents.activate_view("missing")
    assert window.documents.active is text
    plugin.context.documents.activate_view(mindmap.id)
    group, index = window.host.editor.find_token(mindmap.id)
    assert group.currentIndex() == index
    assert window.documents.active is mindmap and plugin.active_session is mindmap
    assert window.manager.is_tool_available("properties")
    assert window.action("app.save").isEnabled()


def test_plugin_enable_switch_is_persisted_for_next_start(window, qtbot, tmp_path):
    window.plugins.settings.setValue("enabled/mindmap", False)
    window.plugins.settings.sync()
    assert "mindmap.editor" in window.providers
    window.dispose()
    restored = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert set(restored.providers) == {"text.editor"}
    assert restored.new_document(provider_id="text.editor")


def test_mixed_session_restores_with_missing_plugin_placeholder(window, qtbot, tmp_path):
    path = tmp_path / "文本.txt"
    path.write_text("文本内容", encoding="utf-8")
    assert window.new_document(tmp_path / "导图.mindmap")
    mindmap_id = window.documents.active.id
    assert window.open_path(path)
    text_id = window.documents.active.id
    assert window.close()
    restored = build_window(storage_dir=tmp_path / "配置", manifests=[text_manifest])
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert set(restored.documents.views) == {mindmap_id, text_id}
    assert "暂不可用" in restored.documents.views[mindmap_id].title
    assert restored.documents.active.id == text_id
    assert restored.documents.active.widget.toPlainText() == "文本内容"


def test_legacy_files_are_read_only_and_imported_once(qtbot, tmp_path):
    directory = tmp_path / "配置"
    directory.mkdir()
    legacy = directory / "documents-v1.json"
    legacy.write_text(json.dumps({"schema_version": 1, "open_documents": [], "recent_files": [],
                                 "active_id": "", "recent_pages": {}}), encoding="utf-8")
    ini = directory / "preferences.ini"
    preferences = QSettings(str(ini), QSettings.Format.IniFormat)
    preferences.setValue("view/wheel_mode", "zoom")
    preferences.sync()
    before = (legacy.read_bytes(), ini.read_bytes())
    window = build_window(storage_dir=directory)
    qtbot.addWidget(window)
    assert window.plugins.instance("mindmap").preferences.value("view/wheel_mode") == "zoom"
    window.plugins.instance("mindmap").preferences.setValue("view/wheel_mode", "scroll")
    assert window.close()
    assert (legacy.read_bytes(), ini.read_bytes()) == before
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert restored.plugins.instance("mindmap").preferences.value("view/wheel_mode") == "scroll"
