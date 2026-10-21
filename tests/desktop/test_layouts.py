"""布局回归：会话迁移、默认恢复、原子验证与单主窗口内容保留。"""

import copy
import json

import pytest
from mindmap_docking import DockPosition, DockTarget, ViewMode
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPlainTextEdit


def test_snapshot_restores_mode_location_theme_and_content(docking):
    manager = docking.manager
    manager.activate("alpha", focus=False)
    content = manager.panel("alpha").content
    content.setPlainText("恢复布局时不得替换")
    manager.move("alpha", DockTarget(DockPosition.RIGHT_BOTTOM))
    manager.set_view_mode("alpha", ViewMode.UNDOCK)
    manager.set_option("compact", True)
    manager.set_theme("dark")
    snapshot = manager.capture_layout()
    manager.hide("alpha")
    manager.move("alpha", DockTarget(DockPosition.LEFT_TOP))
    manager.set_theme("light")
    manager.set_option("compact", False)
    manager.restore_layout(snapshot)
    state = manager.states["alpha"]
    assert state.position == DockPosition.RIGHT_BOTTOM
    assert state.mode == ViewMode.UNDOCK
    assert state.visible
    assert manager.theme.name == "dark"
    assert manager.settings.compact
    assert manager.panel("alpha").content is content
    assert content.toPlainText() == "恢复布局时不得替换"
    assert docking.calls["alpha"] == 1


def test_legacy_name_setting_is_removed_when_loading_saved_layout(docking):
    """旧会话中的名称开关不会影响侧栏，也不会再次写入磁盘。"""
    manager = docking.manager
    manager.save_session()
    payload = manager.layout_store.load()
    payload["session"]["settings"]["show_names"] = True
    manager.layout_store.save(payload)
    assert manager.load_session()
    for rail in (docking.host.left_rail, docking.host.right_rail):
        assert rail.width() == manager.theme.rail_width
        assert all(button.text() == "" for button in rail._buttons.values())
    assert "show_names" not in manager.capture_layout()["settings"]
    with pytest.raises(KeyError, match="未知布局选项"):
        manager.set_option("show_names", True)
    manager.save_session()
    payload = json.loads(manager.storage_file.read_text("utf-8"))
    assert "show_names" not in payload["session"]["settings"]
    assert set(payload) == {"schema_version", "session"}


@pytest.mark.parametrize("obsolete", [
    {"current_layout": "旧布局", "layouts": {"旧布局": {"schema_version": 99}}},
    {"current_layout": ["损坏的名称"], "layouts": "损坏的布局集合"},
])
def test_legacy_named_fields_are_ignored_and_removed_on_save(docking, obsolete):
    """废弃字段无论结构还是快照损坏，都不妨碍有效会话恢复。"""
    manager = docking.manager
    manager.activate("alpha", focus=False)
    manager.move("alpha", DockTarget(DockPosition.RIGHT_TOP))
    manager.set_theme("dark")
    manager.layout_store.save({"schema_version": 1, "session": manager.capture_layout(), **obsolete})
    manager.restore_default_layout()
    assert manager.load_session()
    assert manager.states["alpha"].position == DockPosition.RIGHT_TOP
    assert manager.states["alpha"].visible and manager.theme.name == "dark"
    manager.save_session()
    assert set(manager.layout_store.load()) == {"schema_version", "session"}


def test_default_shortcut_restores_independent_template(docking, qtbot):
    """真实快捷键恢复默认状态；反复修改和恢复不会污染内建模板。"""
    manager = docking.manager
    manager.activate("alpha", focus=False)
    content = manager.panel("alpha").content
    manager.set_default_layout()
    for _ in range(2):
        manager.move("alpha", DockTarget(DockPosition.RIGHT_BOTTOM))
        manager.set_option("compact", True)
        manager.set_theme("dark")
        docking.window.activateWindow()
        docking.document.setFocus()
        QApplication.processEvents()
        qtbot.keyClick(docking.document, Qt.Key.Key_F12, Qt.KeyboardModifier.ShiftModifier)
        assert manager.states["alpha"].position == DockPosition.LEFT_TOP
        assert manager.states["alpha"].visible
        assert manager.theme.name == "light" and not manager.settings.compact
        assert manager.panel("alpha").content is content


def test_default_restore_without_template_does_not_change_state(docking):
    """没有默认模板时恢复接口保持当前状态，也不触发新的保存。"""
    manager = docking.manager
    manager._default_snapshot = None
    manager.activate("alpha", focus=False)
    before = manager.capture_layout()
    manager._save_timer.stop()
    manager.restore_default_layout()
    assert manager.capture_layout() == before
    assert not manager._save_timer.isActive()


