"""从真实视口像素和重绘区域验证间隔外框，不依赖实现中的路径构造公式。"""

import pytest
from mindmap_core import MindMapEngine, Theme
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_ui.ui_style import ui_colors
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QRegion
from PySide6.QtWidgets import QApplication


@pytest.fixture
def canvas(qtbot):
    """生成独立根节点及一个子主题，不读取用户文件或本机偏好。"""
    engine = MindMapEngine()
    engine.update_node(engine.root_id, text="第二大脑", style={"border_width": 2})
    child = engine.add_child(engine.root_id, "任务计划")
    engine.update_node(child, style={"border_width": 2})
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine, toolbar=False, embedded=True)
    qtbot.addWidget(widget)
    widget.resize(900, 600)
    widget.show()
    QApplication.processEvents()
    yield widget, child
    widget.finish_edit(commit=False)
    widget.dispose()


def render_view(widget, dpr=1):
    """通过真实 QWidget 绘制入口取得视口像素；高 DPI 不放大导图的逻辑尺寸。"""
    viewport = widget.view.viewport()
    image = QImage(viewport.width() * dpr, viewport.height() * dpr, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor("#ff00ff"))
    viewport.render(image)
    return image


def top_pixel(widget, key, image, distance):
    """采样节点顶部中点外侧的真实像素，distance 使用屏幕逻辑像素。"""
    item = widget.node_items[key]
    point = item.deviceTransform(widget.view.viewportTransform()).map(QPointF(item.visual.width / 2, 0))
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(point.x() * ratio), int((point.y() - distance) * ratio))


