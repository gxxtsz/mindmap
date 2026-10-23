"""工作台真实控件集成：文件、扩展、焦点、主题与停靠生命周期。"""

from copy import deepcopy

import pytest
from mindmap_core import (
    BranchStyle,
    FormatInfo,
    FormatRegistry,
    LayoutConfig,
    LayoutInfo,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Theme,
)
from mindmap_docking import DockPosition, DockTarget, ViewMode
from mindmap_formats import load_document, save_document
from mindmap_layouts import TreeLayout, resolve_tree_spacing
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QEnterEvent, QIcon, QImage, QWheelEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMenu, QMessageBox, QToolButton


@pytest.fixture
def window(qtbot, tmp_path):
    widget = build_window(storage_dir=tmp_path / "配置", restore=False)

    def prepare_close(item):
        for session in item.sessions.values():
            session.panels.properties.discard_pending()
            session.canvas.finish_edit(commit=False)
            session.engine.mark_saved()

    qtbot.addWidget(widget, before_close_func=prepare_close)
    assert widget.new_document(tmp_path / "初始.mindmap")
    widget.show()
    QApplication.processEvents()
    yield widget
    prepare_close(widget)
    widget.close()


def test_pin_keeps_open_documents_and_active_session(window, qtbot, tmp_path):
    """置顶属于窗口操作，多导图的会话实例、活动项、历史与磁盘内容均保持原样。"""
    assert window.new_document(tmp_path / "第二张.mindmap")
    sessions = list(window.sessions.values())
    active = window.active_session
    documents = [session.engine.document for session in sessions]
    contents = [session.files.path.read_bytes() for session in sessions]
    for _ in range(2):
        qtbot.mouseClick(window.title_bar.pin, Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert list(window.sessions.values()) == sessions and window.active_session is active
        for session, document, content in zip(sessions, documents, contents):
            assert session.engine.document == document
            assert not session.engine.can_undo and not session.files.is_dirty
            assert session.files.path.read_bytes() == content


def test_new_file_session_and_editable_framework(window):
    assert len(window.engine.document.nodes) == 1
    assert window.engine.document.root.style["branch"] == {"kind": "round"}
    assert not window.engine.can_undo and not window.files.is_dirty
    assert window.panels.settings.branch.currentData() == "round"
    assert list(window.manager.documents) == [window.active_session.id]
    assert window.manager.documents[window.active_session.id]["widget"] is window.canvas
    assert window.manager.states["properties"].visible
    assert window.panels.properties.isVisible()
    assert window.panels.properties.fields["style.font_size"].isVisible()
    assert not window.manager.states["outline"].visible
    assert "settings" not in window.manager.states
    assert window.host.region_sizes["dock"]["right"] == 360
    bar = window.title_bar
    assert all(bar.box.indexOf(button) < bar.box.indexOf(bar.minimize) for button in bar.action_buttons)
    assert all(button.accessibleName() not in {"设置", "外观与布局"}
               for button in bar.findChildren(QToolButton))


@pytest.mark.parametrize("entry", ["button", "shortcut"])
@pytest.mark.parametrize("restore_layout", [False, True])
def test_properties_toggle_closes_visible_panel_without_focus(window, qtbot, entry, restore_layout):
    """默认或恢复的属性侧栏在画布取得焦点后，首次切换即关闭，再次切换即打开。"""
    manager = window.manager
    if restore_layout:
        manager.restore_layout(manager.capture_layout())
    properties = window.panels.properties
    button = window.host.right_rail._buttons["properties"]

    def toggle():
        """通过真实侧栏按钮或 Alt+2 触发共用的显隐逻辑。"""
        if entry == "button":
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        else:
            qtbot.keyClick(window, Qt.Key.Key_2, Qt.KeyboardModifier.AltModifier)
        QApplication.processEvents()

    window.activateWindow()
    for _ in range(2):
        window.canvas.view.setFocus()
        QApplication.processEvents()
        assert manager.active_id == ""
        assert properties.isVisible() and button.isChecked()
        toggle()
        assert not manager.states["properties"].visible
        assert not properties.isVisible() and window.host.right.isHidden() and not button.isChecked()
        toggle()
        assert manager.states["properties"].visible
        assert properties.isVisible() and button.isChecked()
        assert window.panels.properties is properties


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_top_left_menu_contains_shared_settings_and_existing_options(window, qtbot, theme_name):
    """明暗主题下点击左上角主菜单，核对设置子菜单中的三组平铺选项。"""
    window.manager.set_theme(theme_name)
    expected = [
        "浅色", "深色", "紧凑模式", None,
        "显示停靠栏", "始终显示标题按钮", None, "宽屏布局", "左侧并排", "右侧并排",
    ]
    popped = []

    def inspect_menu():
        main = QApplication.activePopupWidget()
        assert isinstance(main, QMenu)
        try:
            settings_action = next(action for action in main.actions() if action.text() == "设置")
            menu = settings_action.menu()
            # 菜单刚进入事件循环时使用键盘展开，避免模拟鼠标与子菜单延时竞争。
            main.setActiveAction(settings_action)
            qtbot.keyClick(main, Qt.Key.Key_Right)
            qtbot.waitUntil(menu.isVisible)
            popped.append([None if action.isSeparator() else action.text() for action in menu.actions()])
            for action in menu.actions()[:2]:
                assert action.isChecked() == (action.text() == ("浅色" if theme_name == "light" else "深色"))
            assert all(action.menu() is None for action in menu.actions())
            menu.close()
        finally:
            main.close()

    QTimer.singleShot(0, inspect_menu)
    qtbot.mouseClick(window.title_bar.menu, Qt.MouseButton.LeftButton)
    assert popped == [expected]
    assert set(window.manager.specs) == {"outline", "favorites", "properties", "map_settings"}
    main = window.main_menu()
    settings = next(action.menu() for action in main.actions() if action.text() == "设置")
    assert [None if action.isSeparator() else action.text() for action in settings.actions()] == expected
    assert [action.text() for action in main.actions()] == ["文件", "编辑", "视图", "设置"]
    files = next(action.menu() for action in main.actions() if action.text() == "文件")
    assert [action.text() for action in files.actions()] == ["新建", "打开…", "保存", "保存全部", "另存为…", "导出图形", "退出"]
    exports = next(action.menu() for action in files.actions() if action.text() == "导出图形")
    assert [action.text() for action in exports.actions()] == ["PNG 图片…", "SVG 矢量图…", "PDF 文档…"]
    assert all(action.isEnabled() for action in exports.actions())
    view = next(action.menu() for action in main.actions() if action.text() == "视图")
    assert view.actions() == [window.canvas.action(key) for key in (
        "zoom_in", "zoom_out", "expand_all", "collapse_all",
    )] + [window.read_only_action]
    assert not hasattr(window, "show_document")
    assert window.manager.states["properties"].visible
    main.deleteLater()


@pytest.mark.parametrize(("label", "key"), [
    ("紧凑模式", "compact"), ("显示停靠栏", "show_bars"),
    ("始终显示标题按钮", "always_header_icons"), ("宽屏布局", "widescreen"),
    ("左侧并排", "left_side_by_side"), ("右侧并排", "right_side_by_side"),
])
def test_grouped_menu_options_toggle_and_reopen_with_current_state(window, label, key):
    """各组选项沿用原回调，重新打开菜单时展示最新勾选状态。"""
    initial = getattr(window.manager.settings, key)
    for expected in (not initial, initial):
        menu = window.appearance_menu()
        action = next(action for action in menu.actions() if action.text() == label)
        assert action.isCheckable() and action.isChecked() != expected
        action.trigger()
        assert getattr(window.manager.settings, key) == expected
        menu.deleteLater()
    menu = window.appearance_menu()
    assert next(action for action in menu.actions() if action.text() == label).isChecked() == initial
    menu.deleteLater()


@pytest.mark.parametrize(("label", "theme_name"), [("深色", "dark"), ("浅色", "light")])
def test_grouped_menu_theme_actions_update_theme(window, label, theme_name):
    """主题动作仍切换外壳主题，后续菜单的主题勾选与管理器保持一致。"""
    window.manager.set_theme("light" if theme_name == "dark" else "dark")
    menu = window.appearance_menu()
    next(action for action in menu.actions() if action.text() == label).trigger()
    assert window.manager.theme.name == theme_name
    menu.deleteLater()
    menu = window.appearance_menu()
    assert next(action for action in menu.actions() if action.text() == label).isChecked()
    menu.deleteLater()


def test_settings_shortcut_and_real_menu_click_switch_right_panel(window, qtbot):
    """Alt+3 打开布局侧栏，真实结构菜单点击沿用整图事务。"""
    window.activateWindow()
    window.canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(window.canvas.view, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
    assert window.panels.settings.isVisible() and not window.panels.properties.isVisible()
    window.panels.settings.direction.showPopup()
    directions = window.panels.settings.direction.popup_menu
    action = next(action for action in directions.actions() if action.data() == "left")
    qtbot.mouseClick(directions, Qt.MouseButton.LeftButton, pos=directions.actionGeometry(action).center())
    assert window.engine.document.layout.direction == "left"
    assert window.manager.states["map_settings"].visible
    assert window.engine.undo() and window.engine.document.layout.direction == "right"


def test_settings_sidebar_readonly_and_inline_edit_preserve_document(window, qtbot):
    """侧栏查看和滚轮切换不提交正文，受限状态的布局和外观控件均禁用。"""
    before, revision = window.engine.document, window.engine.revision
    wheel = window._status_controls.buttons["wheel_mode"]
    window.show_settings_panel()
    assert window.panels.settings.isVisible() and not window.panels.properties.isVisible()
    assert window.panels.settings.branch.isVisible() and window.panels.settings.theme.isVisible()
    assert list(window.panels.settings.sections) == ["导图布局", "导图外观"]
    assert window.engine.document == before and window.engine.revision == revision
    window.canvas.begin_edit(window.engine.root_id)
    window.canvas.editor.setPlainText("尚未提交的正文草稿")
    editor = window.canvas.editor
    qtbot.mouseClick(wheel, Qt.MouseButton.LeftButton)
    assert window.canvas.editor is editor and editor.toPlainText() == "尚未提交的正文草稿"
    assert window.canvas.wheel_mode == "zoom"
    assert all(not window._status_controls.buttons[key].isEnabled() for key in ("fit", "reset_zoom", "center"))
    window.show_settings_panel()
    assert window.canvas.is_editing and window.engine.document == before
    assert not window.panels.settings.direction.isEnabled()
    assert not window.panels.settings.horizontal.isEnabled()
    assert not window.panels.settings.branch.isEnabled() and not window.panels.settings.theme.isEnabled()
    window.canvas.finish_edit(commit=False)
    window.canvas.set_editable(False)
    assert all(window._status_controls.buttons[key].isEnabled() for key in ("fit", "reset_zoom", "center"))
    assert not window.panels.settings.vertical.isEnabled()
    assert all(not button.isEnabled() for button in window.panels.settings.preset_buttons)
    assert not window.panels.settings.branch.isEnabled() and not window.panels.settings.theme.isEnabled()
    assert wheel.isEnabled()
    qtbot.mouseClick(wheel, Qt.MouseButton.LeftButton)
    assert window.canvas.wheel_mode == "scroll"
    window.canvas.set_editable(True)
    assert window.panels.settings.horizontal.isEnabled()
    assert window.engine.document == before and window.engine.revision == revision


def test_sidebar_appearance_real_selection_history_autosave_and_sessions(window, tmp_path, qtbot):
    """真实侧栏弹层应用于当前导图，可撤销、重做、自动保存并随分屏切换归属。"""
    first = window.active_session
    first.engine.add_child(first.engine.root_id, "子主题")
    window.show_settings_panel()
    panel = first.panels.settings
    panel.branch.showPopup()
    qtbot.mouseClick(panel.branch.popup_menu.buttons[BranchStyle.ARROW], Qt.MouseButton.LeftButton)
    assert all(node.style["branch"]["kind"] == "arrow" for node in first.engine.document.nodes.values())
    assert "覆盖已有局部连接线样式" in panel.branch.toolTip()
    before = first.engine.document
    panel.theme.showPopup()
    view = panel.theme.view()
    index = panel.theme.model().index(panel.theme.findData("dark"), 0)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())
    after = first.engine.document
    assert after.theme == Theme.dark() and window.manager.theme.name == "light"
    assert first.engine.undo() and first.engine.document == before
    assert first.engine.redo() and first.engine.document == after
    revision = first.engine.revision
    panel.theme.showPopup()
    qtbot.keyClick(panel.theme.view(), Qt.Key.Key_Return)
    assert first.engine.revision == revision
    assert window.new_document(tmp_path / "独立外观.mindmap")
    second = window.active_session
    assert second.panels.settings.branch.currentData() == "round"
    assert second.panels.settings.theme.currentData() == "light"
    qtbot.waitUntil(lambda: not first.engine.is_dirty, timeout=3000)
    assert load_document(first.files.path) == after
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    first.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.active_session is first)
    assert window._settings_stack.currentWidget() is panel
    assert panel.branch.currentData() == "arrow" and panel.theme.currentData() == "dark"
    panel.branch.showPopup()
    stale = panel.branch.popup_menu.buttons[BranchStyle.LINE]
    window.manager.activate_document(second.id)
    assert not panel.branch.popup_menu.isVisible()
    stale.click()
    assert first.engine.document == after and second.engine.document.theme == Theme()
    reopened = load_document(first.files.path)
    first.engine.replace_document(reopened)
    assert panel.branch.currentData() == "arrow" and panel.theme.currentData() == "dark"


