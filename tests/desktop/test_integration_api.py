"""业务接入需要的公开接口及独立命令行启动回归。"""

import json
import os
import subprocess
import sys

import pytest
from mindmap_docking import DocumentSpec, ToolWindowSpec, ViewMode
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QMenu


def test_tool_header_actions_share_state_and_ownership(docking, qtbot):
    """通用标题接口支持菜单和普通动作，空配置不占位，布局快照不序列化 QAction。"""
    manager = docking.manager
    plain = QAction("业务操作", docking.document)
    plain.setEnabled(False)
    menu_action = QAction("层级", docking.document)
    menu = QMenu(docking.document)
    menu.addAction("选项")
    menu_action.setMenu(menu)
    spec = ToolWindowSpec("header_test", "标题动作", lambda: QLabel("业务内容"),
                          header_actions=(plain, menu_action))
    manager.register_tool_window(spec)
    manager.activate(spec.id)
    panel = manager.panel(spec.id)
    first, second = panel.action_buttons
    assert not first.isEnabled() and second.defaultAction().menu() is menu
    assert plain.parent() is docking.document and menu_action.parent() is docking.document
    assert manager.panel("alpha").action_buttons == []
    before = manager.capture_layout()
    calls = []
    plain.triggered.connect(lambda: calls.append(True))
    plain.setEnabled(True)
    plain.setText("更新文字")
    qtbot.mouseClick(first, Qt.MouseButton.LeftButton)
    assert calls == [True] and first.text() == "更新文字"
    assert manager.capture_layout() == before
    for mode in (ViewMode.UNDOCK, ViewMode.DOCK_PINNED):
        manager.set_view_mode(spec.id, mode)
        manager.set_theme("dark")
        assert panel.action_buttons == [first, second]
        assert first.isVisible() and second.isVisible()
    with pytest.raises(TypeError):
        ToolWindowSpec("invalid", "错误动作", lambda: QLabel(), header_actions=("错误",))


def test_document_title_survives_modified_close_and_reopen(docking):
    manager = docking.manager
    widget = manager.register_document(DocumentSpec("rename", "原名", lambda: QLabel("内容"),
                                                     can_close=lambda: True))
    manager.set_document_modified("rename", True)
    manager.set_document_title("rename", "新名称")
    group, index = docking.host.editor.find_token("rename")
    assert group.tabText(index) == "新名称 *"
    assert manager.close_document("rename")
    manager.set_document_title("rename", "关闭后改名")
    manager.open_document("rename")
    group, index = docking.host.editor.find_token("rename")
    assert group.tabText(index) == "关闭后改名 *"
    assert manager.documents["rename"]["widget"] is widget
    for title in ("", "  ", None):
        with pytest.raises(ValueError):
            manager.set_document_title("rename", title)
    with pytest.raises(KeyError):
        manager.set_document_title("missing", "标题")


def test_document_actions_follow_current_tab_without_taking_ownership(docking, qtbot):
    """当前页共享原动作状态；空配置、工具页和非法输入都不留下错误入口。"""
    manager, area = docking.manager, docking.host.editor
    action = QAction("文档操作", docking.document)
    action.setIcon(manager.theme.icon("document", 16))
    action.setToolTip("操作当前文档")
    action.setEnabled(False)
    before = manager.capture_layout()
    manager.set_document_actions("doc", [action])
    group, _ = area.find_token("doc")
    toolbar = group.cornerWidget()
    QApplication.processEvents()
    assert toolbar.isVisible() and toolbar.actions() == [action]
    assert action.parent() is docking.document
    assert manager.capture_layout() == before
    button = toolbar.widgetForAction(action)
    assert not button.isEnabled() and button.toolTip() == action.toolTip()
    triggered = []
    action.triggered.connect(lambda: triggered.append(True))
    action.setEnabled(True)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert triggered == [True]

    with pytest.raises(TypeError):
        manager.set_document_actions("doc", [action, "非法动作"])
    with pytest.raises(KeyError):
        manager.set_document_actions("不存在", [action])
    assert toolbar.actions() == [action]
    manager.register_document(DocumentSpec("plain", "普通页面", lambda: QLabel("无操作")))
    assert not toolbar.isVisible() and not toolbar.actions()
    group.setCurrentWidget(docking.document)
    assert toolbar.isVisible() and toolbar.actions() == [action]
    manager.set_document_actions("doc", [])
    assert not toolbar.isVisible() and not toolbar.actions()
    assert action.parent() is docking.document


@pytest.mark.parametrize("compact", [False, True])
def test_document_actions_survive_split_restore_migration_and_reopen(docking, compact):
    """重建编辑容器或移动标签仍使用业务原动作，Qt 删除旧组不会销毁动作。"""
    manager, area = docking.manager, docking.host.editor
    peer = manager.register_document(DocumentSpec("peer_doc", "其他文档", lambda: QLabel("内容")))
    first, second = QAction("第一个操作", docking.document), QAction("第二个操作", peer)
    manager.set_document_actions("doc", [first])
    manager.set_document_actions("peer_doc", [second])
    area.split_token("peer_doc", Qt.Orientation.Horizontal)
    manager.set_option("compact", compact)
    snapshot = manager.capture_layout()
    json.dumps(snapshot)
    manager.set_theme("dark")
    manager.restore_layout(snapshot)
    QApplication.processEvents()
    for identifier, action in (("doc", first), ("peer_doc", second)):
        group, _ = area.find_token(identifier)
        assert group.cornerWidget().actions() == [action]
        assert group.cornerWidget().height() == manager.theme.editor_header_height

    manager.move_document("peer_doc", "main", area.find_token("doc")[0].group_id)
    group, _ = area.find_token("peer_doc")
    assert group.cornerWidget().actions() == [second]
    assert second.parent() is peer
    assert manager.close_document("peer_doc")
    # 同一主窗口内合并标签后，关闭当前页应显示剩余文档自己的动作。
    assert group.cornerWidget().isVisible() and group.cornerWidget().actions() == [first]
    manager.open_document("peer_doc")
    group, _ = area.find_token("peer_doc")
    assert group.currentWidget() is peer and group.cornerWidget().actions() == [second]


def test_cli_accepts_business_argument_and_screenshots(tmp_path):
    output = tmp_path / "启动.png"
    source = '''
import sys
from pathlib import Path
from mindmap_shell import AppSpec, WorkbenchWindow, run_desktop
def builder(args):
    assert args.file == Path("中文.mindmap")
    return WorkbenchWindow(AppSpec("cli-test", "命令行验收", storage_dir=args.state_dir))
def configure(parser):
    parser.add_argument("file", type=Path)
raise SystemExit(run_desktop(builder, sys.argv[1:], configure_parser=configure))
'''
    result = subprocess.run(
        [sys.executable, "-c", source, "中文.mindmap", "--no-restore", "--theme", "dark",
         "--state-dir", str(tmp_path / "配置"), "--screenshot", str(output)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert output.stat().st_size > 1000
    assert json.loads(output.with_suffix(".json").read_text("utf-8"))["application_id"] == "cli-test"


def test_theme_change_preserves_business_input_focus(docking):
    docking.register("input", factory=lambda: QLineEdit("原始内容"))
    docking.manager.activate("input")
    editor = docking.manager.panel("input").content
    editor.setFocus()
    editor.setCursorPosition(2)
    QApplication.processEvents()
    parent = editor.parentWidget()
    docking.manager.set_theme("dark")
    assert editor.hasFocus() and editor.cursorPosition() == 2
    assert editor.text() == "原始内容" and editor.parentWidget() is parent
