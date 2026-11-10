"""验收产品默认编辑器、插件开关和配置升级。"""

import json

import pytest
from personal_workbench.app import build_window
from PySide6.QtCore import QSettings, Qt
from workbench_runtime.storage import JsonLayoutStore


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.show()
    yield result
    result.dispose()


def test_mindmap_editor_registers_and_routes_view_activation(window, tmp_path):
    assert set(window.providers) == {"mindmap.editor"}
    assert [(item.id, item.status) for item in window.plugins.records] == [("mindmap", "已启用")]
    assert window.new_document(tmp_path / "导图.mindmap")
    mindmap = window.documents.active
    mindmap.engine.update_node(mindmap.engine.root_id, text="导图内容")
    assert window.new_document(tmp_path / "另一份导图.mindmap")
    other = window.documents.active
    assert mindmap.engine.get_node(mindmap.engine.root_id).text == "导图内容"
    plugin = window.plugins.instance("mindmap")
    assert plugin.active_session is other
    with pytest.raises(ValueError, match="文档尚未打开"):
        plugin.context.documents.activate_view("missing")
    assert window.documents.active is other
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
    assert not restored.providers
    assert not restored.new_document()
    assert [(item.id, item.status) for item in restored.plugins.records] == [("mindmap", "已禁用")]


@pytest.mark.parametrize("suffix", [".txt", ".MD"])
def test_retired_file_extensions_are_unsupported(window, tmp_path, suffix):
    path = tmp_path / ("已有文件" + suffix)
    path.write_text("文件内容", encoding="utf-8")
    assert not window.new_document(path)
    with pytest.raises(ValueError, match="没有启用的编辑器支持此文件"):
        window.open_path(path)
    assert not window.documents.views and path.read_text("utf-8") == "文件内容"


@pytest.fixture
def previous_profile(window, tmp_path):
    """从真实导图会话构造升级前的状态和布局，无需安装旧插件。"""
    maps = [tmp_path / name for name in ("甲.mindmap", "乙.mindmap")]
    for path in maps:
        assert window.new_document(path)
    window.host.editor.split_token(window.documents.active.id, Qt.Orientation.Horizontal)
    window.plugins.instance("mindmap").show_settings_panel()
    assert window.close()
    directory = tmp_path / "配置"
    layout_path = directory / "workbench-layout-v1.json"
    state_path = directory / "workbench-state-v1.json"
    state = json.loads(state_path.read_text("utf-8"))
    state.pop("session_checkpoint")
    layout = state["layout"]
    host = layout["session"]["hosts"]["main"]
    editor = host["editor"]
    host["editor"] = {"kind": "split", "orientation": "vertical", "sizes": [500, 250], "children": [
        editor, {"kind": "tabs", "id": "old-text-group", "current": 0, "tokens": ["text.saved"]},
    ]}
    layout_path.write_text(json.dumps(layout, ensure_ascii=False), "utf-8")

    text, history, other = [tmp_path / name for name in ("旧正文.TXT", "仅在历史里的笔记.MD", "其他插件.md")]
    for path in (text, history, other):
        path.write_text(path.stem, "utf-8")
    state.pop("product_profile_revision")
    state["views"].extend([
        {"id": "text.saved", "plugin_id": "text", "editor_type": "text.editor", "path": str(text)},
        {"id": "other.saved", "plugin_id": "other", "editor_type": "other.editor", "path": str(other)},
    ])
    state["plugins"].update(text={"cursor": 3}, other={"enabled": False})
    state["recent_files"] = [str(text), str(history), str(other), *state["recent_files"]]
    state.update(active_id="text.saved", layout=layout)
    state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    settings_path = directory / "workbench-settings-v1.json"
    settings = {"schema_version": 1, "values": {
        "plugins": {"enabled/text": False, "enabled/mindmap": True, "enabled/other": False},
        "text": {"font": "旧设置"}, "mindmap": {"view/wheel_mode": "zoom"}, "other": {"setting": 3},
    }}
    settings_path.write_text(json.dumps(settings, ensure_ascii=False), "utf-8")
    return directory, {path: path.read_bytes() for path in (*maps, text, history, other)}


