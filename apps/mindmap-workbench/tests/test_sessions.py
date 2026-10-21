"""多导图会话：独立状态、自动保存、原子关闭、文件归属及外壳布局兼容。"""

import json
import os
import subprocess
import sys
from copy import deepcopy

import pytest
from mindmap_engine import LayoutConfig, MindMapDocument, MindMapError, Theme, load_document, save_document
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton
from shiboken6 import isValid


@pytest.fixture
def empty_window(qtbot, tmp_path):
    """每例使用独立配置和文件目录；失败注入不影响测试结束时的资源回收。"""
    window = build_window(storage_dir=tmp_path / "配置", restore=False)

    def clean(item):
        for session in item.sessions.values():
            session.panels.properties.discard_pending()
            session.canvas.finish_edit(commit=False)
            session.engine.mark_saved()

    qtbot.addWidget(window, before_close_func=clean)
    window.show()
    QApplication.processEvents()
    yield window
    clean(window)
    window.close()


def create(window, path):
    """显式路径的新建不需要对话框，返回具有独立组件的已保存会话。"""
    assert window.new_document(path)
    return window.active_session


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_welcome_actions_and_last_close(empty_window, tmp_path, theme):
    window = empty_window
    window.manager.set_theme(theme)
    assert len(window.sessions) == 0 and window.active_session is None
    assert window.canvas is window.engine is window.files is window.panels is None
    assert window.manager.documents == {}
    group = window.host.editor.groups[0]
    assert group._empty_widget.isVisible() and group.count() == 0
    assert {button.text() for button in group._empty_widget.findChildren(QPushButton)} == {"新建导图", "打开文件"}
    assert not any(window.action("app." + key).isEnabled() for key in ("save", "save_all", "save_as"))
    assert not window.settings_action.isEnabled()
    menu = window.main_menu()
    files = menu.actions()[0].menu()
    assert [action.text() for action in files.actions()] == [
        "新建", "打开…", "保存", "保存全部", "另存为…", "导出图形", "退出",
    ]
    assert not files.actions()[5].isEnabled()
    assert not menu.actions()[1].menu().actions()[0].isEnabled()
    menu.deleteLater()
    session = create(window, tmp_path / "第一份.mindmap")
    assert not group._empty_widget.isVisible() and group.count() == 1
    session.engine.update_node(session.engine.root_id, text="关闭时立即保存")
    assert window.manager.close_document(session.id)
    assert load_document(session.files.path).root.text == "关闭时立即保存"
    assert window.active_session is None and not window.manager.documents
    assert group._empty_widget.isVisible() and session.disposed and session.canvas._disposed


def test_new_cancel_failure_and_path_collision_leave_sessions_unchanged(empty_window, tmp_path, monkeypatch):
    window = empty_window
    first = create(window, tmp_path / "已有.mindmap")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    assert not window.new_document()
    assert not window.new_document(first.files.path)
    assert not window.new_document(tmp_path / "不存在的目录" / "失败.mindmap")
    assert list(window.sessions.values()) == [first] and window.active_session is first
    assert load_document(first.files.path).root.text == "中心主题"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(tmp_path / "第二份"), ""))
    assert window.new_document() and len(window.sessions) == 2
    assert window.files.path == tmp_path / "第二份.mindmap"


def test_duplicate_open_preserves_split_position_memory_and_path_case(empty_window, tmp_path):
    window = empty_window
    first = create(window, tmp_path / "One.mindmap")
    second = create(window, tmp_path / "Two.json")
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    first.engine.update_node(first.engine.root_id, text="不从磁盘重载")
    first.timer.stop()
    location = window.host.editor.find_token(first.id)
    window.manager.activate_document(second.id)
    assert window.open_path(str(first.files.path).upper())
    assert window.active_session is first and len(window.sessions) == 2
    assert window.host.editor.find_token(first.id) == location
    assert first.engine.document.root.text == "不从磁盘重载"
    assert first.engine.can_undo


def test_save_as_cannot_overwrite_other_session(empty_window, tmp_path, monkeypatch):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    second.engine.update_node(second.engine.root_id, text="第二份修改")
    original = first.files.path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(first.files.path), ""))
    assert not window.save(save_as=True)
    assert second.files.path == tmp_path / "乙.mindmap" and second.files.is_dirty
    assert first.files.path.read_bytes() == original
    assert "另一个标签" in second.last_error
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(tmp_path / "另存.json"), "JSON 文档 (*.json)"))
    assert window.save(save_as=True)
    assert second.files.path.suffix == ".json" and not second.last_error
    assert load_document(second.files.path).root.text == "第二份修改"


