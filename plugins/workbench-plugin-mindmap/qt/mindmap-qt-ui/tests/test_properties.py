"""按真实能力归属迁移的组件与行为回归。"""


import pytest
from mindmap_core import MindMapDocument
from PySide6.QtCore import QPoint
from PySide6.QtGui import QInputMethodEvent, QPalette
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import (
    QApplication,
)


@pytest.mark.parametrize("width", [240, 260, 300, 360])
def test_single_page_wraps_without_clipping_or_horizontal_scroll(qtbot, width):
    from mindmap_qt_ui.properties import NodePropertiesForm

    document = MindMapDocument.new("窄面板")
    form = NodePropertiesForm()
    qtbot.addWidget(form)
    form.load_nodes([document.root], root_id=document.root_id)
    form.resize(width, 460)
    form.show()
    QApplication.processEvents()
    assert form.width() == width
    assert form.page.width() <= form.scroll.viewport().width()
    assert form.scroll.horizontalScrollBar().maximum() == 0
    for field in (*form.fields.values(), form.increase_size, form.decrease_size, form.width_enabled):
        point = field.mapTo(form.page, QPoint())
        assert point.x() >= 0 and point.x() + field.width() <= form.page.width()
    form.scroll.ensureWidgetVisible(form.scope)
    QApplication.processEvents()
    assert form.scroll.verticalScrollBar().value() > 0


def test_public_form_controls_preserve_drafts_composition_and_theme(qtbot):
    """同步色板与分支范围不产生编辑，关闭弹层也不能强制确认输入法候选。"""
    from mindmap_core import Theme
    from mindmap_qt_ui import NodePropertiesForm
    from mindmap_qt_ui.ui_style import ui_colors

    document = MindMapDocument.new()
    form = NodePropertiesForm()
    qtbot.addWidget(form)
    form.load_nodes([document.root], root_id=document.root_id)
    edited, scope_changed = QSignalSpy(form.edited), QSignalSpy(form.scope_changed)
    line = form.fields["style.font_family"].lineEdit()
    line.setText("尚未提交的字体草稿")
    QApplication.sendEvent(line, QInputMethodEvent("候选", []))
    assert form.composing
    colors = ui_colors(Theme.dark())
    form.set_ui_colors(colors)
    form.set_branch_scope("level")
    form.close_popups()
    form.close_popups()
    assert form.composing and line.text() == "尚未提交的字体草稿"
    assert form.scope.currentData() == "level"
    assert edited.count() == scope_changed.count() == 0
    assert form.palette().color(QPalette.ColorRole.Window).name() == colors.surface
    form.set_theme(Theme.dark())
    form.set_ui_colors(None)
    assert form.palette().color(QPalette.ColorRole.Window).name() == colors.surface
    with pytest.raises(ValueError):
        form.set_branch_scope("invalid")
    with pytest.raises(TypeError):
        form.set_ui_colors("dark")
    assert form.scope.currentData() == "level"