def test_upgrade_removes_retired_views_settings_and_history(previous_profile, qtbot, tmp_path):
    directory, sources = previous_profile
    state_path = directory / "workbench-state-v1.json"
    settings_path = directory / "workbench-settings-v1.json"
    before = json.loads(state_path.read_text("utf-8"))
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    remaining = [view for view in before["views"] if view["plugin_id"] != "text"]
    assert set(restored.documents.views) == {view["id"] for view in remaining}
    assert restored.documents.active.id == remaining[0]["id"]
    assert len(restored.host.editor.groups) == 2
    assert restored.manager.states["map_settings"].visible
    assert restored.plugins.instance("mindmap").preferences.value("view/wheel_mode") == "zoom"
    state = json.loads(state_path.read_text("utf-8"))
    assert state["views"] == remaining and state["active_id"] == ""
    assert "text" not in state["plugins"] and state["plugins"]["other"] == {"enabled": False}
    assert state["recent_files"] == before["recent_files"][2:]
    settings = json.loads(settings_path.read_text("utf-8"))["values"]
    assert "text" not in settings and settings["other"] == {"setting": 3}
    assert settings["plugins"] == {"enabled/mindmap": True, "enabled/other": False}

    later = tmp_path / "升级后加入的历史.md"
    restored.remember_path(later)
    assert restored.close()
    assert "text.saved" not in state_path.read_text("utf-8")
    again = build_window(storage_dir=directory)
    qtbot.addWidget(again, before_close_func=lambda item: item.dispose())
    assert again.state.recent_files == [str(later), *state["recent_files"]]
    assert set(again.documents.views) == {view["id"] for view in remaining}
    assert all(path.read_bytes() == raw for path, raw in sources.items())


def test_upgrade_runs_when_restore_is_skipped(previous_profile, qtbot):
    directory, _ = previous_profile
    restored = build_window(storage_dir=directory, restore=False)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert not restored.documents.views
    state = json.loads((directory / "workbench-state-v1.json").read_text("utf-8"))
    assert {view["plugin_id"] for view in state["views"]} == {"mindmap", "other"}
    assert len(state["recent_files"]) == 3 and state["active_id"] == ""
    settings = json.loads((directory / "workbench-settings-v1.json").read_text("utf-8"))["values"]
    assert "enabled/text" not in settings["plugins"]


@pytest.mark.parametrize("filename", ["workbench-state-v1.json", "workbench-settings-v1.json"])
def test_upgrade_write_failure_keeps_original_and_uses_cleaned_memory(previous_profile, qtbot, monkeypatch, filename):
    directory, sources = previous_profile
    path = directory / filename
    before = path.read_bytes()
    original = JsonLayoutStore.save

    def fail_one_file(store, payload):
        if store.path == path:
            raise OSError("模拟配置写入失败")
        return original(store, payload)

    with monkeypatch.context() as patch:
        patch.setattr(JsonLayoutStore, "save", fail_one_file)
        restored = build_window(storage_dir=directory)
        qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
        assert "text.saved" not in restored.documents.views
        assert "text" not in restored.state.data["plugins"]
        assert restored.plugins.settings.value("enabled/text") is None
        assert path.read_bytes() == before
        assert f"启动时未能更新配置 {filename}" in restored.operation_log.plain_text()
    assert restored.close()
    assert path.read_bytes() != before
    assert all(source.read_bytes() == raw for source, raw in sources.items())


def test_legacy_files_are_read_only_and_imported_once(qtbot, tmp_path):
    directory = tmp_path / "配置"
    directory.mkdir()
    legacy = directory / "documents-v1.json"
    legacy.write_text(json.dumps({"schema_version": 1, "open_documents": [],
                                 "recent_files": [str(tmp_path / "旧文本.txt")],
                                 "active_id": "", "recent_pages": {}}), encoding="utf-8")
    ini = directory / "preferences.ini"
    preferences = QSettings(str(ini), QSettings.Format.IniFormat)
    preferences.setValue("view/wheel_mode", "zoom")
    preferences.sync()
    before = (legacy.read_bytes(), ini.read_bytes())
    window = build_window(storage_dir=directory)
    qtbot.addWidget(window)
    assert not window.state.recent_files
    assert window.plugins.instance("mindmap").preferences.value("view/wheel_mode") == "zoom"
    window.plugins.instance("mindmap").preferences.setValue("view/wheel_mode", "scroll")
    assert window.close()
    assert (legacy.read_bytes(), ini.read_bytes()) == before
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert restored.plugins.instance("mindmap").preferences.value("view/wheel_mode") == "scroll"
