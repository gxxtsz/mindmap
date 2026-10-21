"""工作台真实控件集成：文件、扩展、焦点、主题与停靠生命周期。"""

from copy import deepcopy

import pytest
from mindmap_docking import DockPosition, DockTarget, ViewMode
from mindmap_engine import (
    FormatInfo,
    FormatRegistry,
    LayoutConfig,
    LayoutInfo,
    MindMapDocument,
    MindMapEngine,
    MindMapError,
    Theme,
    TreeLayout,
    load_document,
    save_document,
)
from mindmap_workbench.shell_adapter import DOCUMENT_ID, build_window
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QEnterEvent, QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QToolButton


@pytest.fixture
def window(qtbot, tmp_path):
    widget = build_window(storage_dir=tmp_path / "配置", restore=False)

    def prepare_close(item):
        item.engine.mark_saved()
        item.files._unsaved_import = False

    qtbot.addWidget(widget, before_close_func=prepare_close)
    widget.show()
    QApplication.processEvents()
    yield widget
    prepare_close(widget)
    widget.close()


def test_default_single_document_and_editable_framework(window):
    assert len(window.engine.document.nodes) == 1
    assert window.engine.document.root.style["branch"] == {"kind": "round"}
    assert not window.engine.can_undo and not window.files.is_dirty
    assert window.panels.settings.branch.currentData() == "round"
    assert list(window.manager.documents) == [DOCUMENT_ID]
    assert window.manager.documents[DOCUMENT_ID]["widget"] is window.canvas
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


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_top_left_menu_contains_shared_settings_and_existing_options(window, qtbot, theme_name):
    """明暗主题下点击左上角主菜单，核对设置子菜单中的三组平铺选项。"""
    window.manager.set_theme(theme_name)
    expected = [
        "导图设置", None, "浅色", "深色", "紧凑模式", None,
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
            assert menu.actions()[0].menu() is window.settings_menu
            for action in menu.actions()[2:4]:
                assert action.isChecked() == (action.text() == ("浅色" if theme_name == "light" else "深色"))
            assert all(action.menu() is None for action in menu.actions()[2:])
            menu.close()
        finally:
            main.close()

    QTimer.singleShot(0, inspect_menu)
    qtbot.mouseClick(window.title_bar.menu, Qt.MouseButton.LeftButton)
    assert popped == [expected]
    assert set(window.manager.specs) == {"outline", "properties"}
    main = window.main_menu()
    settings = next(action.menu() for action in main.actions() if action.text() == "设置")
    assert [None if action.isSeparator() else action.text() for action in settings.actions()] == expected
    assert [action.text() for action in main.actions()] == ["文件", "编辑", "视图", "导出文档", "设置"]
    view = next(action.menu() for action in main.actions() if action.text() == "视图")
    assert all("滚轮" not in action.text() and "布局算法" != action.text() for action in view.actions())
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


def test_settings_shortcut_and_real_menu_click_preserve_properties(window, qtbot):
    """Alt+3 与真实子菜单点击沿用整图事务，且不隐藏节点属性。"""
    window.activateWindow()
    window.canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(window.canvas.view, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
    menu = window.settings_menu
    assert menu.isVisible() and window.panels.properties.isVisible()
    directions = next(action.menu() for action in menu.actions() if action.menu() and action.menu().objectName() == "settings_direction")
    directions.popup(menu.mapToGlobal(QPoint(menu.width(), 0)))
    action = next(action for action in directions.actions() if action.data() == "left")
    qtbot.mouseClick(directions, Qt.MouseButton.LeftButton, pos=directions.actionGeometry(action).center())
    assert window.engine.document.layout.direction == "left"
    assert window.manager.states["properties"].visible
    assert window.engine.undo() and window.engine.document.layout.direction == "right"


def test_menu_wheel_preference_survives_restart_without_document_changes(window, qtbot):
    """通过菜单改变本机滚轮偏好，重启后恢复但不写入文档和撤销历史。"""
    before, revision = window.engine.document, window.engine.revision
    window.show_settings_menu()
    wheel = next(action.menu() for action in window.settings_menu.actions() if action.menu() and action.menu().objectName() == "settings_wheel")
    next(action for action in wheel.actions() if action.data() == "zoom").trigger()
    window.settings_menu.close()
    assert window.canvas.wheel_mode == "zoom"
    assert window.engine.document == before and window.engine.revision == revision
    assert not window.files.is_dirty
    window.close()
    restored = build_window(storage_dir=window.manager.storage_dir)
    qtbot.addWidget(restored)
    try:
        assert restored.canvas.wheel_mode == "zoom" and not restored.files.is_dirty
        assert "settings" not in restored.manager.specs
    finally:
        restored.close()


def test_legacy_settings_tool_is_ignored_in_saved_session(window, qtbot):
    """带命名布局字段的旧会话仍可加载，已移除的设置浮窗不会恢复。"""
    snapshot = window.manager.capture_layout()
    legacy = deepcopy(next(item for item in snapshot["tools"] if item["id"] == "properties"))
    legacy.update(id="settings", visible=True, mode="float")
    snapshot["tools"].append(legacy)
    snapshot["active"] = "settings"
    window.close()
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
        assert restored.panels.properties.isVisible()
        restored.manager.restore_layout(snapshot)
        assert "settings" not in restored.manager.states
        assert restored.manager.states["properties"].visible
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
    assert button.defaultAction() is tree.depth_action
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
    assert window.new_document() and tree.display_depth == depth
    document = MindMapDocument.new("待打开的导图")
    engine = MindMapEngine(document)
    child = engine.add_child(engine.root_id, "直接子节点")
    engine.add_child(child, "更深子节点")
    path = tmp_path / "层级验收.mindmap"
    save_document(engine.document, path)
    assert window.open_path(path) and tree.display_depth == depth
    assert tree.isExpanded(tree.tree_model.index_for(window.engine.root_id)) == (depth != 1)
    window.engine.mark_saved()
    window.close()
    restored = build_window(storage_dir=window.manager.storage_dir, restore=False)
    qtbot.addWidget(restored)
    try:
        assert restored.panels.outline.display_depth == depth
        assert restored.open_path(path)
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
    assert restored.panels.outline.display_depth == 2
    restored.close()


def test_edraw_paste_preferences_are_saved_only_after_success(window, monkeypatch, qtbot):
    """真实粘贴保存两项偏好；取消不覆盖，重建工作台从相同配置目录恢复。"""
    from mindmap_qt.clipboard import EDRAW_CLIPBOARD_MIME
    from mindmap_qt.dialogs import EdrawPasteOptions
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
        assert window.canvas.edraw_paste_options == ("append", True)
        monkeypatch.setattr("mindmap_qt.widget.edraw_paste_options",
                            lambda *args, **kwargs: EdrawPasteOptions("replace", False))
        window.canvas.paste()
        assert window.preferences.value("clipboard/edraw_mode") == "replace"
        assert window.preferences.value("clipboard/edraw_keep_children", type=bool) is False
        monkeypatch.setattr("mindmap_qt.widget.edraw_paste_options", lambda *args, **kwargs: None)
        assert window.canvas.paste() == []
        restored = build_window(storage_dir=window.manager.storage_dir, restore=False)
        qtbot.addWidget(restored)
        try:
            assert restored.canvas.edraw_paste_options == ("replace", False)
            assert not restored.engine.is_dirty and not restored.engine.can_undo
            assert not restored.engine.document.metadata
        finally:
            restored.close()
    finally:
        if QApplication.platformName() == "offscreen":
            QApplication.clipboard().clear()
        else:
            QApplication.clipboard().setMimeData(original)


@pytest.mark.parametrize(("mode", "keep", "expected"), [
    ("invalid", "invalid", ("append", True)),
    (None, 17, ("append", True)),
    ("replace", "false", ("replace", False)),
    ("append", "true", ("append", True)),
])
def test_edraw_paste_preferences_validate_saved_values(window, qtbot, mode, keep, expected):
    """兼容 INI 布尔值，损坏的方式和开关各自恢复默认，不影响导图或启动。"""
    window.preferences.setValue("clipboard/edraw_mode", mode)
    window.preferences.setValue("clipboard/edraw_keep_children", keep)
    window.preferences.sync()
    restored = build_window(storage_dir=window.manager.storage_dir, restore=False)
    qtbot.addWidget(restored)
    try:
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


def test_integrated_history_actions_keep_buttons_shortcuts_and_readonly(window, qtbot):
    """按钮和键盘共享真实历史动作，原地编辑、只读及重开标签不产生额外入口。"""
    canvas, engine = window.canvas, window.engine
    group, _ = window.host.editor.find_token(DOCUMENT_ID)
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
    assert window.manager.close_document(DOCUMENT_ID)
    window.show_document()
    group, _ = window.host.editor.find_token(DOCUMENT_ID)
    assert group.cornerWidget().actions() == [canvas.action("undo"), canvas.action("redo")]
    assert canvas._view_controls.isVisible() and canvas.toolbar.isHidden()


def test_canvas_controls_follow_viewport_theme_and_stay_out_of_export(window, qtbot, tmp_path):
    """浮条固定在可见视口中，窗口与侧栏变化后仍可点按，界面外观不进入图形导出。"""
    canvas = window.canvas
    controls = canvas._view_controls
    buttons = {button.defaultAction(): button for button in controls.findChildren(QToolButton)}
    assert set(buttons) == {canvas.action("fit"), canvas.action("center")}
    document, revision = window.engine.document, window.engine.revision
    for size in ((1200, 800), (860, 540), (1440, 900)):
        window.resize(*size)
        QApplication.processEvents()
        viewport = canvas.view.viewport()
        position = controls.pos()
        assert controls.isVisible() and viewport.geometry().contains(controls.geometry())
        assert viewport.geometry().right() - controls.geometry().right() == 16
        assert viewport.geometry().bottom() - controls.geometry().bottom() == 16
        # 文档正文从标签栏下方直接开始，隐藏工具栏没有残留高度。
        assert canvas._view_card.mapTo(canvas, QPoint(0, 0)).y() == 0
    canvas.view.zoom(1.3)
    qtbot.mouseClick(buttons[canvas.action("fit")], Qt.MouseButton.LeftButton)
    scale = canvas.view.transform().m11()
    canvas.view.centerOn(500, 500)
    assert controls.pos() == position
    qtbot.mouseClick(buttons[canvas.action("center")], Qt.MouseButton.LeftButton)
    assert canvas.view.transform().m11() == scale
    for theme in ("light", "dark"):
        window.manager.set_theme(theme)
        QApplication.processEvents()
        assert controls._colors == canvas._ui_colors
        assert window.engine.document == document and window.engine.revision == revision
        visible, hidden = tmp_path / f"{theme}-显示浮条.png", tmp_path / f"{theme}-隐藏浮条.png"
        canvas.export(visible)
        controls.hide()
        canvas.export(hidden)
        assert QImage(str(visible)) == QImage(str(hidden))
        controls.show()


def test_file_save_flushes_properties_and_inline_text(window, tmp_path):
    canvas, files = window.canvas, window.files
    window.panels.properties.fields["style.font_size"].setValue(22)
    files.path = tmp_path / "文档.mindmap"
    assert files.save()
    assert load_document(files.path).root.style["font_size"] == 22
    assert not files.is_dirty
    assert window.manager.documents[DOCUMENT_ID]["title"] == "文档.mindmap"
    canvas.begin_edit(window.engine.root_id)
    canvas.editor.setPlainText("原地文字")
    target = tmp_path / "导出.json"
    files.export_path(target, format_id="json")
    assert load_document(target).root.text == "原地文字"
    assert files.is_dirty
    assert window.engine.undo()
    assert window.engine.document.root.text != "原地文字"


def test_invalid_open_failed_save_and_cancel_keep_session(window, tmp_path, monkeypatch):
    before = window.engine.document
    bad = tmp_path / "损坏.mindmap"
    bad.write_bytes(b"broken")
    with pytest.raises(MindMapError):
        window.open_path(bad)
    assert window.engine.document == before and window.files.path is None
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


def test_no_lossless_format_reports_save_failure_without_dialog(window, monkeypatch):
    """没有完整往返格式时直接报告失败，不弹出无法完成保存的文件对话框。"""
    window.canvas.format_registry = FormatRegistry()
    before = window.engine.document
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: pytest.fail("不应弹出保存对话框"))
    assert not window.files.save()
    assert "未注册" in window.statusBar().currentMessage()
    assert window.engine.document == before and window.files.path is None


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
    """仅用于验证有损导入与单向导出能力的独立格式。"""

    def read(self, stream, *, options):
        return MindMapDocument.new(stream.read().decode("utf-8"))

    def write(self, document, stream, *, options):
        stream.write(document.root.text.encode("utf-8"))


def test_dynamic_formats_lossy_import_and_save_as(window, tmp_path, monkeypatch):
    registry = window.canvas.format_registry
    registry.register(FormatInfo("root-read", "根节点导入", (".read",)), importer=RootCodec())
    registry.register(FormatInfo("root-write", "根节点导出", (".write",)), exporter=RootCodec())
    assert "*.read" in window.files.format_filters("open") and "*.write" not in window.files.format_filters("open")
    assert "*.read" not in window.files.format_filters("save") and "*.write" not in window.files.format_filters("save")
    menu = window.main_menu()
    exports = next(action.menu() for action in menu.actions() if action.text() == "导出文档")
    assert "root-write" in [action.data() for action in exports.actions()]
    assert "root-read" not in [action.data() for action in exports.actions()]
    source = tmp_path / "有损.read"
    source.write_text("导入内容", "utf-8")
    assert window.open_path(source)
    assert window.files.path is None and window.files.is_dirty
    before, revision = window.engine.document, window.engine.revision
    output = tmp_path / "导出.write"
    window.files.export_path(output)
    assert output.read_text("utf-8") == "导入内容" and window.files.is_dirty
    assert window.engine.document == before and window.engine.revision == revision
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *args: (str(tmp_path / "另存"), "JSON 文档 (*.json)"))
    assert window.files.save()
    assert window.files.path.suffix == ".json" and not window.files.is_dirty
    assert window.new_document()
    assert len(window.manager.documents) == 1 and window.files.path is None


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
    panel.discard_pending()
    second = tmp_path / "换外壳.png"
    window.canvas.export(second)
    assert QImage(str(first)) == QImage(str(second))
    engine.set_theme(Theme.dark())
    window.manager.set_theme("light")
    assert engine.document.theme == Theme.dark()
    assert window.panels.outline.palette().color(window.panels.outline.backgroundRole()).name() == "#ffffff"
    for suffix in ("svg", "pdf"):
        target = tmp_path / ("原图." + suffix)
        window.canvas.export(target)
        assert target.stat().st_size > 500


@pytest.mark.parametrize("mode", list(ViewMode))
def test_panel_modes_and_closed_document_keep_instances(window, mode):
    manager, panel = window.manager, window.panels.properties
    manager.set_view_mode("properties", mode)
    manager.move("properties", DockTarget(DockPosition.LEFT_BOTTOM))
    manager.activate("properties")
    assert manager.panel("properties").content is panel
    manager.hide("properties")
    manager.activate("properties")
    assert manager.panel("properties").content is panel
    assert manager.close_document(DOCUMENT_ID)
    assert not window.canvas._disposed
    window.show_document()
    assert manager.documents[DOCUMENT_ID]["widget"] is window.canvas
    window.dispose()
    window.dispose()
    assert not panel.timer.isActive() and not window.canvas._downloads


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


def test_escape_keeps_canvas_cancel_and_unfocus_semantics(window, qtbot):
    canvas, engine = window.canvas, window.engine
    child = engine.add_child(engine.root_id, "分支主题")
    canvas.focus_node(child)
    window.activateWindow()
    canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(canvas.view, Qt.Key.Key_Escape)
    assert canvas.focus_id is None
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
