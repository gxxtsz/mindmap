"""卡片表面、主题切换、嵌入隔离及真实停靠预览几何的回归检查。"""

import pytest
from mindmap_engine import MindMapEngine, Theme
from mindmap_qt import MindMapWidget, MindMapWorkspace
from mindmap_qt.properties import NodePropertiesForm
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget


def pixel(widget, point):
    """按实际像素比采样界面，避免把物理坐标误当成高 DPI 下的逻辑坐标。"""
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    return image.pixelColor(round(point.x() * ratio), round(point.y() * ratio))


def test_cards_switch_theme_repeatedly_and_preserve_document_background(qtbot):
    """反复切换后采样三个侧栏的标题、内容、滚动空白与底部，并保留弹窗层次。"""
    workspace = MindMapWorkspace()
    qtbot.addWidget(workspace)
    workspace.resize(1200, 800)
    workspace.show()
    workspace.show_tool_window("outline", focus=False)
    form = NodePropertiesForm()
    qtbot.addWidget(form)
    form.resize(360, 620)
    form.show()
    try:
        for theme, shell, surface, field, dialog in (
            (Theme(), "#e9eaee", "#f5f6f8", "#ffffff", "#ffffff"),
            (Theme.dark(), "#1e1f22", "#2b2d30", "#303237", "#2b2d30"),
            (Theme(), "#e9eaee", "#f5f6f8", "#ffffff", "#ffffff"),
        ):
            workspace.canvas.engine.set_theme(theme)
            workspace.show_tool_window("properties", focus=False)
            form.set_theme(theme)
            QApplication.processEvents()
            canvas = workspace.canvas
            assert pixel(canvas, QPoint(0, 0)) == QColor(shell)
            assert pixel(canvas, QPoint(20, 1)) == QColor(theme.background)
            dock = workspace.tools["properties"].dock
            assert pixel(workspace, dock.mapTo(workspace, QPoint(0, 0))) == QColor(shell)
            assert pixel(workspace, dock.mapTo(workspace, QPoint(180, 18))) == QColor(surface)
            assert dock.x() - (canvas.x() + canvas.width()) == 6
            assert canvas.scene.backgroundBrush().color() == QColor(theme.background)
            assert workspace.properties.fields["branch.kind"].count() == 12
            for tool_id in ("outline", "properties", "settings"):
                workspace.show_tool_window(tool_id, focus=False)
                QApplication.processEvents()
                dock = workspace.tools[tool_id].dock
                panel = getattr(workspace, tool_id)
                viewport = panel.viewport() if tool_id == "outline" else panel.scroll.viewport()
                # 表单从页边距采样，树从末尾空白采样；不能只检查调色板或样式字符串。
                blank = (
                    QPoint(viewport.width() - 2, viewport.height() - 3)
                    if tool_id == "outline"
                    else QPoint(1, 1)
                )
                assert pixel(workspace, dock.mapTo(workspace, QPoint(0, 0))) == QColor(shell)
                assert pixel(dock, QPoint(dock.width() // 2, 12)) == QColor(surface)
                assert pixel(viewport, blank) == QColor(surface)
                assert pixel(dock, QPoint(dock.width() // 2, dock.height() - 3)) == QColor(surface)
                if tool_id != "outline":
                    assert pixel(panel, QPoint(1, 1)) == QColor(surface)
                    control = panel.wheel if tool_id == "settings" else panel.fields["style.font_family"]
                    assert pixel(control, QPoint(control.width() // 2, 2)) == QColor(field)
            # 共享表单的默认背景仍用于独立属性弹窗，不随侧栏变成浅灰。
            assert pixel(form, QPoint(1, 1)) == QColor(dialog)
            control = form.fields["style.font_family"]
            assert pixel(control, QPoint(control.width() // 2, 2)) == QColor(field)
    finally:
        workspace.dispose()


def test_component_theme_does_not_change_host_or_global_style(qtbot):
    """宿主已有样式及同级输入框不应受到组件局部配色的影响。"""
    app = QApplication.instance()
    app_style, app_palette = app.styleSheet(), app.palette()
    host = QWidget()
    host.setStyleSheet("QLineEdit { background: #ffddaa; color: #123456; }")
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    sibling = QLineEdit("宿主输入框")
    layout.addWidget(sibling)
    workspace = MindMapWorkspace(parent=host)
    layout.addWidget(workspace)
    host.resize(1000, 800)
    host.show()
    QApplication.processEvents()
    original = sibling.palette()
    try:
        workspace.canvas.engine.set_theme(Theme.dark())
        workspace.canvas.engine.set_theme(Theme())
        QApplication.processEvents()
        assert sibling.palette() == original
        assert app.styleSheet() == app_style and app.palette() == app_palette
        assert host.styleSheet() == "QLineEdit { background: #ffddaa; color: #123456; }"
    finally:
        workspace.dispose()


@pytest.mark.parametrize("toolbar", [True, False])
@pytest.mark.parametrize("bottom_open", [True, False])
def test_drag_preview_matches_cards_when_embedded_and_toolbar_hidden(qtbot, toolbar, bottom_open):
    """预览对齐卡片而不是视口内边距；嵌入位置和工具栏隐藏不应造成整体偏移。"""
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(23, 17, 19, 11)
    workspace = MindMapWorkspace(parent=host, toolbar=toolbar)
    layout.addWidget(workspace)
    host.resize(1100, 800)
    host.show()
    QApplication.processEvents()
    if bottom_open:
        workspace._move_tool("outline", "bottom")
        QApplication.processEvents()
    before = workspace.save_state()
    document = workspace.canvas.engine.document
    try:
        for area in ("left", "right", "bottom"):
            workspace._begin_drag("properties")
            bar = workspace.rails[area]
            workspace._update_drag(bar.mapToGlobal(bar.rect().center()))
            assert workspace._drop_area == area
            rect = workspace._preview_rect(area)
            card_top = workspace.canvas._view_card.mapTo(workspace, QPoint()).y()
            assert workspace.rect().contains(rect)
            assert rect.top() >= card_top
            assert rect.bottom() < workspace.rails["bottom"].y()
            if area != "bottom":
                assert rect.top() == card_top
                if bottom_open:
                    assert workspace.tools["outline"].dock.y() - rect.bottom() - 1 == 6
            workspace._cancel_drag()
        assert workspace.save_state() == before
        assert workspace.canvas.engine.document == document
        assert not workspace.canvas.engine.can_undo
    finally:
        workspace.dispose()


def test_style_refresh_preserves_pending_input_focus_cursor_and_scroll(qtbot):
    engine = MindMapEngine()
    workspace = MindMapWorkspace(engine)
    qtbot.addWidget(workspace)
    workspace.resize(1000, 620)
    workspace.show()
    QApplication.processEvents()
    panel = workspace.properties
    line = panel.fields["style.font_family"].lineEdit()
    try:
        line.setFocus()
        line.selectAll()
        qtbot.keyClicks(line, "Microsoft YaHei UI")
        line.setCursorPosition(4)
        panel.scroll.verticalScrollBar().setValue(30)
        before, revision = engine.document, engine.revision
        scroll = panel.scroll.verticalScrollBar().value()
        assert scroll > 0
        # 清除表单缓存以真正执行背景和控件样式更新，不能只命中 set_theme 的早退分支。
        panel._theme_signature = None
        workspace.settings._theme_signature = None
        workspace._theme_signature = None
        workspace._apply_theme()
        assert line.hasFocus() and line.cursorPosition() == 4
        assert panel.scroll.verticalScrollBar().value() == scroll
        assert engine.document == before and engine.revision == revision
        assert panel.timer.isActive()
        workspace.flush_pending_edits()
        assert engine.document.root.style["font_family"] == "Microsoft YaHei UI"
        assert engine.undo() and engine.document == before
    finally:
        workspace.dispose()


def test_standalone_canvas_keeps_same_public_interface_and_card(qtbot):
    widget = MindMapWidget()
    qtbot.addWidget(widget)
    widget.resize(600, 420)
    widget.show()
    try:
        widget.engine.set_theme(Theme.dark())
        QApplication.processEvents()
        assert widget.view.owner is widget
        assert widget._view_card.layout().itemAt(0).widget() is widget.view
        assert widget.view.mapTo(widget._view_card, QPoint()) == QPoint(8, 8)
        assert widget.action("properties").isEnabled()
        assert widget.action("branch_style") not in widget.toolbar.actions()
        assert not widget.windowFlags() & Qt.WindowType.FramelessWindowHint
    finally:
        widget.dispose()
