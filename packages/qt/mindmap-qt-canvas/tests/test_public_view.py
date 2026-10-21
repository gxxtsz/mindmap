"""公开借用视图、导航及独立画布承载层的回归。"""

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_canvas import MindMapWidget
from PySide6.QtCore import QPoint, Qt


def test_public_read_view_and_navigation_preserve_history(qtbot):
    engine = MindMapEngine()
    parent = engine.add_child(engine.root_id, "折叠父主题")
    child = engine.add_child(parent, "导航目标")
    engine.set_expanded(parent, False)
    canvas = MindMapWidget(engine)
    qtbot.addWidget(canvas)
    try:
        assert canvas.document_view is canvas.document_view
        assert canvas.parent_index[child] == parent
        with pytest.raises(TypeError):
            canvas.parent_index[child] = engine.root_id
        before = engine.document
        canvas.reveal_nodes([child])
        assert canvas.selected_node_ids() == [child] and engine.get_node(parent).expanded
        assert engine.undo() and engine.document == before
        canvas.set_branch_scope("level")
        assert canvas.branch_scope == "level"
        with pytest.raises(ValueError):
            canvas.set_branch_scope("invalid")
    finally:
        canvas.dispose()


def test_standalone_canvas_keeps_same_public_interface_and_card(qtbot):
    canvas = MindMapWidget()
    qtbot.addWidget(canvas)
    canvas.resize(600, 420)
    canvas.show()
    try:
        canvas.engine.set_theme(Theme.dark())
        assert canvas.view.owner is canvas
        surface = canvas.canvas_surface
        assert surface.layout().itemAt(0).widget() is canvas.view
        assert canvas.view.mapTo(surface, QPoint()) == QPoint(8, 8)
        assert canvas.action("properties").isEnabled()
        assert canvas.action("branch_style") not in canvas.toolbar.actions()
        assert not canvas.windowFlags() & Qt.WindowType.FramelessWindowHint
    finally:
        canvas.dispose()
