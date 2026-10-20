"""工作台真实控件集成：文件、扩展、焦点、主题与停靠生命周期。"""

from copy import deepcopy

import pytest
from harbor_docking import DockPosition, DockTarget, ViewMode
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
from mindmap_workbench.harbor_adapter import DOCUMENT_ID, build_window
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QToolButton


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
    assert list(window.manager.documents) == [DOCUMENT_ID]
    assert window.manager.documents[DOCUMENT_ID]["widget"] is window.canvas
    assert window.manager.states["properties"].visible
    assert window.panels.properties.isVisible()
    assert window.panels.properties.fields["style.font_size"].isVisible()
    assert not window.manager.states["outline"].visible
    assert not window.manager.states["settings"].visible
    assert window.host.region_sizes["dock"]["right"] == 360
    bar = window.title_bar
    assert all(bar.box.indexOf(button) < bar.box.indexOf(bar.theme) for button in bar.action_buttons)


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


def test_dynamic_layout_menu_capabilities_readonly_and_history(window):
    """算法菜单读取实时注册表；不支持的设置及只读操作应正确禁用。"""
    window.canvas.layout_registry.register(MenuLayout())
    menu = window.main_menu()
    view = next(action.menu() for action in menu.actions() if action.text() == "视图")
    algorithms = next(action.menu() for action in view.actions() if action.text() == "布局算法")
    action = next(action for action in algorithms.actions() if action.data() == "menu-test")
    assert action.text() == "自定义菜单布局"
    action.trigger()
    assert window.engine.document.layout.algorithm_id == "menu-test"
    assert not window.panels.settings.direction.isEnabled()
    assert not window.panels.settings.compact.isEnabled()
    assert window.engine.undo()
    assert window.engine.document.layout.algorithm_id == "tree"
    assert window.panels.settings.direction.isEnabled() and window.panels.settings.compact.isEnabled()
    window.canvas.set_editable(False)
    readonly_menu = window.main_menu()
    view = next(action.menu() for action in readonly_menu.actions() if action.text() == "视图")
    algorithms = next(action.menu() for action in view.actions() if action.text() == "布局算法")
    assert all(not action.isEnabled() for action in algorithms.actions())


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


def test_save_shortcut_from_floating_input_flushes_once(window, qtbot, tmp_path):
    """独立浮窗中的文件快捷键交给工作台；输入框仍拥有自己的文本快捷键。"""
    target = tmp_path / "浮窗保存.mindmap"
    window.files.path = target
    window.manager.set_view_mode("properties", ViewMode.WINDOW)
    window.manager.activate("properties")
    field = window.panels.properties.fields["style.font_size"]
    floating = window.manager.floating["properties"]
    floating.activateWindow()
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
    assert canvas.view.hasFocus()