def test_layout_settings_follow_sessions_flush_and_autosave(window, tmp_path, qtbot):
    """切换标签先提交原导图间距，自动保存归属原文件，分屏焦点同步正确面板。"""
    first = window.active_session
    window.show_settings_panel()
    first.panels.settings.horizontal.setValue(91.5)
    first.panels.settings.vertical.setValue(33.5)
    assert first.panels.settings.timer.isActive()
    assert window.new_document(tmp_path / "第二份导图.mindmap")
    second = window.active_session
    assert first is not second
    assert resolve_tree_spacing(first.engine.document.layout) == (91.5, 33.5)
    assert second.engine.document.layout.options == {}
    assert window._settings_stack.currentWidget() is second.panels.settings
    assert second.panels.settings.horizontal.value() == 64
    qtbot.waitUntil(lambda: not first.engine.is_dirty, timeout=3000)
    assert resolve_tree_spacing(load_document(first.files.path).layout) == (91.5, 33.5)
    assert load_document(second.files.path).layout.options == {}
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    first.canvas.view.setFocus()
    qtbot.waitUntil(lambda: window.active_session is first)
    assert window._settings_stack.currentWidget() is first.panels.settings
    first.panels.settings.preset_buttons[2].click()
    assert window.save()
    assert resolve_tree_spacing(load_document(first.files.path).layout) == (96, 36)
    assert second.engine.document.layout.options == {}