def test_save_as_cancel_keeps_pending_autosave(empty_window, tmp_path, monkeypatch, qtbot):
    """取消另存为不应停止原文件已经排队的自动保存，也不改变会话路径。"""
    session = create(empty_window, tmp_path / "取消另存为.mindmap")
    session.engine.update_node(session.engine.root_id, text="继续自动保存")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    assert not empty_window.save(save_as=True) and session.timer.isActive()
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert load_document(session.files.path).root.text == "继续自动保存"


@pytest.mark.parametrize("suffix", [".json", ".md", ".txt", ".bin", ""])
def test_non_native_open_is_rejected_before_allocating_or_committing(empty_window, tmp_path, monkeypatch, suffix):
    """拒绝扩展名发生在会话分配和输入提交前，已有修改、选择和草稿均保留。"""
    window = empty_window
    first = create(window, tmp_path / "已有.mindmap")
    first.engine.add_child(first.engine.root_id, "保留修改")
    first.timer.stop()
    first.canvas.begin_edit(first.engine.root_id)
    first.canvas.editor.setPlainText("未提交的正文")
    source = tmp_path / ("其他文件" + suffix)
    # 使用真正的原生容器也不能绕过扩展名限制，拒绝操作不能改写输入文件。
    save_document(MindMapDocument.new("其他导图"), source, format_id="mindmap")
    original = source.read_bytes()
    before, revision = first.engine.document, first.engine.revision
    selection = first.canvas.selected_node_ids()
    monkeypatch.setattr("mindmap_workbench.sessions.MindMapSession", lambda *args: pytest.fail("拒绝前不能分配会话"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: pytest.fail("不再询问导入另存目标"))
    for operation in (window.open_path, first.files.open_path):
        with pytest.raises(MindMapError, match="仅支持打开 .mindmap"):
            operation(source)
    assert window.active_session is first and len(window.sessions) == 1
    assert first.engine.document == before and first.engine.revision == revision and first.files.is_dirty
    assert first.canvas.selected_node_ids() == selection and first.canvas.editor.toPlainText() == "未提交的正文"
    assert source.read_bytes() == original


@pytest.mark.parametrize("suffix", [".json", ".md", ".txt"])
def test_renamed_non_native_content_cannot_open(empty_window, tmp_path, suffix):
    """额外格式改名为 .mindmap 仍必须通过原生容器校验，失败不创建标签。"""
    first = create(empty_window, tmp_path / "原有.mindmap")
    source = tmp_path / ("非原生" + suffix)
    save_document(MindMapDocument.new("其他格式"), source)
    target = tmp_path / "伪装.mindmap"
    target.write_bytes(source.read_bytes())
    with pytest.raises(MindMapError):
        empty_window.open_path(target)
    assert empty_window.active_session is first and len(empty_window.sessions) == 1
    assert first.engine.document.root.text == "中心主题"


def test_open_dialog_works_without_session_and_cancel_preserves_active(empty_window, tmp_path, monkeypatch):
    """打开入口不依赖活动控制器；取消不创建候选会话，也不替换已有内容。"""
    source = tmp_path / "从空白页打开.mindmap"
    save_document(MindMapDocument.new("已有文件"), source)
    dialogs = []

    def choose(*args):
        dialogs.append(args[1:])
        return str(source), "思维导图 (*.mindmap)"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    assert empty_window.open_dialog()
    assert dialogs == [("打开导图", "", "思维导图 (*.mindmap)")]
    session = empty_window.active_session
    assert session.engine.document.root.text == "已有文件"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    assert not empty_window.open_dialog()
    assert empty_window.active_session is session and len(empty_window.sessions) == 1


def test_open_dialog_and_old_format_parameter_cannot_bypass_native_limit(empty_window, tmp_path, monkeypatch):
    """即使对话框返回额外类型或调用旧参数，也不能越过文件打开边界。"""
    first = create(empty_window, tmp_path / "当前.mindmap")
    source = tmp_path / "额外.json"
    save_document(MindMapDocument.new("JSON 内容"), source)
    selections = []

    def choose(*args):
        selections.append(args[3])
        return str(source), "JSON 文档 (*.json)"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    for operation in (empty_window.open_dialog, first.files.open_dialog):
        with pytest.raises(MindMapError, match="仅支持打开 .mindmap"):
            operation()
    for operation in (empty_window.open_path, empty_window.sessions.open_path, first.files.open_path):
        with pytest.raises(TypeError):
            operation(source, format_id="json")
    assert selections == ["思维导图 (*.mindmap)"] * 2
    assert empty_window.active_session is first and len(empty_window.sessions) == 1


def test_cli_explicit_file_creates_session_with_empty_startup(tmp_path):
    """启动参数必须通过会话管理器打开文件，不能依赖启动前已经存在画布。"""
    source = tmp_path / "命令行.mindmap"
    save_document(MindMapDocument.new("命令行打开"), source)
    output = tmp_path / "窗口.png"
    result = subprocess.run(
        [sys.executable, "-m", "mindmap_workbench", str(source), "--state-dir", str(tmp_path / "命令行配置"),
         "--no-restore", "--theme", "dark", "--screenshot", str(output)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert output.stat().st_size > 1000
    assert json.loads(output.with_suffix(".json").read_text("utf-8"))["theme"] == "dark"


def test_tab_and_split_focus_route_independent_panels_history_and_views(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    first.engine.add_child(first.engine.root_id, "甲的分支")
    first.canvas.focus_node(first.engine.root_id)
    first.canvas.view.scale(1.3, 1.3)
    first.canvas.set_editable(False)
    transform = first.canvas.view.transform()
    second = create(window, tmp_path / "乙.mindmap")
    assert not second.engine.can_undo and second.canvas.editable
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.activateWindow()
    first.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.active_session is first)
    assert window.read_only_action.isChecked()
    assert window._outline_stack.currentWidget() is first.panels.outline
    second.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.active_session is second)
    assert not window.read_only_action.isChecked()
    field = second.panels.properties.fields["style.font_size"]
    field.setValue(26)
    field.lineEdit().setFocus()
    QApplication.processEvents()
    assert window.active_session is second
    window.manager.activate_document(first.id)
    assert second.engine.document.root.style["font_size"] == 26
    assert first.engine.document.root.style.get("font_size") != 26
    assert first.canvas.view.transform() == transform and first.canvas.focus_id == first.engine.root_id
    assert first.engine.can_undo and not first.canvas.editable
    window.manager.activate_document(second.id)
    menu = window.main_menu()
    assert menu.actions()[1].menu().actions()[0] is second.canvas.action("undo")
    assert window.files is second.files and window.panels is second.panels
    menu.deleteLater()


def test_switch_commits_inline_input_without_returning_focus_to_old_split(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(first.id)
    first.canvas.begin_edit(first.engine.root_id)
    first.canvas.editor.setPlainText("提交给甲")
    second.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.active_session is second and first.canvas.editor is None)
    QApplication.processEvents()
    assert second.canvas.view.hasFocus() and window.active_session is second
    assert first.engine.document.root.text == "提交给甲"
    assert second.engine.document.root.text == "中心主题"


def test_outline_depth_is_per_session_and_preferences_are_shared(empty_window, tmp_path):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    first.panels.outline.set_display_depth(4)
    second = create(window, tmp_path / "乙.mindmap")
    assert second.panels.outline.display_depth == 4
    second.panels.outline.set_display_depth(1)
    second.canvas.set_wheel_mode("zoom")
    second.canvas.set_edraw_paste_options("replace", False)
    assert first.canvas.wheel_mode == "zoom" and first.canvas.edraw_paste_options == ("replace", False)
    assert first.panels.outline.display_depth == 4
    window.manager.activate_document(first.id)
    assert window._depth_action.menu() is first.panels.outline.depth_menu
    third = create(window, tmp_path / "丙.mindmap")
    assert third.panels.outline.display_depth == 1
    assert third.canvas.wheel_mode == "zoom" and third.canvas.edraw_paste_options == ("replace", False)


def test_autosave_debounce_undo_redo_and_view_only_changes(empty_window, tmp_path, qtbot):
    session = create(empty_window, tmp_path / "自动.json")
    writes = []

    def writer(document, path, **options):
        writes.append(document.root.text)
        return save_document(document, path, **options)

    session.files.writer = writer
    session.engine.update_node(session.engine.root_id, text="第一次")
    qtbot.wait(270)
    session.engine.update_node(session.engine.root_id, text="第二次")
    qtbot.wait(270)
    assert not writes and session.files.is_dirty
    qtbot.waitUntil(lambda: len(writes) == 1)
    assert writes == ["第二次"] and not session.files.is_dirty
    assert session.engine.undo()
    qtbot.waitUntil(lambda: len(writes) == 2)
    assert load_document(session.files.path).root.text == "第一次"
    assert session.engine.redo()
    qtbot.waitUntil(lambda: len(writes) == 3)
    session.canvas.view.scale(1.1, 1.1)
    session.canvas.center_root()
    session.canvas.select_nodes([session.engine.root_id])
    qtbot.wait(550)
    assert len(writes) == 3 and not session.timer.isActive()


@pytest.mark.parametrize("operation", ["style", "layout", "collapse", "theme"])
def test_document_changes_schedule_committed_save(empty_window, tmp_path, operation, qtbot):
    session = create(empty_window, tmp_path / "内容.mindmap")
    engine = session.engine
    if operation == "style":
        engine.update_node(engine.root_id, style={"font_size": 25})
    elif operation == "layout":
        engine.set_layout(LayoutConfig(direction="left"))
    elif operation == "collapse":
        engine.add_child(engine.root_id, "子主题")
        engine.set_expanded(engine.root_id, False)
    else:
        engine.set_theme(Theme.dark())
    assert session.timer.isActive()
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert load_document(session.files.path) == engine.document


def test_autosave_keeps_inline_ime_and_property_draft(empty_window, tmp_path):
    session = create(empty_window, tmp_path / "不打断输入.mindmap")
    session.engine.update_node(session.engine.root_id, text="已提交")
    session.canvas.begin_edit(session.engine.root_id)
    editor = session.canvas.editor
    editor.setPlainText("尚未提交的正文")
    event = QInputMethodEvent("输入法候选", [])
    editor.inputMethodEvent(event)
    assert session.autosave()
    assert session.canvas.editor is editor and editor.composing
    assert load_document(session.files.path).root.text == "已提交"
    assert session.engine.can_undo and not session.files.is_dirty
    assert not session.flush()
    editor.inputMethodEvent(QInputMethodEvent())
    session.canvas.finish_edit(commit=False)
    panel = session.panels.properties
    panel.fields["style.font_size"].setValue(28)
    panel.timer.stop()
    assert session.autosave()
    assert panel._pending == {"style.font_size": 28}
    assert load_document(session.files.path).root.style.get("font_size") != 28
    assert session.flush() and load_document(session.files.path).root.style["font_size"] == 28


def test_saved_notification_keeps_open_settings_menu(empty_window, tmp_path):
    session = create(empty_window, tmp_path / "菜单.mindmap")
    session.engine.add_child(session.engine.root_id, "修改")
    empty_window.show_settings_menu()
    QApplication.processEvents()
    assert session.settings_menu.isVisible()
    assert session.autosave()
    assert session.settings_menu.isVisible()
    session.settings_menu.close()


def test_save_failure_preserves_file_and_retries_only_on_request_or_edit(empty_window, tmp_path, qtbot):
    session = create(empty_window, tmp_path / "失败.mindmap")
    original = session.files.path.read_bytes()
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise OSError("模拟磁盘写入失败")

    session.files.writer = fail
    session.engine.update_node(session.engine.root_id, text="保留内存修改")
    qtbot.waitUntil(lambda: bool(calls))
    assert session.files.is_dirty and "磁盘" in session.last_error
    assert session.files.path.read_bytes() == original and not session.timer.isActive()
    qtbot.wait(600)
    assert len(calls) == 1
    group, index = empty_window.host.editor.find_token(session.id)
    assert group.tabText(index).endswith(" *") and "保存失败" in group.tabToolTip(index)
    assert not empty_window.manager.close_document(session.id)
    assert session.id in empty_window.manager.documents and not session.disposed
    calls_before_undo = len(calls)
    assert session.engine.undo()
    qtbot.waitUntil(lambda: not session.last_error)
    assert not session.files.is_dirty and len(calls) == calls_before_undo
    assert session.files.path.read_bytes() == original
    session.files.writer = session._write
    session.engine.update_node(session.engine.root_id, text="后续修改重试")
    qtbot.waitUntil(lambda: not session.files.is_dirty)
    assert not session.last_error and load_document(session.files.path).root.text == "后续修改重试"


def test_save_all_order_failure_and_atomic_close_others(empty_window, tmp_path):
    window = empty_window
    sessions = [create(window, tmp_path / f"{name}.mindmap") for name in ("甲", "乙", "丙")]
    group, _ = window.host.editor.find_token(sessions[0].id)
    group.tabBar().moveTab(2, 0)
    writes = []
    for session in sessions:
        session.engine.update_node(session.engine.root_id, text=session.files.path.stem)
        session.timer.stop()

        def writer(document, path, **options):
            writes.append(path.stem)
            if path.stem == "乙":
                raise OSError("乙无法保存")
            save_document(document, path, **options)

        session.files.writer = writer
    window.manager.activate_document(sessions[0].id)
    assert not window.save_all() and writes == ["丙", "甲", "乙"]
    assert window.active_session is sessions[1] and len(window.sessions) == 3
    group.close_others(sessions[0].id)
    assert len(window.sessions) == 3 and not any(session.disposed for session in sessions)
    sessions[1].files.writer = sessions[1]._write
    assert window.save_all() and window.active_session is sessions[1]
    group.close_others(sessions[0].id)
    assert list(window.sessions.values()) == [sessions[0]]
    assert window.active_session is sessions[0]


def test_exit_failure_keeps_all_targets_and_success_releases_resources(empty_window, tmp_path):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    second.engine.update_node(second.engine.root_id, text="关闭前修改")
    second.timer.stop()

    def fail(*args, **kwargs):
        raise OSError("退出保存失败")

    second.files.writer = fail
    assert not window.close() and len(window.sessions) == 2
    assert window.active_session is second and not first.disposed and not second.disposed
    second.files.writer = second._write
    assert window.close() and first.disposed and second.disposed
    assert not first.timer.isActive() and not second.timer.isActive()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(first.canvas) and not isValid(second.panels.properties)


def test_layout_restore_preserves_live_documents_and_restart_only_shell(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    snapshot = window.manager.capture_layout()
    second = create(window, tmp_path / "乙.mindmap")
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(first.id)
    for layout in (snapshot, window.manager._default_snapshot):
        before = deepcopy(layout)
        window.manager.restore_layout(layout)
        assert layout == before and window.active_session is first
        assert all(window.host.editor.find_token(session.id) for session in (first, second))
        group, index = window.host.editor.find_token(second.id)
        assert group.tabToolTip(index) == str(second.files.path)
    # 真实快捷键使用同一保留策略，不允许默认模板中的空标签组隐藏运行中会话。
    window.activateWindow()
    first.canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(first.canvas.view, Qt.Key.Key_F12, Qt.KeyboardModifier.ShiftModifier)
    assert window.active_session is first
    assert all(window.host.editor.find_token(session.id) for session in (first, second))
    window.manager.save_session()
    text = window.manager.storage_file.read_text("utf-8")
    assert str(first.files.path) not in text and "甲.mindmap" not in text
    assert set(json.loads(text)) == {"schema_version", "session"}
    storage = window.manager.storage_dir
    assert window.close()
    restarted = build_window(storage_dir=storage)
    qtbot.addWidget(restarted)
    assert restarted.active_session is None and restarted.manager.documents == {}
    assert all(group.count() == 0 for group in restarted.host.editor.groups)
    restarted.close()


def test_save_all_shortcut_from_sidebar_preserves_active_session(empty_window, tmp_path, qtbot):
    window = empty_window
    first = create(window, tmp_path / "甲.mindmap")
    second = create(window, tmp_path / "乙.mindmap")
    first.engine.update_node(first.engine.root_id, text="保存全部")
    first.timer.stop()
    window.activateWindow()
    field = second.panels.properties.fields["style.font_size"]
    field.lineEdit().setFocus()
    field.setValue(24)
    QApplication.processEvents()
    qtbot.keyClick(field.lineEdit(), Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert not first.files.is_dirty and not second.files.is_dirty
    assert load_document(first.files.path).root.text == "保存全部"
    assert load_document(second.files.path).root.style["font_size"] == 24
    assert window.active_session is second and field.lineEdit().hasFocus()