@pytest.mark.parametrize("readonly", [False, True])
def test_ctrl_a_selects_all_nodes_and_paints_each_outline(canvas, qtbot, readonly):
    """真实 Ctrl+A 同时改变选择和全部外框，只读模式可用且只发送一次选择通知。"""
    widget, child = canvas
    root = widget.engine.root_id
    widget.set_editable(not readonly)
    widget.select_nodes([root])
    widget.activateWindow()
    widget.view.setFocus()
    QApplication.processEvents()
    document, revision = widget.engine.document, widget.engine.revision
    layout = widget.layout_result
    notifications = []
    widget.selection_changed.connect(notifications.append)
    qtbot.keyClick(widget.view.viewport(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert widget.selected_node_ids() == [root, child]
    assert notifications == [[root, child]]
    selected = render_view(widget)
    for key in (root, child):
        assert top_pixel(widget, key, selected, 4) == QColor(widget.document_view.theme.accent)
    assert widget.layout_result is layout
    assert widget.engine.document == document and widget.engine.revision == revision


def test_ctrl_a_in_editor_selects_only_text(canvas, qtbot):
    """原地编辑期间全选交给 Qt 文本控件，不扩大节点选择或提交草稿。"""
    widget, child = canvas
    widget.activateWindow()
    widget.begin_edit(child)
    editor = widget.editor
    cursor = editor.textCursor()
    cursor.clearSelection()
    editor.setTextCursor(cursor)
    QApplication.processEvents()
    before, revision = widget.engine.document, widget.engine.revision
    qtbot.keyClick(widget.view.viewport(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert widget.editor is editor
    assert editor.textCursor().selectedText() == editor.toPlainText()
    assert widget.selected_node_ids() == [child]
    assert widget.engine.document == before and widget.engine.revision == revision


def test_select_all_respects_page_boundary_and_collapsed_descendants(canvas):
    """进入子导图后仅选择该页展开节点，返回主图和进入空灵感页时使用各自范围。"""
    widget, child = canvas
    main_root = widget.engine.root_id
    widget.select_nodes([child])
    widget.create_submap()
    root = widget.document_view.root_id
    branch = widget.engine.add_child(root, "折叠分支")
    widget.engine.add_child(branch, "隐藏后代")
    widget.engine.set_expanded(branch, False)
    before, revision = widget.engine.document, widget.engine.revision
    widget.select_all_nodes()
    assert widget.selected_node_ids() == [root, branch]
    assert widget.engine.document == before and widget.engine.revision == revision
    widget.navigate_to(None)
    widget.select_all_nodes()
    assert widget.selected_node_ids() == [main_root, child]
    widget.show_inspiration()
    assert not widget.action("select_all").isEnabled()
    widget.select_all_nodes()
    assert widget.selected_node_ids() == []


def test_select_all_includes_nodes_not_yet_instantiated_outside_viewport(canvas, monkeypatch):
    """虚拟画布全选包含屏外展开主题，而非仅遍历已创建的屏内图元。"""
    widget, _ = canvas
    monkeypatch.setattr("mindmap_qt_canvas.widget.VIRTUALIZATION_NODE_LIMIT", 3)
    with widget.engine.transaction("构造跨屏主题"):
        for index in range(45):
            widget.engine.add_child(widget.engine.root_id, f"跨屏主题 {index}")
    widget.select_nodes([widget.engine.root_id])
    widget.view.centerOn(widget.node_items[widget.engine.root_id])
    QApplication.processEvents()
    assert widget._virtualized and len(widget.node_items) < len(widget.layout_result.nodes)
    before, revision = widget.engine.document, widget.engine.revision
    widget.action("select_all").trigger()
    assert set(widget.selected_node_ids()) == set(widget.document_view.nodes)
    assert widget.engine.document == before and widget.engine.revision == revision


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("zoom", [0.1, 0.5, 1, 1.25, 4])
@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("target", ["root", "child"])
def test_selection_keeps_gap_and_width_across_zoom_dpi_and_mixed_themes(canvas, dark, zoom, dpr, target):
    """根节点与普通主题均显示独立外框，明暗混用时留白取画布而非文档背景。"""
    widget, child = canvas
    widget.engine.set_theme(Theme.dark() if dark else Theme())
    widget.set_ui_colors(ui_colors(Theme(background="#ffffff" if dark else "#202124")))
    key = widget.engine.root_id if target == "root" else child
    item = widget.node_items[key]
    widget.view.resetTransform()
    widget.view.scale(zoom, zoom)
    widget.view.centerOn(item)
    widget.select_nodes([])
    QApplication.processEvents()
    plain = render_view(widget, dpr)
    geometry = item.boundingRect(), item.shape(), widget.layout_result
    document, revision = widget.engine.document, widget.engine.revision
    widget.select_nodes([key])
    selected = render_view(widget, dpr)
    background = widget.scene.backgroundBrush().color()
    assert top_pixel(widget, key, selected, 1.5) == background
    assert top_pixel(widget, key, selected, 4) == QColor(item.theme.accent)
    assert top_pixel(widget, key, plain, 4) == background
    assert (item.boundingRect(), item.shape(), widget.layout_result) == geometry
    assert widget.engine.document == document and widget.engine.revision == revision
    widget.select_nodes([])
    assert render_view(widget, dpr) == plain


def test_editing_keeps_outline_without_selection_and_tracks_draft_size(canvas):
    """正文抢占选择时编辑外框仍存在，草稿伸缩后边框跟随，取消后清除。"""
    widget, _ = canvas
    root = widget.engine.root_id
    before, revision = widget.engine.document, widget.engine.revision
    widget.begin_edit(root)
    widget.scene.clearSelection()
    assert widget.editor is not None and not widget.node_items[root].isSelected()
    for text in ("输入后持续换行的正文" * 12, "短"):
        widget.editor.setPlainText(text)
        image = render_view(widget)
        assert top_pixel(widget, root, image, 1.5) == widget.scene.backgroundBrush().color()
        assert top_pixel(widget, root, image, 4) == QColor(widget.document_view.theme.accent)
    widget.finish_edit(commit=False)
    assert top_pixel(widget, root, render_view(widget), 4) == widget.scene.backgroundBrush().color()
    assert widget.engine.document == before and widget.engine.revision == revision


@pytest.mark.parametrize("width", [0, 1, 6])
@pytest.mark.parametrize("zoom", [0.5, 1, 4])
def test_custom_border_keeps_its_pixels_and_gap_starts_outside_visible_edge(canvas, width, zoom):
    """自定义粗边框仍完整可见，实际描边外缘到选中环之间保持三个屏幕像素留白。"""
    widget, _ = canvas
    root = widget.engine.root_id
    widget.engine.update_node(root, style={"border_width": width, "border_color": "#803a4f"})
    widget.view.resetTransform()
    widget.view.scale(zoom, zoom)
    widget.view.centerOn(widget.node_items[root])
    widget.select_nodes([])
    plain = render_view(widget)
    widget.select_nodes([root])
    selected = render_view(widget)
    item = widget.node_items[root]
    top = item.deviceTransform(widget.view.viewportTransform()).map(QPointF(item.visual.width / 2, 0))
    x = int(top.x())
    background = widget.scene.backgroundBrush().color()
    # 从未选中的实际图像寻找正文外缘，不通过被测路径函数反算预期值。
    body_rows = [y for y in range(int(top.y()) - 20, int(top.y()) + 20)
                 if plain.pixelColor(x, y) != background]
    edge = min(body_rows)
    for y in range(edge + 1, int(top.y()) + 12):
        assert selected.pixelColor(x, y) == plain.pixelColor(x, y)
    assert selected.pixelColor(x, edge - 2) == background
    assert selected.pixelColor(x, edge - 4) == QColor(item.theme.accent)


class PaintRegions(QObject):
    """记录视口真实接收到的重绘区域，验证旧框外缘被纳入刷新。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.region = QRegion()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint:
            self.region |= event.region()
        return False


def test_moving_after_view_zoom_repaints_old_and_new_external_outline(canvas, qtbot):
    """只移动图元也会清除正文边界之外的旧框，包括前一次仅改变视图缩放的情况。"""
    widget, child = canvas
    item = widget.node_items[child]
    widget.select_nodes([child])
    QApplication.processEvents()
    widget.view.scale(0.5, 0.5)
    widget.view.centerOn(item)
    QApplication.processEvents()
    observer = PaintRegions(widget.view.viewport())
    widget.view.viewport().installEventFilter(observer)
    old = item.deviceTransform(widget.view.viewportTransform()).map(QPointF(item.visual.width / 2, 0))
    item.setPos(item.pos() + QPointF(160, 80))
    new = item.deviceTransform(widget.view.viewportTransform()).map(QPointF(item.visual.width / 2, 0))
    qtbot.waitUntil(lambda: observer.region.contains(QPoint(int(old.x()), int(old.y() - 4)))
                   and observer.region.contains(QPoint(int(new.x()), int(new.y() - 4))))


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
def test_selected_nodes_export_without_view_decoration(canvas, tmp_path, monkeypatch, suffix):
    """多选时的正式导出不调用交互外框，也不清除屏幕选择或改写节点样式。"""
    widget, child = canvas
    widget.select_nodes([widget.engine.root_id, child])
    document, revision = widget.engine.document, widget.engine.revision
    selected = widget.selected_node_ids()

    def forbidden(*args):
        raise AssertionError("导出不得绘制交互选中外框")

    with monkeypatch.context() as patch:
        patch.setattr("mindmap_qt_canvas.view.paint_node_selection", forbidden)
        path = tmp_path / f"选中节点.{suffix}"
        widget.export(path)
    assert path.stat().st_size > 100
    assert widget.selected_node_ids() == selected
    assert widget.engine.document == document and widget.engine.revision == revision