def test_layout_sidebar_restores_geometry_and_default_layout(window, qtbot):
    """新面板独立持久化，恢复默认布局后仍回到节点属性且保留文档配置。"""
    window.show_settings_panel()
    window.panels.settings.horizontal.setValue(80)
    window.panels.settings.vertical.setValue(30)
    assert window.save()
    snapshot = window.manager.capture_layout()
    snapshot["hosts"]["main"]["region_sizes"]["dock"]["right"] = 410
    window.manager.restore_layout(snapshot)
    assert window.manager.states["map_settings"].visible
    window.manager.save_session()
    path, directory = window.files.path, window.manager.storage_dir
    assert window.close()
    restored = build_window(storage_dir=directory)
    qtbot.addWidget(restored)
    try:
        restored.show()
        QApplication.processEvents()
        assert restored.files.path == path
        assert restored.manager.states["map_settings"].visible
        assert restored.panels.settings.isVisible() and not restored.panels.properties.isVisible()
        assert restored.host.region_sizes["dock"]["right"] == 410
        assert resolve_tree_spacing(restored.engine.document.layout) == (80, 30)
        restored.manager.restore_default_layout()
        assert restored.manager.states["properties"].visible
        assert not restored.manager.states["map_settings"].visible
        assert resolve_tree_spacing(restored.engine.document.layout) == (80, 30)
    finally:
        restored.close()


def test_status_controls_survive_temporary_main_menu_deletion(window, qtbot):
    """临时主菜单释放后，常驻状态栏仍可切换滚轮，侧栏仍可提交布局。"""
    temporary = window.main_menu()
    temporary.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtbot.mouseClick(window._status_controls.buttons["wheel_mode"], Qt.MouseButton.LeftButton)
    assert window.canvas.wheel_mode == "zoom"
    window.panels.settings.horizontal.setValue(80)
    window.panels.flush_pending_edits()
    assert resolve_tree_spacing(window.engine.document.layout) == (80, 22)


def test_status_wheel_preference_survives_restart_without_document_changes(window, qtbot):
    """按钮改变本机滚轮偏好，重启后恢复但不写入文档和撤销历史。"""
    before, revision = window.engine.document, window.engine.revision
    wheel = window._status_controls.buttons["wheel_mode"]
    initial_icon = wheel.icon().cacheKey()
    for mode, current, following in (("zoom", "缩放", "上下滚动"), ("scroll", "上下滚动", "缩放"),
                                     ("zoom", "缩放", "上下滚动")):
        qtbot.mouseClick(wheel, Qt.MouseButton.LeftButton)
        assert window.canvas.wheel_mode == mode
        assert f"滚轮行为：{current}" in wheel.toolTip() and f"点击切换为{following}" in wheel.toolTip()
        assert "Ctrl＋滚轮始终缩放" in wheel.toolTip()
        assert not wheel.isCheckable() and not wheel.isChecked() and wheel.menu() is None
        assert QApplication.activePopupWidget() is None
    assert wheel.icon().cacheKey() != initial_icon
    assert window.canvas.wheel_mode == "zoom"
    assert window.engine.document == before and window.engine.revision == revision
    assert not window.files.is_dirty
    window.close()
    restored = build_window(storage_dir=window.manager.storage_dir)
    qtbot.addWidget(restored)
    try:
        assert restored.new_document(window.manager.storage_dir / "重启.mindmap")
        assert restored.canvas.wheel_mode == "zoom" and not restored.files.is_dirty
        assert "滚轮行为：缩放" in restored._status_controls.buttons["wheel_mode"].toolTip()
        assert "settings" not in restored.manager.specs
    finally:
        restored.close()


def test_empty_status_controls_are_disabled_and_application_options_remain(window, qtbot, tmp_path):
    """关闭全部导图后按钮原位禁用；应用设置可用，新建后重新绑定新的画布。"""
    controls = window._status_controls
    previous = window.canvas
    old_icon = controls.buttons["wheel_mode"].icon().cacheKey()
    assert window.manager.close_document(window.active_session.id)
    menu = window.appearance_menu()
    assert controls.isVisible() and all(not button.isEnabled() for button in controls.buttons.values())
    assert all("没有打开" in button.toolTip() for button in controls.buttons.values())
    assert all(not button.actions() for button in controls.buttons.values())
    assert not any(action.text().startswith("滚轮") for action in menu.actions())
    assert next(action for action in menu.actions() if action.text() == "深色").isEnabled()
    assert not window.settings_action.isEnabled()
    assert not any(action.text().startswith(("导图设置", "导图布局", "连接线样式", "导图主题"))
                   for action in menu.actions())
    menu.deleteLater()
    # 关闭后的旧画布即使尚未完成延迟销毁，也不能再更新按钮或成为点击目标。
    previous.action("wheel_mode").setIcon(QIcon())
    assert controls.buttons["wheel_mode"].icon().cacheKey() == old_icon
    assert window.new_document(tmp_path / "重新绑定.mindmap")
    assert controls.buttons["fit"].defaultAction() is window.canvas.action("fit")
    assert controls.buttons["reset_zoom"].defaultAction() is window.canvas.action("reset_zoom")
    assert all(button.isEnabled() for button in controls.buttons.values())
    qtbot.mouseClick(controls.buttons["wheel_mode"], Qt.MouseButton.LeftButton)
    assert window.canvas.wheel_mode == "zoom"


def test_legacy_settings_tool_is_ignored_in_saved_session(window, qtbot):
    """带命名布局字段的旧会话仍可加载，已移除的设置浮窗不会恢复。"""
    snapshot = window.manager.capture_layout()
    legacy = deepcopy(next(item for item in snapshot["tools"] if item["id"] == "properties"))
    legacy.update(id="settings", visible=True, mode="float")
    snapshot["tools"].append(legacy)
    snapshot["active"] = "settings"
    window.close()
    # 模拟旧版只有外壳布局、没有独立文档记录的配置；旧标签标识不能单独重开文件。
    window.document_state.store.path.unlink()
    window.manager.layout_store.save({
        "schema_version": 1, "current_layout": "旧布局", "session": snapshot,
        "layouts": {"旧布局": snapshot},
    })
    restored = build_window(storage_dir=window.manager.storage_dir)
    qtbot.addWidget(restored)
    try:
        restored.show()
        QApplication.processEvents()
        assert "settings" not in restored.manager.states
        assert set(restored.manager.hosts) == {"main"}
        assert restored.manager.states["properties"].visible
        assert restored.active_session is None
        assert not restored._properties_stack.isVisible()
        assert restored.host.right.isHidden()
        restored.manager.restore_layout(snapshot)
        assert "settings" not in restored.manager.states
        assert restored.manager.states["properties"].visible
        assert restored.host.right.isHidden()
        restored.manager.save_session()
        assert set(restored.manager.layout_store.load()) == {"schema_version", "session"}
    finally:
        restored.close()


