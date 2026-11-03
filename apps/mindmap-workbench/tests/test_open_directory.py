"""打开所在文件夹的真实菜单、跨标签路由、路径更新及失败状态回归。"""

from pathlib import Path

import pytest
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QContextMenuEvent, QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu


@pytest.fixture
def directory_window(qtbot, tmp_path, monkeypatch):
    """使用隔离窗口和真实文件，只替换系统打开服务，避免测试弹出文件管理器。

    记录 Qt 收到的完整 URL，验证中文及特殊字符的往返；结束时直接释放业务会话，
    允许用例删除目录或保留草稿而不在清理过程中触发额外保存。
    """
    opened = []

    def open_url(address):
        opened.append(QUrl(address))
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)
    window = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    window.resize(1200, 820)
    window.show()
    QApplication.processEvents()
    yield window, opened
    window.dispose()
    window.close()


def create_document(window, path):
    """先建立隔离目录并按正式新建流程保存，返回拥有有效磁盘路径的会话。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    assert window.new_document(path)
    return window.active_session


def test_directory_action_tracks_active_document_and_empty_state(directory_window, tmp_path):
    """空白页禁用入口，标签切换后使用当前文件，失效标识不会误用活动标签。"""
    window, opened = directory_window
    action = window.action("app.open_directory")
    assert not action.isEnabled() and action.shortcut().isEmpty()
    assert not window.open_file_directory()
    first = create_document(window, tmp_path / "目录甲" / "甲.mindmap")
    second = create_document(window, tmp_path / "目录乙" / "乙.mindmap")
    recent = list(window.document_state.recent_files)
    for session in (first, second):
        window.manager.activate_document(session.id)
        assert action.isEnabled()
        menu = window.main_menu()
        files = next(item.menu() for item in menu.actions() if item.text() == "文件")
        next(item for item in files.actions() if item.text() == "打开所在文件夹").trigger()
        menu.deleteLater()
        assert window.active_session is session
    assert [Path(url.toLocalFile()) for url in opened] == [first.files.path.parent, second.files.path.parent]
    assert window.document_state.recent_files == recent
    assert not window.open_file_directory("失效标签") and not window.open_file_directory("")
    assert window.manager.close_documents([first.id, second.id])
    assert not action.isEnabled() and not window.open_file_directory(first.id)
    assert not window.manager.open_document_directory(first.id)
    window.dispose()
    assert not window.open_file_directory()
    assert len(opened) == 2


@pytest.mark.parametrize("deleted", [False, True])
def test_readonly_directory_supports_special_paths_and_deleted_files(directory_window, tmp_path, deleted):
    """中文、空格、&、#、% 不被解释为网址；只读及文件已删除均可打开有效父目录。"""
    window, opened = directory_window
    session = create_document(window, tmp_path / "中文 空格 & #%" / "测试.mindmap")
    session.canvas.set_editable(False)
    if deleted:
        session.files.path.unlink()
    window.operation_log.clear()
    assert window.action("app.open_directory").isEnabled()
    window.action("app.open_directory").trigger()
    assert len(opened) == 1 and opened[0].isLocalFile()
    assert Path(opened[0].toLocalFile()) == session.files.path.parent
    assert not session.last_error and not session.files.is_dirty
    entry, = window.operation_log.model.entries
    assert entry.level == "info" and entry.source_path == str(session.files.path)


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tab_menu_opens_hit_directory_without_activation(directory_window, tmp_path, split, theme):
    """真实右键菜单命中非活动标签和其他分屏，目录图标随主题呈现，保留原活动标签。"""
    window, opened = directory_window
    first = create_document(window, tmp_path / "来源目录" / "来源.mindmap")
    other = create_document(window, tmp_path / "活动目录" / "活动.mindmap")
    if split:
        window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    window.manager.set_theme(theme)
    window.manager.activate_document(other.id)
    group, index = window.host.editor.find_token(first.id)
    bar = group.tabBar()
    recent = list(window.document_state.recent_files)
    window.operation_log.clear()

    def choose_directory():
        menu = bar.findChildren(QMenu)[-1]
        try:
            assert window.active_session is other
            assert [action.text() for action in menu.actions()[:2]] == ["复制标签", "打开所在文件夹"]
            action = menu.actions()[1]
            assert not action.icon().isNull() and action.property("mindmap_icon") == "open-directory"
            action.trigger()
        finally:
            menu.close()

    QTimer.singleShot(0, choose_directory)
    point = bar.tabRect(index).center()
    bar.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, bar.mapToGlobal(point)))
    assert [Path(url.toLocalFile()) for url in opened] == [first.files.path.parent]
    assert window.active_session is other
    assert window.host.editor.find_token(first.id) == (group, index)
    assert window.document_state.recent_files == recent
    entry, = window.operation_log.model.entries
    assert entry.source_path == str(first.files.path)


