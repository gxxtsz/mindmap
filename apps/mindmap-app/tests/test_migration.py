"""旧架构仅保留只读迁移入口，转换后不再维护插件式会话和配置。"""

import json
from copy import deepcopy

import pytest
from mindmap_app.app import build_window
from mindmap_desktop_runtime.storage import JsonLayoutStore
from PySide6.QtCore import QStandardPaths, Qt


@pytest.fixture
def previous_profile(qtbot, tmp_path):
    """由真实导图现场构造旧版多业务会话，确保转换覆盖分屏、偏好和来源归档。"""
    window = build_window(storage_dir=tmp_path / "准备", restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    target = tmp_path / "历史导图.mindmap"
    assert window.new_document(target)
    first = window.documents.active
    first.engine.update_node(first.engine.root_id, text="保留导图")
    assert first.duplicate()
    window.host.editor.split_token(window.documents.active.id, Qt.Orientation.Horizontal)
    window.application.show_settings_panel()
    assert window.close()
    old = json.loads((tmp_path / "准备/mindmap-state-v2.json").read_text("utf-8"))
    ids = [view["id"] for view in old["views"]]
    old["schema_version"] = 1
    old["session_checkpoint"] = True
    old["plugins"] = {"mindmap": old.pop("mindmap"), "software-structure": {"保留设置": True}}
    for view in old["views"]:
        view.update(plugin_id="mindmap", editor_type="mindmap.editor")
    source = tmp_path / "架构来源.software-structure"
    source.write_text('{"schema_version":1,"modules":[]}', encoding="utf-8")
    retired = {"id": "structure.saved", "path": str(source), "plugin_id": "software-structure",
               "editor_type": "software-structure.document", "view_state": {"zoom": 2}}
    old["views"].append(retired)
    old["active_id"] = retired["id"]
    old["recent_files"].insert(0, str(source))
    layout = old["layout"]["session"]
    for identifier in ("software-structure.public-dependencies", "software-structure.module-api"):
        layout["tools"].append({**deepcopy(layout["tools"][0]), "id": identifier})
    layout["active"] = "software-structure.module-api"
    pending = [layout["hosts"]["main"]["editor"]]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("kind") == "tabs":
                node["tokens"].extend([retired["id"], "tool:software-structure.public-dependencies"])
                break
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    directory = tmp_path / "旧配置"
    directory.mkdir()
    (directory / "workbench-state-v1.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    settings = {"schema_version": 1, "values": {"plugins": {"enabled/mindmap": False},
                "mindmap": {"view/wheel_mode": "zoom"}, "": {"window/always_on_top": False}}}
    (directory / "workbench-settings-v1.json").write_text(json.dumps(settings), encoding="utf-8")
    originals = {path: path.read_bytes() for path in (*directory.iterdir(), target, source)}
    return directory, originals, ids, retired


def test_old_profile_becomes_mindmap_session_without_modifying_sources(previous_profile, qtbot):
    directory, originals, ids, retired = previous_profile
    window = build_window(storage_dir=directory)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert set(window.documents.views) == set(ids)
    assert len(window.host.editor.groups) == 2
    assert window.manager.states["map_settings"].visible
    assert window.application.preferences.value("view/wheel_mode") == "zoom"
    assert window.documents.active.engine.document.root.text == "保留导图"
    data = window.state.data
    assert data["schema_version"] == 2 and "plugins" not in data
    assert all("plugin_id" not in view and "editor_type" not in view for view in data["views"])
    assert data["migration_archive"]["views"] == [retired]
    assert data["migration_archive"]["sources"] == [retired["path"]]
    layout_text = json.dumps(data["layout"])
    assert "software-structure." not in layout_text and retired["id"] not in layout_text
    assert "plugins" not in window.settings.values
    assert window.close()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert (directory / "mindmap-state-v2.json").is_file()
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert set(restored.documents.views) == set(ids)
    assert restored.state.data["migration_archive"] == data["migration_archive"]


def test_skip_restore_still_imports_preferences_and_archives_sources(previous_profile, qtbot):
    directory, originals, _ids, retired = previous_profile
    window = build_window(storage_dir=directory, restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert not window.documents.views
    assert window.application.preferences.value("view/wheel_mode") == "zoom"
    assert window.state.data["migration_archive"]["views"] == [retired]
    assert window.close()
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_new_profile_write_failure_does_not_touch_legacy_files(previous_profile, qtbot, monkeypatch):
    directory, originals, _ids, _retired = previous_profile
    window = build_window(storage_dir=directory)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    save = JsonLayoutStore.save

    def fail(store, payload):
        if store.path == directory / "mindmap-state-v2.json":
            raise OSError("模拟新会话写入失败")
        return save(store, payload)

    monkeypatch.setattr(JsonLayoutStore, "save", fail)
    assert not window.close()
    assert window.documents.views
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_default_directory_can_import_historical_subdirectory(previous_profile, qtbot, tmp_path, monkeypatch):
    directory, originals, ids, _retired = previous_profile
    legacy = tmp_path / "mindmap/mindmap-workbench"
    legacy.mkdir(parents=True)
    for path, raw in originals.items():
        if path.parent == directory:
            (legacy / path.name).write_bytes(raw)
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda _: str(tmp_path))
    window = build_window()
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert window.manager.storage_dir == tmp_path / "mindmap"
    assert set(window.documents.views) == set(ids)
    before = deepcopy(window.state.data["migration_archive"])
    assert window.close()
    assert (legacy / "workbench-state-v1.json").read_bytes() == originals[directory / "workbench-state-v1.json"]
    restored = build_window()
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert restored.state.data["migration_archive"] == before