def test_outline_header_menu_survives_theme_and_docking(window, qtbot):
    """真实标题按钮弹出共用菜单；临时展开、重停靠和主题切换不替换按钮或动作。"""
    manager, tree = window.manager, window.panels.outline
    manager.activate("outline", focus=False)
    panel = manager.panel("outline")
    button, = panel.action_buttons
    assert button.defaultAction() is window._depth_action
    assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert not button.icon().isNull() and button.width() == 28
    assert button.iconSize() == QSize(14, 14)
    assert panel.header_layout.indexOf(button) < panel.header_layout.indexOf(panel.more)
    popped = []

    def choose_depth():
        """在按钮进入原生菜单事件循环后点击真实菜单项。"""
        popped.append(tree.depth_menu.isVisible())
        action = tree.depth_menu.actions()[4]
        qtbot.mouseClick(tree.depth_menu, Qt.MouseButton.LeftButton,
                         pos=tree.depth_menu.actionGeometry(action).center())

    QTimer.singleShot(0, choose_depth)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert popped == [True] and tree.display_depth == 4 and button.text() == "4层"
    assert button.toolTip() == "显示层级数：4层"
    before, revision = window.engine.document, window.engine.revision
    for theme in ("dark", "light"):
        manager.set_theme(theme)
        for mode in (ViewMode.UNDOCK, ViewMode.DOCK_PINNED):
            manager.set_view_mode("outline", mode)
            QApplication.processEvents()
            assert manager.panel("outline").action_buttons == [button]
            assert button.isVisible() == panel.more.isVisible() == panel.hide_button.isVisible()
            assert button.defaultAction().menu() is tree.depth_menu
    assert window.engine.document == before and window.engine.revision == revision
    # 非活动面板离开时三个入口同时消失，悬停、激活或全局常显设置又同时出现。
    controls = (button, panel.more, panel.hide_button)
    manager.mark_active("properties")
    QApplication.sendEvent(panel, QEvent(QEvent.Type.Leave))
    assert all(control.isHidden() for control in controls)
    QApplication.sendEvent(panel, QEnterEvent(QPointF(), QPointF(), QPointF()))
    assert all(not control.isHidden() for control in controls)
    QApplication.sendEvent(panel, QEvent(QEvent.Type.Leave))
    assert all(control.isHidden() for control in controls)
    manager.mark_active("outline")
    assert all(not control.isHidden() for control in controls)
    manager.mark_active("properties")
    manager.set_option("always_header_icons", True)
    assert all(not control.isHidden() for control in controls)
    manager.set_option("always_header_icons", False)
    # 外观刷新可能重新触发鼠标进入；明确模拟离开，保证本断言检查非悬停状态。
    QApplication.sendEvent(panel, QEvent(QEvent.Type.Leave))
    assert all(control.isHidden() for control in controls)


@pytest.mark.parametrize("depth", [1, 9, None])
def test_outline_depth_preferences_survive_new_open_and_restart(window, qtbot, tmp_path, depth):
    """持久化的只是预设，新建、打开与同目录重建窗口均恢复它。"""
    tree = window.panels.outline
    tree.set_display_depth(depth)
    assert int(window.preferences.value("outline/display_depth")) == (0 if depth is None else depth)
    assert window.new_document(tmp_path / "新建.mindmap") and tree.display_depth == depth
    tree = window.panels.outline
    document = MindMapDocument.new("待打开的导图")
    engine = MindMapEngine(document)
    child = engine.add_child(engine.root_id, "直接子节点")
    engine.add_child(child, "更深子节点")
    path = tmp_path / "层级验收.mindmap"
    save_document(engine.document, path)
    assert window.open_path(path)
    tree = window.panels.outline
    assert tree.display_depth == depth
    assert tree.isExpanded(tree.tree_model.index_for(window.engine.root_id)) == (depth != 1)
    window.engine.mark_saved()
    window.close()
    restored = build_window(storage_dir=window.manager.storage_dir, restore=False)
    qtbot.addWidget(restored)
    try:
        assert restored.active_session is None
        assert restored.open_path(path)
        assert restored.panels.outline.display_depth == depth
        assert restored.panels.outline.isExpanded(
            restored.panels.outline.tree_model.index_for(restored.engine.root_id)
        ) == (depth != 1)
    finally:
        restored.engine.mark_saved()
        restored.close()


@pytest.mark.parametrize("value", [None, -1, 10, "损坏配置", True, "2.0"])
def test_outline_invalid_preferences_use_default(qtbot, tmp_path, value):
    """缺失、越界和非整数配置都回退2层，不向组件传递非法参数。"""
    preferences = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    if value is not None:
        preferences.setValue("outline/display_depth", value)
        preferences.sync()
    restored = build_window(storage_dir=tmp_path, restore=False)
    qtbot.addWidget(restored)
    assert restored.new_document(tmp_path / "默认层级.mindmap")
    assert restored.panels.outline.display_depth == 2
    restored.close()


@pytest.mark.parametrize("replace_root", [False, True])
def test_edraw_paste_preferences_are_saved_only_after_success(window, monkeypatch, qtbot, replace_root):
    """真实粘贴保存三项偏好；取消或引擎拒绝不覆盖，重建工作台后恢复根节点选择。"""
    from mindmap_qt_canvas.clipboard import EDRAW_CLIPBOARD_MIME
    from mindmap_qt_ui.dialogs import EdrawPasteOptions
    from PySide6.QtCore import QMimeData

    original = QMimeData()
    current = QApplication.clipboard().mimeData()
    if current is not None:
        for fmt in current.formats():
            original.setData(fmt, current.data(fmt))
    mime = QMimeData()
    mime.setData(EDRAW_CLIPBOARD_MIME, b'<Clipboard><Shapes><Shape Type="MainIdea" ID="1">'
                 b'<Text><HtmlText>&lt;p&gt;Edraw&lt;/p&gt;</HtmlText></Text></Shape></Shapes></Clipboard>')
    QApplication.clipboard().setMimeData(mime)
    try:
        assert window.canvas.edraw_paste_options == ("append", True, True)
        monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                            lambda *args, **kwargs: EdrawPasteOptions("replace", False, replace_root))
        window.canvas.paste()
        assert window.preferences.value("clipboard/edraw_mode") == "replace"
        assert window.preferences.value("clipboard/edraw_keep_children", type=bool) is False
        assert window.preferences.value("clipboard/edraw_replace_root", type=bool) is replace_root
        expected = ("replace", False, replace_root)
        preference_keys = ("clipboard/edraw_mode", "clipboard/edraw_keep_children", "clipboard/edraw_replace_root")
        monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options", lambda *args, **kwargs: None)
        assert window.canvas.paste() == []
        assert window.canvas.edraw_paste_options == expected
        assert tuple(window.preferences.value(key) for key in preference_keys) == expected
        before = window.engine.document
        monkeypatch.setattr("mindmap_qt_canvas.widget.edraw_paste_options",
                            lambda *args, **kwargs: EdrawPasteOptions("append", True, not replace_root))
        window.engine.before_operation = lambda operation, args: operation != "paste_fragment"
        with pytest.raises(MindMapError):
            window.canvas.paste()
        window.engine.before_operation = None
        assert window.engine.document == before
        assert window.canvas.edraw_paste_options == expected
        assert tuple(window.preferences.value(key) for key in preference_keys) == expected
        restored = build_window(storage_dir=window.manager.storage_dir, restore=False)
        qtbot.addWidget(restored)
        try:
            assert restored.new_document(window.manager.storage_dir / "粘贴偏好.mindmap")
            assert restored.canvas.edraw_paste_options == expected
            assert not restored.engine.is_dirty and not restored.engine.can_undo
            assert not restored.engine.document.metadata
        finally:
            restored.close()
    finally:
        if QApplication.platformName() == "offscreen":
            QApplication.clipboard().clear()
        else:
            QApplication.clipboard().setMimeData(original)


