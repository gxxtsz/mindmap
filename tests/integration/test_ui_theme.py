"""宿主公开色板切换保持输入、文档主题及界面隔离。"""

from mindmap_core import Theme
from mindmap_qt_ui import UiColors
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtWidgets import QApplication, QWidget


def test_palette_change_preserves_pending_input_and_history(workspace, qtbot):
    panel, engine = workspace.properties, workspace.canvas.engine
    line = panel.fields["style.font_family"].lineEdit()
    line.setFocus()
    line.selectAll()
    qtbot.keyClicks(line, "Microsoft YaHei UI")
    line.setCursorPosition(4)
    panel.scroll.verticalScrollBar().setValue(30)
    scroll = panel.scroll.verticalScrollBar().value()
    before, revision = engine.document, engine.revision
    for theme in (Theme.dark(), Theme()):
        colors = ui_colors(theme)
        assert isinstance(colors, UiColors)
        workspace.canvas.set_ui_colors(colors)
        workspace.panels.set_ui_colors(colors)
        assert line.hasFocus() and line.cursorPosition() == 4
        assert panel.scroll.verticalScrollBar().value() == scroll
        assert engine.document == before and engine.revision == revision
        assert panel.timer.isActive()
    workspace.flush_pending_edits()
    assert engine.document.root.style["font_family"] == "Microsoft YaHei UI"
    assert engine.undo() and engine.document == before


def test_components_keep_application_and_other_host_styles(workspace, qapp):
    outside = QWidget()
    before = qapp.styleSheet(), qapp.palette(), qapp.font(), outside.palette(), outside.styleSheet()
    document = workspace.canvas.engine.document
    for theme in (Theme.dark(), Theme()):
        colors = ui_colors(theme)
        workspace.canvas.set_ui_colors(colors)
        workspace.panels.set_ui_colors(colors)
        QApplication.processEvents()
        assert (qapp.styleSheet(), qapp.palette(), qapp.font(), outside.palette(), outside.styleSheet()) == before
        assert workspace.canvas.engine.document == document
    outside.close()
