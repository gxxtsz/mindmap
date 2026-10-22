"""最近文件、分屏重开及正常退出记录的行为验收。"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from mindmap_core import MindMapDocument, MindMapError
from mindmap_formats import load_document, save_document
from mindmap_workbench.document_state import RECENT_FILE_LIMIT, DocumentState, path_key
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog


@pytest.fixture
def history_window(qtbot, tmp_path):
    """所有文件和配置均隔离；保存失败测试在退出前恢复正常写入器。"""
    window = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()
    yield window
    window.close()


def create(window, path):
    assert window.new_document(path)
    return window.active_session


def state_payload(window):
    return json.loads(window.document_state.store.path.read_text("utf-8"))


def test_recent_limit_deduplication_and_history_writes_preserve_exit_snapshot(tmp_path):
    """历史单独更新时不覆盖上次退出集合，超限淘汰最早文件并支持跨实例读取。"""
    state = DocumentState(tmp_path / "documents-v1.json")
    first = {"id": "mindmap." + "a" * 32, "path": str(tmp_path / "退出时的导图.mindmap")}
    state.save_session([first], first["id"])
    paths = [tmp_path / f"最近 {index}.mindmap" for index in range(RECENT_FILE_LIMIT + 3)]
    for path in paths:
        state.remember(path)
    assert state.recent_files == [str(path) for path in reversed(paths[-RECENT_FILE_LIMIT:])]
    state.remember(paths[-2].parent / "." / paths[-2].name)
    assert state.recent_files[0] == str(paths[-2])
    assert len(state.recent_files) == RECENT_FILE_LIMIT
    restored = DocumentState(state.store.path)
    restored.load()
    assert restored.recent_files == state.recent_files
    assert restored.open_documents == [first] and restored.active_id == first["id"]
    restored.clear_recent()
    loaded = json.loads(state.store.path.read_text("utf-8"))
    assert loaded["recent_files"] == []
    assert loaded["open_documents"] == [first] and loaded["active_id"] == first["id"]


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 使用不区分大小写的路径键")
def test_windows_history_deduplicates_path_case(tmp_path):
    state = DocumentState(tmp_path / "documents-v1.json")
    source = tmp_path / "Case.mindmap"
    state.remember(source)
    state.remember(str(source).upper())
    assert len(state.recent_files) == 1
    assert path_key(state.recent_files[0]) == path_key(source)


def test_state_atomic_write_failure_keeps_previous_file(tmp_path, monkeypatch):
    """替换失败时旧 JSON 保持完整，临时文件也会清理。"""
    state = DocumentState(tmp_path / "documents-v1.json")
    state.remember(tmp_path / "甲.mindmap")
    original = state.store.path.read_bytes()

    def fail(*args):
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr("mindmap_docking.storage.os.replace", fail)
    with pytest.raises(OSError, match="替换失败"):
        state.remember(tmp_path / "乙.mindmap")
    assert state.store.path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [state.store.path]


@pytest.mark.parametrize("invalid", [
    {"schema_version": 2, "recent_files": [], "open_documents": []},
    {"schema_version": 1, "recent_files": "错误类型", "open_documents": []},
    {"schema_version": 1, "recent_files": ["relative.mindmap"], "open_documents": []},
    {"schema_version": 1, "recent_files": [], "open_documents": [{"id": "tool:错误", "path": "错误"}]},
])
def test_invalid_state_does_not_install_partial_records(tmp_path, invalid):
    state = DocumentState(tmp_path / "documents-v1.json")
    state.store.path.write_text(json.dumps(invalid), "utf-8")
    with pytest.raises(ValueError):
        state.load()
    assert not state.recent_files and not state.open_documents and not state.active_id


def test_file_operations_update_history_but_switching_and_autosave_do_not(history_window, tmp_path):
    window = history_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    recent = [str(second.files.path), str(first.files.path)]
    assert state_payload(window)["recent_files"] == recent
    window.manager.activate_document(first.id)
    first.engine.update_node(first.engine.root_id, text="普通编辑")
    first.timer.stop()
    assert first.autosave()
    assert state_payload(window)["recent_files"] == recent
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    location = window.host.editor.find_token(first.id)
    assert window.open_path(first.files.path)
    assert window.active_session is first and window.host.editor.find_token(first.id) == location
    assert first.engine.can_undo
    assert window.document_state.recent_files == list(reversed(recent))
    assert state_payload(window)["open_documents"] == []


def test_failed_or_cancelled_file_operations_do_not_pollute_history(history_window, tmp_path, monkeypatch):
    window = history_window
    create(window, tmp_path / "原文件.mindmap")
    original = window.document_state.store.path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    assert not window.open_dialog()
    assert not window.new_document()
    assert not window.save(save_as=True)
    assert not window.new_document(tmp_path / "不存在的目录" / "失败.mindmap")
    invalid = tmp_path / "损坏.mindmap"
    invalid.write_text("不是导图", "utf-8")
    for path in (tmp_path / "不存在.mindmap", invalid, tmp_path / "其他.json"):
        with pytest.raises((MindMapError, OSError, ValueError)):
            window.open_path(path)
    assert window.document_state.store.path.read_bytes() == original
    assert len(window.sessions) == 1


def test_save_as_updates_history_and_restored_path(history_window, tmp_path, monkeypatch, qtbot):
    window = history_window
    first = create(window, tmp_path / "原文件.mindmap")
    second = create(window, tmp_path / "其他.mindmap")
    window.manager.activate_document(first.id)
    target = tmp_path / "另存的新文件.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    # 直接使用文件控制器也必须被路径变更监听捕获。
    assert first.files.save(save_as=True)
    assert window.document_state.recent_files == [str(target), str(second.files.path), str(tmp_path / "原文件.mindmap")]
    assert window.recent_files_button.toolTip() == str(target)
    storage, identifier = window.manager.storage_dir, first.id
    assert window.close()
    restored = build_window(storage_dir=storage)
    qtbot.addWidget(restored)
    assert restored.active_session.id == identifier and restored.files.path == target
    assert restored.close()


def test_successful_same_path_save_as_moves_file_to_front(history_window, tmp_path, monkeypatch):
    window = history_window
    first = create(window, tmp_path / "甲.mindmap")
    create(window, tmp_path / "乙.mindmap")
    window.manager.activate_document(first.id)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(first.files.path), ""))
    assert window.save(save_as=True)
    assert window.document_state.recent_files[0] == str(first.files.path)


def test_history_write_failure_does_not_fail_opening(history_window, tmp_path, monkeypatch):
    window = history_window

    def fail(payload):
        raise OSError("配置目录只读")

    monkeypatch.setattr(window.document_state.store, "save", fail)
    session = create(window, tmp_path / "正常文件.mindmap")
    assert session.files.path.exists() and len(window.sessions) == 1
    assert "最近文件未能保存" in window.status.currentMessage()


def test_restart_restores_tab_order_split_positions_and_active_document(history_window, tmp_path, qtbot):
    window = history_window
    first, second, third, fourth = [create(window, tmp_path / f"{name}.mindmap") for name in "甲乙丙丁"]
    window.host.editor.split_token(third.id, Qt.Orientation.Horizontal)
    right, _ = window.host.editor.find_token(third.id)
    window.manager.move_document(fourth.id, "main", right.group_id)
    left, _ = window.host.editor.find_token(first.id)
    left.tabBar().moveTab(1, 0)
    window.manager.activate_document(fourth.id)
    window.manager.activate_document(first.id)
    first.engine.update_node(first.engine.root_id, text="退出时提交的修改")
    first.timer.stop()
    locations = {item.id: (window.host.editor.find_token(item.id)[0].group_id,
                           window.host.editor.find_token(item.id)[1]) for item in window.sessions.values()}
    selections = {group.group_id: group.tabBar().tabData(group.currentIndex()) for group in window.host.editor.groups}
    ordered = [item.id for item in window.sessions.ordered()]
    recent = list(window.document_state.recent_files)
    storage = window.manager.storage_dir
    assert window.close()
    payload = state_payload(window)
    assert [item["id"] for item in payload["open_documents"]] == ordered
    assert payload["active_id"] == first.id
    assert "甲.mindmap" not in window.manager.storage_file.read_text("utf-8")
    saved_bytes = (tmp_path / "甲.mindmap").read_bytes()
    restored = build_window(storage_dir=storage)
    qtbot.addWidget(restored)
    restored.show()
    QApplication.processEvents()
    assert [item.id for item in restored.sessions.ordered()] == ordered
    for identifier, location in locations.items():
        group, index = restored.host.editor.find_token(identifier)
        assert (group.group_id, index) == location
    assert {group.group_id: group.tabBar().tabData(group.currentIndex()) for group in restored.host.editor.groups} == selections
    assert restored.active_session.id == first.id
    assert restored._properties_stack.currentWidget() is restored.panels.properties
    assert restored.recent_files_button.toolTip() == str(first.files.path)
    assert restored.engine.document.root.text == "退出时提交的修改"
    assert restored.document_state.recent_files == recent
    assert not restored.engine.is_dirty and not restored.engine.can_undo
    assert (tmp_path / "甲.mindmap").read_bytes() == saved_bytes
    assert restored.close()


def test_closed_tabs_stay_closed_and_clear_history_keeps_startup_session(history_window, tmp_path, qtbot):
    window = history_window
    first = create(window, tmp_path / "关闭的导图.mindmap")
    second = create(window, tmp_path / "保留的导图.mindmap")
    assert window.manager.close_document(first.id)
    window.clear_recent_files()
    assert len(window.sessions) == 1 and window.active_session is second
    storage = window.manager.storage_dir
    assert window.close()
    original = window.document_state.store.path.read_bytes()
    window.close()
    window.dispose()
    assert window.document_state.store.path.read_bytes() == original
    restored = build_window(storage_dir=storage)
    qtbot.addWidget(restored)
    assert [item.id for item in restored.sessions.values()] == [second.id]
    assert restored.document_state.recent_files == []
    assert restored.manager.close_document(second.id)
    assert restored.close()
    empty = build_window(storage_dir=storage)
    qtbot.addWidget(empty)
    assert not empty.sessions and empty.active_session is None
    assert empty.recent_files_button.text() == "最近文件"
    assert empty.close()


def test_rejected_close_does_not_commit_exit_snapshot(history_window, tmp_path):
    window = history_window
    session = create(window, tmp_path / "退出失败.mindmap")
    original = window.document_state.store.path.read_bytes()
    session.engine.update_node(session.engine.root_id, text="尚未保存的修改")
    session.timer.stop()

    def fail(*args, **kwargs):
        raise OSError("模拟退出保存失败")

    session.files.writer = fail
    try:
        assert not window.close()
        assert window.document_state.store.path.read_bytes() == original
        assert not session.disposed and not window._disposed
    finally:
        session.files.writer = session._write


@pytest.mark.parametrize("all_failed", [False, True])
def test_restore_skips_unreadable_files_and_preserves_recent_history(history_window, tmp_path, qtbot, all_failed):
    window = history_window
    first, corrupt, missing = [create(window, tmp_path / f"{name}.mindmap") for name in ("正常", "损坏", "丢失")]
    recent = list(window.document_state.recent_files)
    storage = window.manager.storage_dir
    assert window.close()
    corrupt.files.path.write_text("损坏的内容", "utf-8")
    missing.files.path.unlink()
    if all_failed:
        first.files.path.unlink()
    restored = build_window(storage_dir=storage)
    qtbot.addWidget(restored)
    assert restored.document_state.recent_files == recent
    assert "部分导图未能恢复" in restored.status.currentMessage()
    assert "损坏.mindmap" in restored.status.currentMessage() and "丢失.mindmap" in restored.status.currentMessage()
    assert len(restored.sessions) == (0 if all_failed else 1)
    if all_failed:
        assert restored.active_session is None and restored.host.right.isHidden()
    else:
        assert restored.active_session.id == first.id
    assert restored.close()


@pytest.mark.parametrize("corrupt", ["documents", "layout"])
def test_document_records_and_layout_recover_independently(history_window, tmp_path, qtbot, corrupt):
    window = history_window
    first = create(window, tmp_path / "甲.mindmap")
    window.manager.set_theme("dark")
    storage = window.manager.storage_dir
    assert window.close()
    invalid = window.document_state.store.path if corrupt == "documents" else window.manager.storage_file
    invalid.write_text("损坏的 JSON", "utf-8")
    restored = build_window(storage_dir=storage)
    qtbot.addWidget(restored)
    if corrupt == "documents":
        assert restored.active_session is None and restored.document_state.recent_files == []
        assert restored.manager.theme.name == "dark"
        assert "退出会话记录无效" in restored.status.currentMessage()
    else:
        assert restored.active_session.id == first.id
        assert restored.document_state.recent_files == [str(first.files.path)]
    assert restored.close()


def test_no_restore_still_reads_recent_files(history_window, tmp_path, qtbot):
    window = history_window
    session = create(window, tmp_path / "上次的文件.mindmap")
    storage = window.manager.storage_dir
    assert window.close()
    restored = build_window(storage_dir=storage, restore=False)
    qtbot.addWidget(restored)
    assert not restored.sessions and restored.active_session is None
    assert restored.document_state.recent_files == [str(session.files.path)]
    assert restored.close()


@pytest.mark.parametrize("source", ["mouse", "keyboard"])
def test_recent_dropdown_mouse_and_keyboard_activate_original_tab(history_window, tmp_path, qtbot, source):
    """通过真实弹出菜单选择另一文件，验证入口、方向键和回车均走原文件打开路径。"""
    window = history_window
    first = create(window, tmp_path / "甲.mindmap")
    create(window, tmp_path / "乙.mindmap")
    button = window.recent_files_button
    menu = button.menu()
    chosen = []

    def select():
        try:
            assert menu.isVisible()
            assert menu.actions()[0].isChecked() and not menu.actions()[1].isChecked()
            if source == "keyboard":
                menu.setActiveAction(menu.actions()[0])
                qtbot.keyClick(menu, Qt.Key.Key_Down)
                qtbot.keyClick(menu, Qt.Key.Key_Return)
            else:
                qtbot.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(menu.actions()[1]).center())
            chosen.append(True)
        finally:
            menu.close()

    QTimer.singleShot(0, select)
    if source == "keyboard":
        button.setFocus()
        qtbot.keyClick(button, Qt.Key.Key_Space)
    else:
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(chosen))
    assert chosen and window.active_session is first and len(window.sessions) == 2
    assert window.document_state.recent_files[0] == str(first.files.path)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_recent_button_elides_long_names_and_fits_minimum_window(history_window, tmp_path, theme, compact):
    window = history_window
    session = create(window, tmp_path / (("很长的思维导图文件名称" * 12) + ".mindmap"))
    window.manager.set_theme(theme)
    window.manager.set_option("compact", compact)
    window.resize(860, 540)
    QApplication.processEvents()
    button, bar = window.recent_files_button, window.title_bar
    assert bar.menu.geometry().right() < button.x()
    assert button.geometry().right() < bar.minimize.x()
    assert bar.rect().contains(button.geometry())
    assert button.height() == 32 and button.width() <= 280
    assert button.text() != session.files.path.stem and "…" in button.text()
    assert button.toolTip() == str(session.files.path)
    assert button.icon().isNull()
    assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextOnly
    button.menu().aboutToShow.emit()
    assert button.menu().palette().color(button.menu().foregroundRole()) == window.manager.theme.color("text-default")


def test_recent_menu_disambiguates_names_and_offers_open_and_clear(history_window, tmp_path, monkeypatch):
    window = history_window
    for directory in ("工作", "个人"):
        parent = tmp_path / directory
        parent.mkdir()
        create(window, parent / "同名&导图.mindmap")
    menu = window.recent_files_button.menu()
    menu.aboutToShow.emit()
    first, second = menu.actions()[:2]
    assert first.text() != second.text()
    assert first.toolTip() != second.toolTip() and first.data() != second.data()
    assert "&&" in first.text() and first.isChecked()
    called = []
    monkeypatch.setattr(window, "open_dialog", lambda: called.append(True))
    next(action for action in menu.actions() if action.text() == "打开文件…").trigger()
    assert called
    next(action for action in menu.actions() if action.text() == "清空最近记录").trigger()
    assert window.document_state.recent_files == [] and len(window.sessions) == 2
    menu.aboutToShow.emit()
    assert menu.actions()[0].text() == "暂无最近文件" and not menu.actions()[0].isEnabled()
    assert not menu.actions()[-1].isEnabled()


@pytest.mark.parametrize("extra,no_restore", [(False, False), (True, False), (True, True), (None, True)])
def test_real_launcher_file_precedence_and_exit_snapshot(history_window, tmp_path, extra, no_restore):
    """真实事件循环退出验证启动参数，并覆盖停靠管理器先关闭后的业务保存和记录提交。"""
    window = history_window
    first, second = [create(window, tmp_path / f"{name}.mindmap") for name in "甲乙"]
    storage = window.manager.storage_dir
    assert window.close()
    third = tmp_path / "命令行导图.mindmap"
    save_document(MindMapDocument.new("命令行内容"), third)
    output = tmp_path / "启动结果.json"
    source = textwrap.dedent("""\
        import json
        import sys
        from pathlib import Path
        from mindmap_workbench import shell_adapter
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        original = shell_adapter.build_window
        def capture(**options):
            window = original(**options)
            def finish():
                payload = {
                    "paths": [str(item.files.path) for item in window.sessions.ordered()],
                    "active": str(window.files.path) if window.files else None,
                }
                Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
                if window.engine:
                    window.engine.update_node(window.engine.root_id, text="事件循环退出前的修改")
                    window.active_session.timer.stop()
                QApplication.instance().quit()
            QTimer.singleShot(0, finish)
            return window
        shell_adapter.build_window = capture
        raise SystemExit(shell_adapter.run(sys.argv[2:]))
        """)
    arguments = ["--state-dir", str(storage)]
    if no_restore:
        arguments.append("--no-restore")
    if extra is not None:
        arguments.append(str(third if extra else first.files.path))
    result = subprocess.run(
        [sys.executable, "-c", source, str(output), *arguments],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text("utf-8"))
    expected = [] if no_restore else [str(first.files.path), str(second.files.path)]
    if extra:
        expected.append(str(third))
    assert payload["paths"] == expected
    active = None if extra is None else str(third if extra else first.files.path)
    assert payload["active"] == active
    persisted = json.loads((storage / "documents-v1.json").read_text("utf-8"))
    assert [item["path"] for item in persisted["open_documents"]] == expected
    if active is not None:
        assert load_document(Path(active)).root.text == "事件循环退出前的修改"