@pytest.mark.parametrize(("mode", "keep", "replace_root", "expected"), [
    ("invalid", "invalid", "invalid", ("append", True, True)),
    (None, 17, 17, ("append", True, True)),
    ("replace", "false", None, ("replace", False, True)),
    ("append", "true", "true", ("append", True, True)),
    ("replace", False, "false", ("replace", False, False)),
    ("replace", True, False, ("replace", True, False)),
    ("append", True, 0, ("append", True, True)),
])
def test_edraw_paste_preferences_validate_saved_values(window, qtbot, mode, keep, replace_root, expected):
    """兼容旧 INI 缺项及布尔字符串，损坏的方式和两个开关各自恢复默认，不影响启动。"""
    window.preferences.setValue("clipboard/edraw_mode", mode)
    window.preferences.setValue("clipboard/edraw_keep_children", keep)
    if replace_root is not None:
        window.preferences.setValue("clipboard/edraw_replace_root", replace_root)
    window.preferences.sync()
    restored = build_window(storage_dir=window.manager.storage_dir, restore=False)
    qtbot.addWidget(restored)
    try:
        assert restored.new_document(window.manager.storage_dir / "粘贴校验.mindmap")
        assert restored.canvas.edraw_paste_options == expected
    finally:
        restored.close()


def test_panel_and_file_icons_stay_distinct_and_available_after_theme_changes(window):
    """核对真实菜单与侧栏的图标，避免资源删除、主题切换后空白或不同操作混用图形。"""
    manager = window.manager
    for theme in ("light", "dark"):
        manager.set_theme(theme)
        menu = window.main_menu()
        file_menu = next(action.menu() for action in menu.actions() if action.text() == "文件")
        actions = [action for action in file_menu.actions() if not action.isSeparator()]
        assert all(not action.icon().isNull() for action in actions)
        assert len({action.property("mindmap_icon") for action in actions}) == len(actions)
        for action in actions:
            expected = manager.theme.icon(action.property("mindmap_icon")).pixmap(20, 20).toImage()
            assert action.icon().pixmap(20, 20).toImage() == expected
        images = []
        for tool_id in ("outline", "properties"):
            spec = manager.specs[tool_id]
            assert spec.icon == tool_id
            icon = manager.theme.icon(spec.icon)
            assert not icon.isNull()
            images.append(icon.pixmap(20, 20).toImage())
        assert all(left != right for index, left in enumerate(images) for right in images[index + 1:])
        menu.deleteLater()