def test_directory_callbacks_follow_shared_save_as_and_layout_restore(directory_window, tmp_path, monkeypatch):
    """复制标签及布局恢复沿用回调；任一标签另存为后，所有入口即时使用新的共享路径。"""
    window, opened = directory_window
    first = create_document(window, tmp_path / "旧目录" / "原文件.mindmap")
    assert window.manager.duplicate_document(first.id)
    copied = window.active_session
    target = tmp_path / "新目录" / "另存.mindmap"
    target.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    assert window.save(save_as=True)
    assert first.files.path == copied.files.path == target
    window.manager.restore_layout(window.manager.capture_layout())
    window.manager.activate_document(copied.id)
    recent = list(window.document_state.recent_files)
    window.operation_log.clear()
    assert window.manager.open_document_directory(first.id)
    assert window.manager.open_document_directory(copied.id)
    assert [Path(url.toLocalFile()) for url in opened] == [target.parent, target.parent]
    assert window.active_session is copied and window.document_state.recent_files == recent
    assert len(window.operation_log.model.entries) == 2
    assert all(entry.source_path == str(target) for entry in window.operation_log.model.entries)


@pytest.mark.parametrize(("failure", "existing_error"), [
    ("missing", ""), ("parent_file", ""), ("rejected", ""), ("raised", "既有保存错误"),
])
def test_directory_failure_logs_target_without_changing_save_state(
    directory_window, tmp_path, monkeypatch, failure, existing_error,
):
    """失效目录或系统失败仅提示一次，日志归属目标标签，保留原保存状态和活动导图。"""
    window, opened = directory_window
    target = create_document(window, tmp_path / "目标目录" / "目标.mindmap")
    other = create_document(window, tmp_path / "其他目录" / "活动.mindmap")
    if failure in ("missing", "parent_file"):
        target.files.path.unlink()
        target.files.path.parent.rmdir()
        if failure == "parent_file":
            target.files.path.parent.write_text("此位置已被普通文件占用", encoding="utf-8")
    else:
        def reject_directory(address):
            opened.append(QUrl(address))
            if failure == "raised":
                raise OSError("模拟系统桌面服务异常")
            return False

        monkeypatch.setattr(QDesktopServices, "openUrl", reject_directory)
    target.document.last_error = existing_error
    window.sessions.sync(target)
    title = window.manager.documents[target.id]["title"]
    recent = list(window.document_state.recent_files)
    window.operation_log.clear()
    assert not window.manager.open_document_directory(target.id)
    assert len(opened) == (0 if failure in ("missing", "parent_file") else 1)
    assert "打开所在文件夹失败" in window.status.currentMessage()
    entry, = window.operation_log.model.entries
    assert entry.level == "error" and entry.source_path == str(target.files.path)
    assert target.last_error == existing_error and not other.last_error
    assert window.manager.documents[target.id]["title"] == title
    assert not target.files.is_dirty and not other.files.is_dirty
    assert window.active_session is other and window.document_state.recent_files == recent


def test_directory_action_preserves_pending_text_and_unsaved_content(directory_window, tmp_path):
    """打开目录不主动提交正文草稿或保存已提交修改，磁盘内容、历史和最近记录保持原样。"""
    window, opened = directory_window
    session = create_document(window, tmp_path / "草稿.mindmap")
    disk_content = session.files.path.read_bytes()
    session.engine.update_node(session.engine.root_id, text="已经提交，等待自动保存")
    # 暂停原有自动保存，单独检查目录操作是否主动写盘；正常应用仍保留原调度规则。
    session.timer.stop()
    session.canvas.begin_edit(session.engine.root_id)
    editor = session.canvas.editor
    editor.setPlainText("尚未提交的中文草稿")
    document, revision = session.engine.document, session.engine.revision
    recent = list(window.document_state.recent_files)
    window.action("app.open_directory").trigger()
    assert len(opened) == 1
    assert session.canvas.editor is editor and editor.toPlainText() == "尚未提交的中文草稿"
    assert session.engine.document == document and session.engine.revision == revision
    assert session.engine.can_undo and session.files.is_dirty
    assert session.files.path.read_bytes() == disk_content
    assert window.document_state.recent_files == recent
