"""验收主菜单的编辑路由、插件边界和临时控件生命周期。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QLineEdit
from shiboken6 import isValid
from workbench_api import Disposable, EditorProvider, OperationResult, PluginManifest
from workbench_api.qt import CommandSpec, MenuContribution, MenuLocation
from workbench_runtime import WorkbenchRuntimeWindow


def submenu(menu, title):
    return next(action.menu() for action in menu.actions() if action.text() == title)


def test_standard_edit_commands_are_owned_by_host_with_or_without_legacy_items(window, qapp):
    """只实现公开编辑器协议也有编辑菜单，旧视图附带的标准动作不会重复出现。"""
    assert window.new_document()
    view = window.documents.active
    view.widget.setPlainText("示例文字")
    view.widget.selectAll()
    window.activateWindow()
    view.widget.setFocus()
    QApplication.processEvents()
    expected = [window.action("app." + key) for key in ("undo", "redo", "cut", "copy", "paste", "select_all")]

    menu = window.main_menu()
    assert [action for action in submenu(menu, "编辑").actions() if not action.isSeparator()] == expected
    expected[3].trigger()
    assert qapp.clipboard().text() == "示例文字"
    menu.deleteLater()

    extra = QAction("旧编辑器扩展", view.widget)
    view.edit_actions["copy"] = QAction("旧视图复制", view.widget)
    view.menu_actions = {"编辑": [view.edit_actions["copy"], window.action("app.copy"), extra]}
    menu = window.main_menu()
    assert [action for action in submenu(menu, "编辑").actions() if not action.isSeparator()] == [*expected, extra]
    menu.deleteLater()

    view.menu_actions = {"写错的菜单": [extra]}
    menu = window.main_menu()
    assert [action for action in submenu(menu, "编辑").actions() if not action.isSeparator()] == expected
    assert "未知的主菜单位置：写错的菜单" in window.operation_log.plain_text()
    menu.deleteLater()


def test_multiple_editors_share_new_submenu_and_keep_window_shortcut(qtbot, tmp_path, sample_manifest):
    """子菜单选择类型，Ctrl+N 仍按原规则优先调用活动编辑器。"""
    calls = []

    def activate(context):
        def create(path):
            calls.append(path)
            return OperationResult()

        context.editors.register(EditorProvider("notes.editor", "记录", (".notes",), create, lambda *_: None))
        return Disposable()

    notes = PluginManifest("notes", "记录插件", "1", activate)
    window = WorkbenchRuntimeWindow(storage_dir=tmp_path, restore=False, manifests=[notes, sample_manifest])
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    window.show()
    menu = window.main_menu()
    files = submenu(menu, "文件")
    choices = submenu(files, "新建")
    assert window.action("app.new") not in files.actions()
    assert [action.text() for action in choices.actions()] == ["记录", "示例"]
    choices.actions()[0].trigger()
    assert calls == [None]
    choices.actions()[1].trigger()
    assert len(window.documents.views) == 1
    menu.deleteLater()

    window.activateWindow()
    window.documents.active.widget.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(window, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    assert len(window.documents.views) == 2 and calls == [None]


def test_menu_registration_validates_locations_and_isolates_failed_contributions(window):
    """中文别名继续可用，错误分区及半成品不会影响其他贡献或宿主命令。"""
    context = window.plugins.instance("sample").context
    with pytest.raises(ValueError, match="未知的主菜单位置"):
        context.commands.register(CommandSpec("sample.bad", "无效命令", lambda: None, menu="edit.typo"))
    assert "sample.bad" not in window.manager.actions
    with pytest.raises(ValueError, match="未知的主菜单位置"):
        context.ui.contribute_menu(MenuContribution("file.typo", lambda menu: None))

    command = context.commands.register(CommandSpec("sample.view", "旧版视图命令", lambda: None, menu="视图"))

    def fail(menu):
        menu.addAction("未完成的导出项")
        raise RuntimeError("模拟动态菜单异常")

    context.ui.contribute_menu(MenuContribution(MenuLocation.FILE_EXPORT, fail))
    context.ui.contribute_menu(MenuContribution("file.export", lambda menu: menu.addAction("示例导出")))

    def legacy_file_menu(menu):
        action = QAction("旧版文件扩展", menu)
        menu.insertAction(context.ui.action("app.quit"), action)

    context.ui.contribute_menu(MenuContribution("文件", legacy_file_menu))
    menu = window.main_menu()
    files = submenu(menu, "文件")
    assert [action.text() for action in files.actions()[-3:]] == ["旧版文件扩展", "导出当前页", "退出"]
    assert [action.text() for action in submenu(files, "导出当前页").actions()] == ["示例导出"]
    assert command in submenu(menu, "视图").actions()
    assert "插件 sample 的菜单填充失败：模拟动态菜单异常" in window.operation_log.plain_text()
    menu.deleteLater()


def test_title_bar_releases_popup_menus_and_keeps_shared_actions(window, qtbot, monkeypatch):
    """常驻标题反复打开时，临时子菜单能触发命令并在关闭后释放。"""
    menus = []
    calls = []
    build = window.main_menu

    def contribute(menu):
        nested = menu.addMenu("测试子菜单")
        nested.addAction("测试命令", lambda: calls.append(True))
        menus.append(nested)

    context = window.plugins.instance("sample").context
    context.ui.contribute_menu(MenuContribution(MenuLocation.FILE, contribute))

    def inspect():
        menu = build()
        menus.extend([menu, *(action.menu() for action in menu.actions())])
        return menu

    monkeypatch.setattr(window, "main_menu", inspect)
    bar = window.title_bar.menu_bar
    entry = bar.actions()[0]
    popup = entry.menu()
    shared = window.action("app.open")
    for trigger in (True, False):
        qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.actionGeometry(entry).center())
        qtbot.waitUntil(popup.isVisible)
        assert shared in popup.actions()
        if trigger:
            nested = submenu(popup, "测试子菜单")
            popup.setActiveAction(nested.menuAction())
            qtbot.keyClick(popup, Qt.Key.Key_Right)
            qtbot.waitUntil(nested.isVisible)
            nested.setActiveAction(nested.actions()[0])
            qtbot.keyClick(nested, Qt.Key.Key_Return)
            assert calls == [True]
        else:
            qtbot.keyClick(popup, Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: all(not isValid(menu) for menu in menus))
        assert not popup.isVisible() and not popup.actions()
        assert isValid(shared) and isValid(entry)


def test_keyboard_edit_menu_preserves_line_edit_selection(window, qtbot, qapp):
    """Alt 导航借用原输入框及反向选区，菜单复制与粘贴仍作用于该选区。"""
    field = QLineEdit("abcdefgh", window)
    field.move(120, 80)
    field.show()
    window.activateWindow()
    field.setFocus()
    field.setSelection(5, -3)
    bar = window.title_bar.menu_bar
    file_entry, edit_entry, *_ = bar.actions()
    edit = edit_entry.menu()

    def execute(command):
        qtbot.keyClick(field, Qt.Key.Key_Alt)
        qtbot.waitUntil(lambda: qapp.focusWidget() is bar)
        assert bar.activeAction() is file_entry
        qtbot.keyClick(bar, Qt.Key.Key_Right)
        assert bar.activeAction() is edit_entry
        qtbot.keyClick(bar, Qt.Key.Key_Down)
        qtbot.waitUntil(edit.isVisible)
        assert field.selectedText() == "cde" and field.cursorPosition() == 2
        action = window.action("app." + command)
        assert action in edit.actions() and action.isEnabled()
        edit.setActiveAction(action)
        qtbot.keyClick(edit, Qt.Key.Key_Return)
        qtbot.waitUntil(field.hasFocus)

    execute("copy")
    assert qapp.clipboard().text() == "cde"
    qapp.clipboard().setText("替换")
    execute("paste")
    assert field.text() == "ab替换fgh"