def test_integrated_history_actions_keep_buttons_shortcuts_and_readonly(window, qtbot, tmp_path):
    """按钮和键盘共享所属会话的历史动作；关闭后新建使用独立实例。"""
    canvas, engine = window.canvas, window.engine
    group, _ = window.host.editor.find_token(window.active_session.id)
    toolbar = group.cornerWidget()
    assert canvas.toolbar.isHidden()
    assert toolbar.actions() == [canvas.action("undo"), canvas.action("redo")]
    engine.add_child(engine.root_id, "验证撤销与重做")
    qtbot.mouseClick(toolbar.widgetForAction(canvas.action("undo")), Qt.MouseButton.LeftButton)
    assert len(engine.document.nodes) == 1
    qtbot.mouseClick(toolbar.widgetForAction(canvas.action("redo")), Qt.MouseButton.LeftButton)
    assert len(engine.document.nodes) == 2
    window.activateWindow()
    canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(canvas.view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(engine.document.nodes) == 1
    canvas.begin_edit(engine.root_id)
    assert not toolbar.widgetForAction(canvas.action("undo")).isEnabled()
    canvas.finish_edit(commit=False)
    canvas.set_editable(False)
    assert all(not toolbar.widgetForAction(action).isEnabled() for action in toolbar.actions())
    assert canvas.action("fit").isEnabled() and canvas.action("center").isEnabled()
    engine.mark_saved()
    assert window.manager.close_document(window.active_session.id)
    assert canvas._disposed and window.canvas is None
    assert window.new_document(tmp_path / "新历史.mindmap")
    current = window.canvas
    group, _ = window.host.editor.find_token(window.active_session.id)
    assert group.cornerWidget().actions() == [current.action("undo"), current.action("redo")]
    assert current is not canvas and not window.engine.can_undo
    assert current._view_controls is None and current.toolbar.isHidden()
    assert window._status_controls.buttons["fit"].defaultAction() is current.action("fit")


def test_status_controls_follow_window_theme_and_stay_out_of_export(window, qtbot, tmp_path):
    """状态栏按钮随窗口调整，消息不会覆盖入口，主题与紧凑模式不影响图形导出。"""
    canvas = window.canvas
    controls = window._status_controls
    buttons = controls.buttons
    assert canvas._view_controls is None
    assert buttons["fit"].defaultAction() is canvas.action("fit")
    assert buttons["reset_zoom"].defaultAction() is canvas.action("reset_zoom")
    assert buttons["center"].defaultAction() is canvas.action("center")
    assert list(buttons) == ["fit", "reset_zoom", "center", "wheel_mode"]
    assert not any("思维导图工作台" in label.text() for label in window.status.findChildren(QLabel))
    document, revision = window.engine.document, window.engine.revision
    for size in ((1200, 800), (860, 540), (1440, 900)):
        window.resize(*size)
        QApplication.processEvents()
        assert controls.isVisible() and window.status.rect().contains(controls.geometry())
        # 文档正文从标签栏下方直接开始，隐藏工具栏没有残留高度。
    assert canvas.path_navigation.mapTo(canvas, QPoint(0, 0)).y() == 0
    assert canvas._view_card.mapTo(canvas, QPoint(0, 0)).y() == canvas.path_navigation.height()
    canvas.view.zoom(1.3)
    qtbot.mouseClick(buttons["fit"], Qt.MouseButton.LeftButton)
    scale = canvas.view.transform().m11()
    canvas.view.centerOn(500, 500)
    qtbot.mouseClick(buttons["center"], Qt.MouseButton.LeftButton)
    assert canvas.view.transform().m11() == scale
    root = QPointF(*canvas.layout_result.nodes[canvas.engine.root_id].center)
    visible_center = canvas.view.mapToScene(canvas.view.viewport().rect()).boundingRect().center()
    assert (visible_center - root).manhattanLength() < 2 / scale
    for theme in ("light", "dark"):
        window.manager.set_theme(theme)
        QApplication.processEvents()
        assert controls._colors == canvas._ui_colors
        assert window.engine.document == document and window.engine.revision == revision
        visible, hidden = tmp_path / f"{theme}-显示按钮.png", tmp_path / f"{theme}-隐藏按钮.png"
        canvas.export(visible)
        controls.hide()
        canvas.export(hidden)
        assert QImage(str(visible)) == QImage(str(hidden))
        controls.show()
        for compact in (True, False):
            window.manager.set_option("compact", compact)
            window.show_status_message("状态消息" * 100)
            QApplication.processEvents()
            assert window.status.height() == (24 if compact else 28)
            assert window.status.rect().contains(controls.geometry())
            assert all(button.isVisible() and controls.rect().contains(button.geometry())
                       for button in buttons.values())
            assert window.engine.document == document and window.engine.revision == revision


@pytest.mark.parametrize("scale", [0.1, 0.4, 2.0, 4.0])
def test_status_reset_zoom_preserves_view_and_document(window, qtbot, scale):
    """真实点击恢复原始尺寸并保留查看位置，重复操作不修改文档、选择和历史。"""
    canvas, view = window.canvas, window.canvas.view
    button = window._status_controls.buttons["reset_zoom"]
    assert button.toolTip() == "恢复默认尺寸（100%）"
    assert button.accessibleName() == "恢复默认尺寸"
    assert not canvas.action("reset_zoom").shortcuts()
    canvas.select_nodes([canvas.engine.root_id])
    view.zoom(scale / view.transform().m11())
    root = QPointF(*canvas.layout_result.nodes[canvas.engine.root_id].center)
    view.update_navigation_rect(center=root + QPointF(100, -80))
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    document, revision = window.engine.document, window.engine.revision
    selected = canvas.selected_node_ids()
    history = window.engine.can_undo, window.engine.can_redo
    for _ in range(2):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert view.transform().m11() == pytest.approx(1.0)
        actual = view.mapToScene(view.viewport().rect()).boundingRect().center()
        assert (actual - center).manhattanLength() < 2
        assert (actual - root).manhattanLength() > 100
        assert canvas.selected_node_ids() == selected
        assert window.engine.document == document and window.engine.revision == revision
        assert (window.engine.can_undo, window.engine.can_redo) == history
        assert not window.files.is_dirty


def test_status_controls_route_buttons_and_shortcuts_to_active_split(window, qtbot, tmp_path, monkeypatch):
    """分屏与标签反复切换后按钮只执行一次活动会话动作，原快捷键仍在画布生效。"""
    first = window.active_session
    assert window.new_document(tmp_path / "分屏按钮.mindmap")
    second = window.active_session
    window.host.editor.split_token(first.id, Qt.Orientation.Horizontal)
    fit_calls, reset_calls, center_calls = [], [], []
    for session in (first, second):
        monkeypatch.setattr(session.canvas, "fit_to_view", lambda selected=session: fit_calls.append(selected.id))
        monkeypatch.setattr(session.canvas, "reset_zoom", lambda selected=session: reset_calls.append(selected.id))
        monkeypatch.setattr(session.canvas, "center_root", lambda selected=session: center_calls.append(selected.id))
    controls = window._status_controls
    window.activateWindow()
    expected = []
    for index, session in enumerate((first, second, first), start=1):
        session.canvas.view.setFocus()
        qtbot.waitUntil(lambda: window.active_session is session)
        for key in ("fit", "reset_zoom", "center"):
            assert controls.buttons[key].actions() == [session.canvas.action(key)]
            qtbot.mouseClick(controls.buttons[key], Qt.MouseButton.LeftButton)
        qtbot.keyClick(session.canvas.view, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        qtbot.keyClick(session.canvas.view, Qt.Key.Key_Home)
        expected.extend([session.id, session.id])
        assert fit_calls == center_calls == expected
        assert reset_calls == [first.id, second.id, first.id][:index]
    qtbot.mouseClick(controls.buttons["wheel_mode"], Qt.MouseButton.LeftButton)
    assert first.canvas.wheel_mode == second.canvas.wheel_mode == "zoom"
    assert window.preferences.value("view/wheel_mode") == "zoom"
    assert all(not item.files.is_dirty and not item.engine.can_undo for item in (first, second))


def test_status_wheel_button_changes_real_wheel_events(window, qtbot):
    """真实滚轮事件跟随按钮模式，Ctrl 覆盖普通滚动且不改变文档状态。"""
    canvas, wheel = window.canvas, window._status_controls.buttons["wheel_mode"]
    view = canvas.view
    before, revision = canvas.engine.document, canvas.engine.revision
    for mode, ctrl in (("scroll", False), ("scroll", True), ("zoom", False), ("scroll", False)):
        if canvas.wheel_mode != mode:
            qtbot.mouseClick(wheel, Qt.MouseButton.LeftButton)
        position = view.viewport().rect().center()
        scale, offset = view.transform().m11(), view.verticalScrollBar().value()
        event = QWheelEvent(QPointF(position), QPointF(view.viewport().mapToGlobal(position)),
                            QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(view.viewport(), event)
        if mode == "zoom" or ctrl:
            assert view.transform().m11() == pytest.approx(scale * 1.15)
        else:
            assert view.transform().m11() == scale and view.verticalScrollBar().value() < offset
    assert canvas.engine.document == before and canvas.engine.revision == revision
    assert not window.files.is_dirty


@pytest.mark.parametrize(("format_id", "label"), [("png", "PNG 图片"), ("svg", "SVG 矢量图"), ("pdf", "PDF 文档")])
@pytest.mark.parametrize("expanded", [False, True])
def test_graphic_export_menu_selects_format_and_keeps_fold_state(window, tmp_path, monkeypatch, format_id, label, expanded):
    """每个菜单项打开对应的单格式对话框，生成正确文件，折叠范围选择不改写文档。"""
    canvas, engine = window.canvas, window.engine
    engine.add_child(engine.root_id, "折叠子节点")
    canvas.action("collapse_all").trigger()
    before, revision = engine.document, engine.revision
    dialogs, ranges = [], []
    target = tmp_path / "导出图形"

    def select_file(*args):
        """记录真实菜单回调传入的标题、默认文件名和唯一格式，模拟省略扩展名。"""
        dialogs.append(args)
        return str(target), f"{label} (*.{format_id})"

    export = canvas.export

    def capture_range(path, *, expand_all):
        """记录范围参数后执行真实导出，避免只验证动作接线却没有生成有效文件。"""
        ranges.append(expand_all)
        return export(path, expand_all=expand_all)

    monkeypatch.setattr(QFileDialog, "getSaveFileName", select_file)
    monkeypatch.setattr(QMessageBox, "question", lambda *args:
                        QMessageBox.StandardButton.Yes if expanded else QMessageBox.StandardButton.No)
    monkeypatch.setattr(canvas, "export", capture_range)
    menu = window.main_menu()
    files = next(action.menu() for action in menu.actions() if action.text() == "文件")
    exports = next(action.menu() for action in files.actions() if action.text() == "导出图形")
    next(action for action in exports.actions() if action.data() == format_id).trigger()
    assert dialogs == [(window, "导出 " + label, f"思维导图.{format_id}", f"{label} (*.{format_id})")]
    assert ranges == [expanded]
    output = target.with_suffix("." + format_id)
    data = output.read_bytes()
    assert len(data) > 500
    if format_id == "png":
        assert not QImage(str(output)).isNull()
    elif format_id == "svg":
        assert b"<svg" in data
    else:
        assert data.startswith(b"%PDF-") and b"%%EOF" in data
    assert engine.document == before and engine.revision == revision
    assert window.files.path.name == "初始.mindmap" and window.files.is_dirty
    menu.deleteLater()


def test_graphic_export_cancel_or_wrong_suffix_does_not_write(window, tmp_path, monkeypatch):
    """取消文件选择不询问范围；扩展名不符时保留既有文件，不输出另一种格式。"""
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("不应询问导出范围"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    window.files.export_dialog("png")
    target = tmp_path / "保留.svg"
    target.write_bytes(b"original")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), "PNG 图片 (*.png)"))
    with pytest.raises(MindMapError, match="请使用 .png 扩展名"):
        window.files.export_dialog("png")
    assert target.read_bytes() == b"original" and not target.with_suffix(".png").exists()


def test_file_save_flushes_properties_and_inline_text(window, tmp_path):
    """正常保存提交属性和原地文字，更新保存标记且保留撤销能力。"""
    canvas, files = window.canvas, window.files
    window.panels.properties.fields["style.font_size"].setValue(22)
    files.path = tmp_path / "文档.mindmap"
    assert files.save()
    assert load_document(files.path).root.style["font_size"] == 22
    assert not files.is_dirty
    assert window.manager.documents[window.active_session.id]["title"] == "文档"
    canvas.begin_edit(window.engine.root_id)
    canvas.editor.setPlainText("原地文字")
    assert files.save()
    assert load_document(files.path).root.text == "原地文字"
    assert not files.is_dirty
    assert window.engine.undo()
    assert window.engine.document.root.text != "原地文字"
    assert files.is_dirty


def test_invalid_open_failed_save_and_cancel_keep_session(window, tmp_path, monkeypatch):
    before = window.engine.document
    bad = tmp_path / "损坏.mindmap"
    bad.write_bytes(b"broken")
    with pytest.raises(MindMapError):
        window.open_path(bad)
    assert window.engine.document == before and window.files.path.name == "初始.mindmap"
    window.engine.update_node(window.engine.root_id, text="未保存")
    target = tmp_path / "原文档.mindmap"
    target.write_bytes(b"original")
    window.files.path = target

    def fail(*args, **kwargs):
        raise OSError("磁盘写入失败")

    window.files.writer = fail
    assert not window.files.save()
    assert target.read_bytes() == b"original" and window.files.is_dirty
    assert "磁盘" in window.statusBar().currentMessage()
    window.panels.properties.fields["style.font_size"].setValue(22)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    assert not window.close()
    assert window.engine.document.root.style["font_size"] == 22
    assert not window._disposed and not window.canvas._disposed
    window.engine.add_child(window.engine.root_id, "取消后继续编辑")
    assert len(window.engine.document.nodes) == 2


@pytest.mark.parametrize("invalid", ["image", "layout"])
def test_open_rejects_invalid_canvas_before_save_confirmation(window, tmp_path, monkeypatch, invalid):
    """合法容器中的损坏图片或未知布局，不能触发保存确认或替换当前会话。"""
    child = window.engine.add_child(window.engine.root_id, "当前文档")
    window.files.path = tmp_path / "当前.mindmap"
    assert window.files.save()
    window.engine.update_node(child, text="仍需保留的修改")
    window.canvas.select_nodes([child])
    before, revision = window.engine.document, window.engine.revision
    path, selection = window.files.path, window.canvas.selected_node_ids()
    candidate = MindMapEngine()
    if invalid == "image":
        candidate.attach_image(candidate.root_id, b"invalid image bytes")
    else:
        candidate.set_layout(LayoutConfig(algorithm_id="not-installed"))
    source = tmp_path / "不可显示.mindmap"
    save_document(candidate.document, source)
    questions = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args: questions.append(args) or QMessageBox.StandardButton.Cancel,
    )
    with pytest.raises(MindMapError, match="图片" if invalid == "image" else "未注册布局"):
        window.open_path(source)
    assert not questions
    assert window.engine.document == before and window.engine.revision == revision
    assert window.canvas.selected_node_ids() == selection and window.files.path == path
    assert window.files.is_dirty