@pytest.mark.parametrize("layout", ["默认", "旧布局", None, []])
def test_restore_requires_snapshot_and_leaves_state_unchanged(docking, layout):
    """快照接口拒绝名称及其他非字典输入，在预检失败时保留当前布局。"""
    manager = docking.manager
    before = manager.capture_layout()
    with pytest.raises(ValueError, match="布局格式"):
        manager.restore_layout(layout)
    assert manager.capture_layout() == before


@pytest.mark.parametrize("damage", ["version", "duplicate", "position", "host", "geometry"])
def test_invalid_snapshot_is_rejected_before_mutation(docking, damage):
    manager = docking.manager
    manager.activate("alpha", focus=False)
    before = manager.capture_layout()
    invalid = copy.deepcopy(before)
    if damage == "version":
        invalid["schema_version"] = 99
    elif damage == "duplicate":
        invalid["tools"].append(copy.deepcopy(invalid["tools"][0]))
    elif damage == "position":
        invalid["tools"][0]["position"] = "not_a_position"
    elif damage == "host":
        invalid["tools"][0]["host_id"] = "missing_host"
    else:
        invalid["tools"][0]["geometry"] = [0, 0, -1, 300]
    with pytest.raises(ValueError):
        manager.restore_layout(invalid)
    assert manager.capture_layout() == before


def test_unknown_tool_and_document_are_ignored_on_restore(docking):
    manager = docking.manager
    snapshot = manager.capture_layout()
    unknown = copy.deepcopy(snapshot["tools"][0])
    unknown.update(id="removed_plugin", visible=True)
    snapshot["tools"].append(unknown)
    node = snapshot["hosts"]["main"]["editor"]
    while node["kind"] == "split":
        node = node["children"][0]
    node["tokens"].append("removed_document")
    manager.restore_layout(snapshot)
    assert "removed_plugin" not in manager.states
    assert set(manager.states) == {"alpha", "beta", "gamma", "peer"}
    assert docking.host.editor.find_token("removed_document") is None
    assert docking.host.editor.find_token("doc") is not None
    assert docking.document.toPlainText().startswith("def sample")


@pytest.mark.parametrize("raw", ["{broken", '{"schema_version":99}', "[]"])
def test_corrupt_session_falls_back_to_default(docking, qtbot, raw):
    manager = docking.manager
    manager.activate("alpha", focus=False)
    manager.set_default_layout()
    manager.move("alpha", DockTarget(DockPosition.RIGHT_TOP))
    manager.set_theme("dark")
    manager.storage_dir.mkdir(parents=True, exist_ok=True)
    manager.storage_file.write_text(raw, encoding="utf-8")
    with qtbot.waitSignal(manager.message, timeout=1000) as signal:
        assert manager.load_session() is False
    assert "恢复默认布局" in signal.args[0]
    assert manager.states["alpha"].position == DockPosition.LEFT_TOP
    assert manager.states["alpha"].visible
    assert manager.theme.name == "light"


def test_session_round_trip_uses_fresh_factories(system_factory, tmp_path):
    first = system_factory(tmp_path / "共享存储")
    first.manager.activate("alpha", focus=False)
    first.manager.move("alpha", DockTarget(DockPosition.BOTTOM_RIGHT))
    first.manager.set_view_mode("alpha", ViewMode.UNDOCK)
    first.manager.set_theme("dark")
    first.manager.save_session()
    first.manager.shutdown()
    second = system_factory(tmp_path / "共享存储")
    assert second.manager.load_session() is True
    assert second.manager.theme.name == "dark"
    assert second.manager.states["alpha"].position == DockPosition.BOTTOM_RIGHT
    assert second.manager.states["alpha"].mode == ViewMode.UNDOCK
    assert set(second.manager.layout_store.load()) == {"schema_version", "session"}
    assert second.calls["alpha"] == 1
    assert second.manager.panel("alpha").content is not first.manager.panel("alpha").content




def test_nested_editor_split_restores_same_documents(docking):
    manager, host = docking.manager, docking.host
    second = QPlainTextEdit("第二份文档")
    manager.add_document("second", "second.py", second)
    host.editor.split_token("second", Qt.Orientation.Horizontal)
    first_group = host.editor.find_token("doc")[0]
    second_group = host.editor.find_token("second")[0]
    assert first_group is not second_group
    snapshot = manager.capture_layout()
    manager.move_document("second", "main", first_group.group_id)
    manager.restore_layout(snapshot)
    assert host.editor.find_token("doc")[0] is not host.editor.find_token("second")[0]
    assert manager.documents["second"]["widget"] is second
    assert second.toPlainText() == "第二份文档"
