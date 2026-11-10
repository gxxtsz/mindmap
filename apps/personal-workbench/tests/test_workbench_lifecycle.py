"""导图分屏、局部输入、插件开关和历史会话恢复验收。"""

import json
from pathlib import Path

import pytest
from mindmap_core import MindMapError
from personal_workbench.app import build_window
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QLineEdit


@pytest.fixture
def workspace(qtbot, tmp_path):
    window = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    window.show()
    yield window
    window.dispose()


def test_split_routes_commands_and_preserves_local_text_input(workspace, tmp_path, qtbot):
    window = workspace
    assert window.new_document(tmp_path / "导图.mindmap")
    mindmap = window.documents.active
    mindmap.engine.update_node(mindmap.engine.root_id, text="导图修改")
    assert window.new_document(tmp_path / "另一份导图.mindmap")
    other = window.documents.active
    other.engine.update_node(other.engine.root_id, text="另一份导图修改")
    window.host.editor.split_token(other.id, Qt.Orientation.Horizontal)
    QApplication.processEvents()
    assert {view.id for view in window.documents.ordered()} == {mindmap.id, other.id}
    assert len(window.host.editor.groups) == 2
    window.manager.activate_document(mindmap.id)
    group, index = window.host.editor.find_token(other.id)
    bar = group.tabBar()
    # 右组的标签本来就已选中，点击时仍须将活动文档及焦点切换到右侧。
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(index).center())
    assert window.documents.active is other
    assert bar.property("activeGroup")
    assert not window.host.editor.find_token(mindmap.id)[0].tabBar().property("activeGroup")
    qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert other.engine.document.root.text == "中心主题"
    assert mindmap.engine.document.root.text == "导图修改"
    window.manager.activate_document(mindmap.id)
    mindmap.canvas.view.setFocus()
    qtbot.keyClick(mindmap.canvas.view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert mindmap.engine.document.root.text == "中心主题"
    assert not mindmap.canvas.action("undo").shortcuts()
    # 面板搜索等输入框通过菜单操作时也保留本地选区，不触发导图全选或复制。
    field = QLineEdit(window)
    field.setText("局部输入")
    field.show()
    field.setFocus()
    field.selectAll()
    window.edit_commands.refresh()
    window.action("app.copy").trigger()
    assert QApplication.clipboard().text() == "局部输入"
    assert mindmap.engine.document.root.text == "中心主题"
    window.manager.activate_document(other.id)
    assert window.new_document(tmp_path / "在活动组新建.mindmap")
    assert window.host.editor.find_token(window.documents.active.id)[0] is group


def test_same_name_titles_and_full_paths_survive_split_and_restore(workspace, tmp_path, qtbot):
    window = workspace
    views = []
    for folder in ("甲", "乙"):
        path = tmp_path / folder / "项目" / "同名.mindmap"
        path.parent.mkdir(parents=True)
        assert window.new_document(path)
        views.append(window.documents.active)
    window.host.editor.split_token(views[1].id, Qt.Orientation.Horizontal)
    for restore in (False, True):
        if restore:
            window.manager.restore_layout(window.manager.capture_layout())
        for view, folder in zip(views, ("甲", "乙"), strict=True):
            window.manager.activate_document(view.id)
            group, index = window.host.editor.find_token(view.id)
            suffix = str(Path(folder) / "项目")
            assert group.tabText(index) == f"同名 · {suffix}"
            assert group.tabToolTip(index) == str(view.document.path)
            qtbot.waitUntil(lambda: suffix in window.recent_files_button.text())
            assert window.recent_files_button.toolTip() == str(view.document.path)
    assert window.manager.close_document(views[0].id)
    group, index = window.host.editor.find_token(views[1].id)
    assert group.tabText(index) == "同名"


def test_disabled_plugin_restart_preserves_document_and_panel_layout(workspace, qtbot, tmp_path):
    window = workspace
    assert window.new_document(tmp_path / "保留.mindmap")
    view_id = window.documents.active.id
    window.plugins.instance("mindmap").show_settings_panel()
    window.plugins.settings.setValue("enabled/mindmap", False)
    assert window.close()
    directory = tmp_path / "配置"
    disabled = build_window(storage_dir=directory)
    qtbot.addWidget(disabled)
    assert not disabled.providers
    assert disabled.documents.views[view_id].title.endswith("暂不可用）")
    assert disabled.close()
    # 第二次无插件启动仍保留资源与面板状态，重新启用后能够按原标识恢复。
    disabled = build_window(storage_dir=directory)
    qtbot.addWidget(disabled)
    disabled.plugins.settings.setValue("enabled/mindmap", True)
    assert disabled.close()
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    assert restored.documents.active.id == view_id
    assert restored.manager.states["map_settings"].visible


def test_missing_file_retry_failure_preserves_placeholder_record(workspace, qtbot, tmp_path):
    target = tmp_path / "暂时离线.mindmap"
    assert workspace.new_document(target)
    identifier = workspace.documents.active.id
    engine = workspace.documents.active.engine
    engine.update_node(engine.root_id, text="再次可用")
    assert workspace.close()
    original = target.read_bytes()
    target.unlink()
    restored = build_window(storage_dir=tmp_path / "配置")
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    record = restored.documents.snapshot()
    with pytest.raises(MindMapError, match="读取文档失败"):
        restored.open_path(target)
    assert restored.documents.snapshot() == record
    target.write_bytes(original)
    assert restored.open_path(target)
    assert identifier not in restored.documents.views
    assert restored.documents.active.engine.document.root.text == "再次可用"


def test_legacy_real_documents_split_pages_and_preferences_migrate_without_rewrite(workspace, qtbot, tmp_path):
    """用旧文件结构恢复真实共享导图、页面与分屏，原始三份配置逐字节保留。"""
    assert workspace.new_document(tmp_path / "历史导图.mindmap")
    first = workspace.documents.active
    child = first.engine.add_child(first.engine.root_id, "子导图入口")
    page = first.engine.create_submap(child)
    first.canvas.navigate_to(page)
    assert first.duplicate()
    second = workspace.documents.active
    second.canvas.navigate_to(None)
    workspace.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    workspace.manager.activate_document(first.id)
    QApplication.processEvents()
    records = [{"id": view.id, "path": str(view.document.path),
                "view_state": view.canvas.capture_page_view_state()}
               for view in workspace.documents.ordered()]
    layout = workspace.manager.capture_layout()
    assert workspace.close()
    saved = json.loads((tmp_path / "配置" / "workbench-state-v1.json").read_text("utf-8"))
    directory = tmp_path / "旧配置"
    directory.mkdir()
    (directory / "documents-v1.json").write_text(json.dumps({
        "schema_version": 1, "recent_files": [str(first.document.path)], "open_documents": records,
        "active_id": first.id, "recent_pages": saved["plugins"]["mindmap"]["page_history"]["recent_pages"],
    }), encoding="utf-8")
    (directory / "layouts-v1.json").write_text(json.dumps({"schema_version": 1, "session": layout}), encoding="utf-8")
    preferences = QSettings(str(directory / "preferences.ini"), QSettings.Format.IniFormat)
    preferences.setValue("view/wheel_mode", "zoom")
    preferences.sync()
    originals = {path: path.read_bytes() for path in directory.iterdir()}
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    restored.show()
    QApplication.processEvents()
    assert list(restored.documents.views) == [item["id"] for item in records]
    assert restored.documents.active.id == first.id
    assert restored.plugins.instance("mindmap").canvas.current_page_id == page and restored.plugins.instance("mindmap").canvas.wheel_mode == "zoom"
    assert restored.plugins.instance("mindmap").sessions.get(first.id).document is restored.plugins.instance("mindmap").sessions.get(second.id).document
    assert restored.host.editor.snapshot() == layout["hosts"]["main"]["editor"]
    assert restored.state.recent_files == [str(first.document.path)]
    assert restored.close()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