@pytest.mark.parametrize("native", ["missing", "read-only", "write-only", "lossy"])
def test_unavailable_native_format_reports_save_failure_without_dialog(window, monkeypatch, native):
    """即使 JSON 可完整读写，原生编解码器不可用也不能回退或弹出保存对话框。"""
    original = window.canvas.format_registry
    registry = FormatRegistry()
    json_entry = original.get("json")
    registry.register(json_entry.info, importer=json_entry.importer, exporter=json_entry.exporter)
    if native != "missing":
        entry = original.get("mindmap")
        registry.register(
            FormatInfo("mindmap", "思维导图", (".mindmap",), native != "lossy"),
            importer=None if native == "write-only" else entry.importer,
            exporter=None if native == "read-only" else entry.exporter,
        )
    monkeypatch.setattr(window.canvas, "format_registry", registry)
    before = window.engine.document
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: pytest.fail("不应弹出保存对话框"))
    monkeypatch.setattr(window.files, "writer", lambda *args, **kwargs: pytest.fail("不能回退写入 JSON"))
    assert not window.files.save()
    assert not window.files.save(save_as=True)
    assert not window.files.save_committed()
    assert "未注册可用的 .mindmap 原生编解码器" in window.statusBar().currentMessage()
    assert window.engine.document == before and window.files.path.name == "初始.mindmap"


class MenuLayout(TreeLayout):
    """用扩展算法验证菜单重建、能力限制与撤销，几何仍复用内置树布局。"""

    info = LayoutInfo("menu-test", "自定义菜单布局", LayoutConfig(algorithm_id="menu-test"))

    def validate_config(self, config):
        """该测试布局不接受额外选项，错误配置继续使用引擎统一异常。"""
        if config.options:
            raise MindMapError("菜单测试布局没有额外参数")

    def layout(self, document, measurements=None, **kwargs):
        """只在副本上切换内置布局，避免几何试算修改原文档。"""
        document = deepcopy(document)
        document.layout = LayoutConfig()
        return TreeLayout().layout(document, measurements, **kwargs)


def test_layout_extension_still_works_without_algorithm_menu(window):
    """工作台移除算法入口，底层注册、布局调用、能力限制和撤销仍然有效。"""
    window.canvas.layout_registry.register(MenuLayout())
    menu = window.main_menu()
    view = next(action.menu() for action in menu.actions() if action.text() == "视图")
    assert all(action.text() != "布局算法" for action in view.actions())
    window.canvas.set_layout(MenuLayout.info.default_config)
    assert window.engine.document.layout.algorithm_id == "menu-test"
    assert not window.panels.settings.direction.isEnabled()
    assert not window.panels.settings.compact.isEnabled()
    assert window.engine.undo()
    assert window.engine.document.layout.algorithm_id == "tree"
    assert window.panels.settings.direction.isEnabled() and window.panels.settings.compact.isEnabled()
    menu.deleteLater()


class RootCodec:
    """额外编解码器只供底层交换，任何能力组合都不能扩大应用文件范围。"""

    def read(self, stream, *, options):
        pytest.fail("扩展格式不能用于文件打开或保存")

    def write(self, document, stream, *, options):
        stream.write(document.root.text.encode("utf-8"))


def test_dynamic_formats_do_not_expand_opening_or_saving(window, tmp_path, monkeypatch):
    """完整扩展格式也不能扩大文件范围，与原生后缀冲突时仍明确使用原生编码。"""
    registry = window.canvas.format_registry
    registry.register(FormatInfo("root-read", "根节点导入", (".read",)), importer=RootCodec())
    registry.register(FormatInfo("root-write", "根节点导出", (".write",)), exporter=RootCodec())
    registry.register(FormatInfo("root-full", "完整扩展", (".full", ".mindmap"), True),
                      importer=RootCodec(), exporter=RootCodec())
    assert window.files.format_filters("open") == "思维导图 (*.mindmap)"
    assert window.files.format_filters("save") == "思维导图 (*.mindmap)"
    menu = window.main_menu()
    assert [action.text() for action in menu.actions()] == ["文件", "编辑", "视图", "设置"]
    menu.deleteLater()
    assert not hasattr(window.files, "export_document_dialog")
    assert not hasattr(window.files, "export_path")
    for purpose in ("export", "unknown"):
        with pytest.raises(MindMapError, match="未知文件对话框用途"):
            window.files.format_filters(purpose)
    source = tmp_path / "额外格式.read"
    source.write_text("额外内容", "utf-8")
    with pytest.raises(MindMapError, match="仅支持打开 .mindmap"):
        window.open_path(source)
    original_path = window.files.path
    before, revision = window.engine.document, window.engine.revision
    for suffix in (".json", ".write", ".full"):
        output = tmp_path / ("不支持保存" + suffix)
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(output), ""))
        assert not window.files.save(save_as=True)
        assert not output.exists() and window.files.path == original_path
        assert "仅支持保存为 .mindmap 文件" in window.statusBar().currentMessage()
    assert window.engine.document == before and window.engine.revision == revision
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *args: (str(tmp_path / "另存"), "完整扩展 (*.full *.mindmap)"))
    assert window.files.save(save_as=True)
    assert window.files.path.suffix == ".mindmap" and not window.files.is_dirty
    assert load_document(window.files.path, format_id="mindmap") == before
    assert window.open_path(window.files.path) and len(window.sessions) == 1
    assert window.new_document(tmp_path / "又一份.mindmap")
    assert len(window.manager.documents) == 2 and window.files.path.suffix == ".mindmap"


