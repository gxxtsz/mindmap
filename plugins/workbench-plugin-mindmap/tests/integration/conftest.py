"""导图画布与面板的真实组合夹具；仅由导图集成用例共享。"""

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def composition_factory(qtbot):
    """只组合正式画布和面板，不实现已删除工作区的停靠或浮动功能。"""
    from mindmap_core import MindMapDocument, MindMapEngine
    from mindmap_qt_editor.canvas import MindMapWidget
    from mindmap_qt_editor.panels import MindMapPanels
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QHBoxLayout, QWidget

    class Composition(QWidget):
        def __init__(self, engine=None):
            super().__init__()
            self.canvas = MindMapWidget(engine, self)
            self.canvas.setFocusProxy(self.canvas.view)
            self.panels = MindMapPanels(self.canvas, self)
            self.outline, self.properties, self.settings = self.panels.outline, self.panels.properties, self.panels.settings
            self.settings_menu = self.settings.create_menu(self)
            layout = QHBoxLayout(self)
            layout.addWidget(self.outline)
            layout.addWidget(self.canvas, 1)
            layout.addWidget(self.properties)
            self.outline.setFixedWidth(260)
            self.properties.setFixedWidth(360)
            self.settings.hide()
            self.outline.hide()
            self.canvas.set_properties_handler(lambda: self.show_tool_window("properties"))
            self._disposed = False

        def show_tool_window(self, name, *, focus=True):
            if focus:
                self.canvas.finish_edit(commit=True)
            panel = {"outline": self.outline, "properties": self.properties}[name]
            panel.show()
            if focus:
                panel.setFocus(Qt.FocusReason.OtherFocusReason)

        def hide_tool_window(self, name):
            self.panels.flush_pending_edits()
            {"outline": self.outline, "properties": self.properties}[name].hide()

        def show_settings_menu(self):
            self.settings_menu.popup(self.mapToGlobal(QPoint(self.width() - 240, 30)))

        def flush_pending_edits(self):
            self.panels.flush_pending_edits()

        def hideEvent(self, event):
            self.settings.close_popups()
            super().hideEvent(event)

        def dispose(self):
            if not self._disposed:
                self._disposed = True
                self.panels.dispose()
                self.canvas.dispose()

    widgets = []

    def create(engine=None):
        widget = Composition(engine or MindMapEngine(MindMapDocument.new("根主题")))
        qtbot.addWidget(widget)
        widget.resize(1280, 820)
        widget.show()
        QApplication.processEvents()
        widgets.append(widget)
        return widget

    yield create
    for widget in reversed(widgets):
        widget.dispose()


@pytest.fixture
def workspace(composition_factory):
    """三节点测试数据；名称沿用测试历史，不对应任何发行组件。"""
    from mindmap_core import MindMapDocument, MindMapEngine

    engine = MindMapEngine(MindMapDocument.new("根主题"))
    a = engine.add_child(engine.root_id, "甲")
    b = engine.add_child(engine.root_id, "乙")
    leaf = engine.add_child(a, "叶主题")
    engine.replace_document(engine.document)
    widget = composition_factory(engine)
    widget.ids = a, b, leaf
    return widget
