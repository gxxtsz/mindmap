"""只使用公开协议连接普通 QWidget，验证面板不要求真实画布或场景。"""


from mindmap_qt_panels import MindMapPanels


def test_panels_edit_and_navigate_without_canvas(qtbot, panel_host_factory):
    """属性、全局设置、大纲导航及撤销均可在没有场景图元的宿主上工作。"""
    host = panel_host_factory()
    qtbot.addWidget(host)
    panels = MindMapPanels(host)
    try:
        panels.properties.fields["style.font_size"].setValue(24)
        panels.flush_pending_edits()
        assert host.engine.get_node(host.engine.root_id).style["font_size"] == 24
        assert host.engine.undo()
        assert "font_size" not in host.engine.get_node(host.engine.root_id).style
        index = panels.settings.direction.findData("down")
        panels.settings.direction.setCurrentIndex(index)
        panels.settings.direction.activated.emit(index)
        assert host.document_view.layout.direction == "down"
        child = host.engine.add_child(host.engine.root_id, "独立面板导航")
        panels.outline.nodes_selected.emit([child])
        assert host.revealed == [child] and panels.properties._ids == [child]
        panels.dispose()
        panels.dispose()
    finally:
        panels.dispose()
        host.unsubscribe()
