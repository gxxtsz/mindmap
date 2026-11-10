"""只使用公开协议连接普通 QWidget，验证面板不要求真实画布或场景。"""


from mindmap_qt_panels import MindMapPanels
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication


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


def test_public_panel_lifecycle_reports_composition_without_changing_document(panel_host_factory):
    """普通宿主可查询输入法状态、关闭弹层并切换色板，无需访问画布实现。"""
    from mindmap_core import Theme
    from mindmap_qt_ui.ui_style import ui_colors

    host = panel_host_factory()
    panels = MindMapPanels(host)
    before, revision = host.engine.document, host.engine.revision
    try:
        line = panels.properties.fields["style.font_family"].lineEdit()
        assert not panels.is_composing
        QApplication.sendEvent(line, QInputMethodEvent("候选", []))
        assert panels.is_composing
        panels.set_ui_colors(ui_colors(Theme.dark()))
        panels.close_popups()
        assert panels.is_composing
        panels.set_ui_colors(None)
        QApplication.sendEvent(line, QInputMethodEvent())
        assert not panels.is_composing
        assert host.engine.document == before and host.engine.revision == revision
    finally:
        panels.dispose()
    assert not panels.is_composing
    panels.close_popups()


def test_initial_host_palette_applies_before_first_sync_and_preserves_document(panel_host_factory):
    """预设深色外壳时，首次同步直接使用宿主色板；恢复后仍能跟随原文档主题。"""
    from mindmap_core import Theme
    from mindmap_qt_ui.ui_style import ui_colors

    host = panel_host_factory()
    before, revision = host.engine.document, host.engine.revision
    colors = ui_colors(Theme.dark())
    panels = MindMapPanels(host, ui_colors=colors)
    try:
        for panel in (panels.outline, panels.properties, panels.settings):
            assert panel._ui_colors_override == colors
            assert colors.text in panel.styleSheet()
        panels.properties.fields["style.font_size"].setValue(24)
        assert panels.properties.timer.isActive()
        panels.set_ui_colors(None)
        assert panels.properties.timer.isActive()
        assert host.engine.document == before and host.engine.revision == revision
        panels.flush_pending_edits()
        assert host.engine.get_node(host.engine.root_id).style["font_size"] == 24
        assert host.engine.document.theme == before.theme
    finally:
        panels.dispose()


def test_dispose_discards_pending_form_draft_before_closing_popups(panel_host_factory):
    """销毁组件只取消未提交输入，不能因弹层失焦追加一条编辑或历史记录。"""
    host = panel_host_factory()
    panels = MindMapPanels(host)
    font = panels.properties.fields["style.font_family"]
    line = font.lineEdit()
    line.setText("尚未提交的字体")
    line.textEdited.emit(line.text())
    before, revision = host.engine.document, host.engine.revision
    assert panels.properties.timer.isActive()
    panels.dispose()
    QApplication.processEvents()
    assert not panels.properties.timer.isActive()
    assert host.engine.document == before and host.engine.revision == revision
