"""按真实能力归属迁移的组件与行为回归。"""


import pytest
from mindmap_core import MindMapDocument
from PySide6.QtCore import QPoint
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