@pytest.mark.parametrize("invalid", ["", "unknown", 123, ["zoom"]])
def test_invalid_saved_wheel_mode_falls_back_to_scroll(window, qtbot, invalid):
    """损坏或类型错误的本机滚轮偏好回退为滚动，不修改导图或保存标记。"""
    window.preferences.setValue("view/wheel_mode", invalid)
    window.preferences.sync()
    storage = window.manager.storage_dir
    window.close()
    restored = build_window(storage_dir=storage)
    qtbot.addWidget(restored)
    try:
        assert restored.new_document(storage / "滚轮校验.mindmap")
        assert restored.canvas.wheel_mode == "scroll"
        assert restored.canvas.action("wheel_scroll").isChecked()
        assert not restored.files.is_dirty
    finally:
        restored.close()


def test_shell_theme_preserves_pending_input_document_and_export(window, tmp_path):
    panel, engine = window.panels.properties, window.engine
    before, revision = engine.document, engine.revision
    first = tmp_path / "原图.png"
    window.canvas.export(first)
    window.activateWindow()
    field = panel.fields["style.font_size"]
    field.lineEdit().setFocus()
    QApplication.processEvents()
    panel.fields["style.font_size"].setValue(25)
    window.manager.set_theme("dark")
    assert panel.fields["style.font_size"].value() == 25
    assert field.hasFocus()
    assert engine.document == before and engine.revision == revision
    assert panel.palette().color(panel.backgroundRole()).name() == window.manager.theme.color("tool-window-bg").name()
    # 工作台只覆盖视图底色并交出卡片轮廓；切换主题和后续文档刷新都不能改写导出背景。
    canvas = window.canvas
    assert canvas.view.mapTo(canvas._view_card, QPoint()) == QPoint()
    assert canvas.scene.backgroundBrush().color() == window.manager.theme.color("editor-bg")
    panel.discard_pending()
    second = tmp_path / "换外壳.png"
    window.canvas.export(second)
    assert QImage(str(first)) == QImage(str(second))
    engine.set_theme(Theme.dark())
    window.manager.set_theme("light")
    assert engine.document.theme == Theme.dark()
    assert canvas.scene.backgroundBrush().color() == window.manager.theme.color("editor-bg")
    assert window.panels.outline.palette().color(window.panels.outline.backgroundRole()).name() == "#ffffff"
    for suffix in ("svg", "pdf"):
        target = tmp_path / ("原图." + suffix)
        window.canvas.export(target)
        assert target.stat().st_size > 500


@pytest.mark.parametrize("mode", list(ViewMode))
def test_panel_modes_keep_instances_and_closed_session_is_disposed(window, mode, tmp_path):
    manager, panel = window.manager, window.panels.properties
    manager.set_view_mode("properties", mode)
    manager.move("properties", DockTarget(DockPosition.LEFT_BOTTOM))
    manager.activate("properties")
    assert manager.panel("properties").content.currentWidget() is panel
    manager.hide("properties")
    manager.activate("properties")
    assert manager.panel("properties").content.currentWidget() is panel
    canvas = window.canvas
    assert manager.close_document(window.active_session.id)
    assert window.canvas is None and canvas._disposed
    assert window.new_document(tmp_path / "释放后新建.mindmap")
    current = window.canvas
    assert manager.documents[window.active_session.id]["widget"] is current and current is not canvas
    window.dispose()
    window.dispose()
    assert not panel.timer.isActive() and not current._downloads


def test_preferences_restore_and_local_input_shortcuts(window, qtbot):
    window.canvas.set_wheel_mode("zoom")
    window.manager.move("outline", DockTarget(DockPosition.BOTTOM_LEFT))
    window.manager.activate("outline", focus=False)
    snapshot = window.manager.capture_layout()
    window.manager.restore_layout(snapshot)
    window.manager.activate("properties")
    field = window.panels.properties.fields["style.font_family"].lineEdit()
    window.activateWindow()
    field.setFocus()
    field.selectAll()
    qtbot.keyClicks(field, "Arial")
    qtbot.keyClick(field, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "Arial"
    qtbot.keyClick(field, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.engine.document.root.text == "中心主题"
    window.panels.properties.discard_pending()
    window.engine.mark_saved()
    storage = window.manager.storage_dir
    window.close()
    restored = build_window(storage_dir=storage)
    qtbot.addWidget(restored)
    try:
        assert restored.new_document(window.manager.storage_dir / "重启.mindmap")
        assert restored.canvas.wheel_mode == "zoom"
        assert restored.manager.states["outline"].position == DockPosition.BOTTOM_LEFT
        assert restored.manager.states["outline"].visible
        assert not restored.files.is_dirty
    finally:
        restored.close()


def test_save_shortcut_from_docked_input_flushes_once(window, qtbot, tmp_path):
    """停靠面板中的文件快捷键交给工作台；输入框仍拥有自己的文本快捷键。"""
    target = tmp_path / "面板保存.mindmap"
    window.files.path = target
    window.manager.activate("properties")
    field = window.panels.properties.fields["style.font_size"]
    window.activateWindow()
    field.lineEdit().setFocus()
    QApplication.processEvents()
    field.setValue(21)
    qtbot.keyClick(field.lineEdit(), Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(target.is_file)
    assert load_document(target).root.style["font_size"] == 21
    assert not window.files.is_dirty


def test_optional_ui_palette_restores_document_defaults(window):
    before = window.engine.document
    window.manager.set_theme("dark")
    window.canvas.set_ui_colors(None)
    window.panels.set_ui_colors(None)
    assert window.canvas._ui_colors.shell == "#e9eaee"
    assert window.engine.document == before
    with pytest.raises(TypeError):
        window.canvas.set_ui_colors("dark")
    with pytest.raises(TypeError):
        window.panels.set_ui_colors("dark")


def test_escape_keeps_canvas_cancel_without_changing_navigation(window, qtbot):
    """空闲 Escape 保留导航临时显示，编辑时仍取消草稿，侧栏焦点流转继续可用。"""
    canvas, engine = window.canvas, window.engine
    child = engine.add_child(engine.root_id, "分支主题")
    engine.set_expanded(engine.root_id, False)
    canvas.navigate_to(None, child)
    window.activateWindow()
    canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert canvas.focus_id == child and canvas.selected_node_ids() == [child]
    canvas.navigate_to(None, engine.root_id)
    original = engine.document.root.text
    canvas.begin_edit(engine.root_id)
    canvas.editor.setPlainText("应当取消的草稿")
    qtbot.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert canvas.editor is None and engine.document.root.text == original
    window.manager.activate("properties")
    field = window.panels.properties.fields["style.font_family"].lineEdit()
    field.setFocus()
    qtbot.keyClick(field, Qt.Key.Key_F6)
    assert field.hasFocus()
    window.manager.hide("properties")
    assert canvas.view.hasFocus()
