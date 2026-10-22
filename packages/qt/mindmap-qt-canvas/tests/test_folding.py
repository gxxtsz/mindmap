"""外侧折叠按钮的显示、真实事件路由、直接子节点计数与静态导出回归。"""

import gc
from weakref import ref

import pytest
from mindmap_core import LayoutConfig, MindMapDocument, MindMapEngine, Theme
from mindmap_qt_canvas import MindMapWidget
from mindmap_qt_canvas.items import FoldControlItem
from mindmap_qt_render import NodeVisual
from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


@pytest.fixture
def canvas(qtbot):
    """包含三名直接子节点及已收起的孙分支，初始历史清空以核对单次撤销边界。"""
    engine = MindMapEngine()
    a = engine.add_child(engine.root_id, "可收起的主题")
    children = [engine.add_child(a, f"子主题 {index}") for index in range(3)]
    engine.add_child(children[0], "孙主题一")
    engine.add_child(children[0], "孙主题二")
    engine.set_expanded(children[0], False)
    b = engine.add_child(engine.root_id, "另一个主题")
    engine.add_child(b, "另一个子主题")
    engine.set_expanded(b, False)
    engine.set_layout(LayoutConfig(direction="right"))
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine, toolbar=False)
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    qtbot.addWidget(widget)
    widget.resize(1100, 750)
    widget.show()
    QApplication.processEvents()
    widget.view.resetTransform()
    widget.center_root()
    widget.select_nodes([b])
    yield widget, a, b, children
    widget.finish_edit(commit=False)
    widget.dispose()


def mouse(view, kind, position, *, pressed=False):
    """经真实视口事件传递按键状态，不直接调用折叠逻辑，也不移动系统鼠标。"""
    button = Qt.MouseButton.LeftButton if kind in (
        QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick
    ) else Qt.MouseButton.NoButton
    event = QMouseEvent(
        kind, QPointF(position), QPointF(view.viewport().mapToGlobal(position)), button,
        Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)


def body_position(widget, key):
    """正文中心不包括外侧按钮，覆盖按钮与正文分离后的命中约定。"""
    item = widget.node_items[key]
    return widget.view.mapFromScene(item.mapToScene(QPointF(item.visual.width / 2, item.visual.height / 2)))


def click_control(widget, key, *, offset=QPoint()):
    """先经正文唤出按钮，再完整发送按下、可选移动及释放，返回实际命中的控件。"""
    view = widget.view
    mouse(view, QEvent.Type.MouseMove, body_position(widget, key))
    control = widget.node_items[key].fold_control
    position = view.mapFromScene(control.mapToScene(control.button_rect().center()))
    mouse(view, QEvent.Type.MouseMove, position)
    assert view.itemAt(position) is control
    mouse(view, QEvent.Type.MouseButtonPress, position, pressed=True)
    if not offset.isNull():
        mouse(view, QEvent.Type.MouseMove, position + offset, pressed=True)
    mouse(view, QEvent.Type.MouseButtonRelease, position + offset)
    return control


class LabelPainter(QPainter):
    """记录实际绘制的文字，同时完成真实离屏绘图，核对计数而非仅检查提示文字。"""

    def __init__(self, image):
        super().__init__(image)
        self.labels = []

    def drawText(self, *args):
        self.labels.append(args[-1])
        return super().drawText(*args)


def painted_labels(control):
    """将按钮局部区域画到图片，返回真正传给画笔的完整数字。"""
    area = control.boundingRect()
    image = QImage(int(area.width()) + 4, int(area.height()) + 4, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = LabelPainter(image)
    painter.translate(2 - area.left(), 2 - area.top())
    control.paint(painter, None)
    painter.end()
    return painter.labels


def test_hover_selection_and_bridge_only_repaint_related_controls(canvas, monkeypatch, qtbot):
    widget, a, b, children = canvas
    view, control = widget.view, widget.node_items[a].fold_control
    assert not control.isVisible()
    assert widget.node_items[b].fold_control.isVisible()
    assert not widget.node_items[children[1]].fold_control.isVisible()
    original = widget.engine.document

    def forbidden(*args, **kwargs):
        raise AssertionError("悬停和选择不能重新布局、测量或刷新整张导图")

    monkeypatch.setattr(widget, "refresh", forbidden)
    monkeypatch.setattr(widget.factory, "measure", forbidden)
    monkeypatch.setattr(widget.layout_registry, "layout", forbidden)
    mouse(view, QEvent.Type.MouseMove, body_position(widget, a))
    assert control.isVisible()
    rect = control.button_rect()
    start = QPointF(widget.node_items[a].visual.width - 1, 8)
    for fraction in (0, 0.15, 0.3, 0.45, 0.6, 0.8, 1):
        local = start + (rect.center() - start) * fraction
        mouse(view, QEvent.Type.MouseMove, view.mapFromScene(control.mapToScene(local)))
        assert control.isVisible(), "从正文边缘斜向移向圆圈时不能隐藏按钮"
    for local in (QPointF(rect.left() - 2, rect.center().y()), rect.center()):
        mouse(view, QEvent.Type.MouseMove, view.mapFromScene(control.mapToScene(local)))
        assert control.isVisible() and widget.node_items[a].hovered
    assert view.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
    mouse(view, QEvent.Type.MouseMove, QPoint(10, 10))
    assert control.isVisible()
    qtbot.waitUntil(lambda: not control.isVisible(), timeout=700)
    widget.select_nodes([a])
    assert control.isVisible()
    widget.select_nodes([b])
    assert not control.isVisible()
    assert widget.engine.document == original and not widget.engine.can_undo


def fork_position(widget, key, *, offset_pixels=0):
    """从正式布局中的节点出口取圆角分叉位置，偏移使用固定视口像素。"""
    item = widget.node_items[key]
    gap = 32 if widget.document_view.layout.compact else 64
    reach = gap * 0.35
    offset = offset_pixels / widget.view.transform().m11()
    direction = item.fold_control.direction
    if direction == "left":
        local = QPointF(-reach, item.visual.height / 2 + offset)
    elif direction == "down":
        local = QPointF(item.visual.width / 2 + offset, item.visual.height + reach)
    else:
        local = QPointF(item.visual.width + reach, item.visual.height / 2 + offset)
    return widget.view.mapFromScene(item.mapToScene(local))


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("scale", [0.25, 1.0, 2.0])
def test_hidden_control_reveals_directly_near_fork_at_constant_pixel_distance(
    canvas, direction, compact, scale, monkeypatch,
):
    """从空白直接进入线旁八像素处，无需先经过正文；四方向及缩放保持同等感应宽度。"""
    widget, a, b, _ = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction, compact=compact))
    widget.view.resetTransform()
    widget.view.scale(scale, scale)
    widget.view.centerOn(widget.node_items[a].sceneBoundingRect().center())
    mouse(widget.view, QEvent.Type.MouseMove, QPoint(10, 10))
    control = widget.node_items[a].fold_control
    assert not control.isVisible()
    before = widget.engine.document
    had_undo = widget.engine.can_undo

    def forbidden(*args, **kwargs):
        raise AssertionError("连线悬停只查询现有图元，不应重新布局、测量或刷新")

    monkeypatch.setattr(widget, "refresh", forbidden)
    monkeypatch.setattr(widget.factory, "measure", forbidden)
    monkeypatch.setattr(widget.layout_registry, "layout", forbidden)
    mouse(widget.view, QEvent.Type.MouseMove, fork_position(widget, a, offset_pixels=8))
    assert control.isVisible() and widget.node_items[a].hovered
    assert widget.selected_node_ids() == [b] and widget.engine.document == before
    assert widget.engine.can_undo == had_undo


def test_hover_hide_delay_reentry_and_selected_node_priority(canvas, qtbot):
    """离开等待 200 毫秒；重入取消旧倒计时，选中状态不会被旧计时器误隐藏。"""
    widget, a, b, _ = canvas
    view, item = widget.view, widget.node_items[a]
    line = fork_position(widget, a)
    mouse(view, QEvent.Type.MouseMove, line)
    mouse(view, QEvent.Type.MouseMove, QPoint(10, 10))
    assert item.fold_control.isVisible() and view._fold_hide_timer.isActive()
    qtbot.wait(120)
    assert item.fold_control.isVisible()
    mouse(view, QEvent.Type.MouseMove, line)
    assert not item._fold_hide_pending and not view._fold_hide_timer.isActive()
    qtbot.wait(120)
    assert item.fold_control.isVisible()
    mouse(view, QEvent.Type.MouseMove, QPoint(10, 10))
    qtbot.wait(110)
    assert item.fold_control.isVisible()
    widget.select_nodes([a])
    qtbot.wait(130)
    assert item.fold_control.isVisible()
    widget.select_nodes([b])
    assert not item.fold_control.isVisible()
    mouse(view, QEvent.Type.MouseMove, line)
    mouse(view, QEvent.Type.MouseMove, QPoint(10, 10))
    qtbot.waitUntil(lambda: not item.fold_control.isVisible(), timeout=700)


def test_hovering_long_trunk_away_from_fork_does_not_reveal_control(canvas):
    widget, a, _, _ = canvas
    control = widget.node_items[a].fold_control
    mouse(widget.view, QEvent.Type.MouseMove, fork_position(widget, a, offset_pixels=42))
    assert not control.isVisible()


def test_both_direction_root_reveals_from_its_left_fork(canvas):
    """双向根按钮仍在右侧，但左右两侧的分叉附近都能将其唤出。"""
    widget, _, b, _ = canvas
    widget.engine.set_layout(LayoutConfig(direction="both"))
    widget.select_nodes([b])
    item = widget.node_items[widget.engine.root_id]
    assert item.fold_control.direction == "right" and not item.fold_control.isVisible()
    position = widget.view.mapFromScene(item.mapToScene(QPointF(-22.4, item.visual.height / 2)))
    mouse(widget.view, QEvent.Type.MouseMove, position)
    assert item.fold_control.isVisible() and item.hovered


def test_fork_hover_does_not_fold_on_line_click(canvas):
    widget, a, _, _ = canvas
    before = widget.engine.document
    position = fork_position(widget, a, offset_pixels=8)
    mouse(widget.view, QEvent.Type.MouseMove, position)
    control = widget.node_items[a].fold_control
    assert control.isVisible() and not control.contains_button(widget.view.mapToScene(position))
    mouse(widget.view, QEvent.Type.MouseButtonPress, position, pressed=True)
    mouse(widget.view, QEvent.Type.MouseButtonRelease, position)
    assert widget.engine.document == before and widget.editor is None
    assert widget.view.drag_ghost is None


def test_pending_hide_is_safe_after_canvas_disposal(canvas, qtbot):
    widget, a, _, _ = canvas
    mouse(widget.view, QEvent.Type.MouseMove, fork_position(widget, a))
    mouse(widget.view, QEvent.Type.MouseMove, QPoint(10, 10))
    assert widget.view._fold_hide_timer.isActive()
    widget.dispose()
    assert not widget.view._fold_hide_timer.isActive()
    qtbot.wait(230)
    assert not widget.node_items[a].fold_control.isVisible()


@pytest.mark.parametrize("transition", ["remove", "collapse", "replace"])
def test_removed_node_is_not_kept_alive_by_external_fold_control(canvas, transition):
    """保留按钮包装对象不会延长父图元寿命，场景移除后立即完成 Qt 父子析构。

    旧实现的按钮强引用父节点，父节点又持有按钮；父子双向持有让图元滞留到
    循环垃圾回收，可能恰好在下一次 QGraphicsObject 构造的虚函数回调内析构。
    本例保留外部按钮引用，使该问题不依赖随机 GC 时机也能稳定暴露。
    """
    widget, target, _, _ = canvas
    widget.select_nodes([widget.engine.root_id])
    node_ref = ref(widget.node_items[target])
    control = widget.node_items[target].fold_control
    if transition == "remove":
        widget.engine.remove_nodes([target])
    elif transition == "collapse":
        widget.engine.set_expanded(widget.engine.root_id, False)
    else:
        widget.engine.replace_document(MindMapDocument.new("替换后的导图"))
    assert target not in widget.node_items
    assert node_ref() is None
    assert not isValid(control)
    assert control.node_item is None and not control.can_activate()


def test_repeated_canvas_destruction_clears_hover_timer_before_qt_objects_are_collected(qtbot):
    """连续销毁带待隐藏按钮的真实场景，强制释放 Qt 对象并回收 Python 包装层。"""
    for _ in range(12):
        engine = MindMapEngine()
        engine.add_child(engine.root_id, "生命周期验收")
        widget = MindMapWidget(engine, toolbar=False)
        item = widget.node_items[engine.root_id]
        widget.select_nodes([])
        item.set_hovered(True)
        item.set_hovered(False)
        assert widget.view._fold_hide_timer.isActive()
        widget.dispose()
        assert not widget.view._fold_hide_timer.isActive() and not widget.view._fold_hide_requests
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(widget) and not isValid(item)
        del item, widget
        gc.collect()
    qtbot.wait(230)


def test_hover_bridge_and_badge_corners_do_not_activate_or_start_drag(canvas):
    widget, a, b, _ = canvas
    view, item = widget.view, widget.node_items[a]
    before = widget.engine.document
    for local in (
        QPointF(item.visual.width + 2, 8),
        item.fold_control.button_rect().topLeft() + QPointF(1, 1),
    ):
        mouse(view, QEvent.Type.MouseMove, body_position(widget, a))
        position = view.mapFromScene(item.mapToScene(local))
        mouse(view, QEvent.Type.MouseMove, position)
        assert item.fold_control.isVisible() and view.itemAt(position) is item.fold_control
        assert view.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
        mouse(view, QEvent.Type.MouseButtonPress, position, pressed=True)
        mouse(view, QEvent.Type.MouseMove, position + QPoint(30, 0), pressed=True)
        mouse(view, QEvent.Type.MouseButtonRelease, position + QPoint(30, 0))
        assert widget.engine.document == before and widget.selected_node_ids() == [b]
        assert widget.editor is None and view.drag_ghost is None


def test_click_preserves_selection_nested_folds_and_single_undo(canvas):
    widget, a, b, children = canvas
    before = widget.engine.document
    control = click_control(widget, a, offset=QPoint(45, 20))
    assert not widget.engine.get_node(a).expanded
    assert painted_labels(control) == ["3"]
    assert control.toolTip() == "展开 3 个子节点"
    assert widget.selected_node_ids() == [b]
    assert widget.editor is None and widget.view.drag_ghost is None
    assert widget.view.press_node is None and not widget.view._fold_pressed
    assert widget.engine.undo() and widget.engine.document == before
    assert not widget.engine.can_undo and widget.selected_node_ids() == [b]
    assert widget.engine.redo() and not widget.engine.get_node(a).expanded
    click_control(widget, a)
    assert widget.engine.get_node(a).expanded
    assert not widget.engine.get_node(children[0]).expanded
    assert children[0] in widget.node_items
    assert all(key not in widget.node_items for key in widget.engine.get_node(children[0]).children)
    assert widget.selected_node_ids() == [b]


def test_readonly_and_inline_editor_block_button_without_committing_draft(canvas):
    widget, a, b, _ = canvas
    widget.engine.set_expanded(a, False)
    before = widget.engine.document
    widget.set_editable(False)
    control = click_control(widget, a)
    assert not control.can_activate()
    assert widget.view.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    assert widget.engine.document == before
    widget.set_editable(True)
    widget.begin_edit(b)
    editor = widget.editor
    editor.setPlainText("尚未提交的正文")
    click_control(widget, a)
    assert widget.editor is editor and editor.toPlainText() == "尚未提交的正文"
    assert widget.engine.document == before and not control.can_activate()
    widget.finish_edit(commit=False)
    assert control.can_activate()


@pytest.mark.parametrize("operation", ["add", "delete", "move", "paste"])
def test_child_count_updates_for_every_structural_command_and_undo(canvas, operation):
    widget, a, b, children = canvas
    engine = widget.engine
    engine.set_expanded(a, False)
    item, visual = widget.node_items[a], widget.node_items[a].visual
    count = 3
    if operation == "add":
        engine.add_child(a, "新增子节点")
        count += 1
    elif operation == "delete":
        engine.remove_nodes([children[1]])
        count -= 1
    elif operation == "move":
        engine.move_nodes([engine.get_node(b).children[0]], a)
        count += 1
        assert not widget.node_items[b].fold_control.isVisible()
    else:
        engine.paste_fragment(a, engine.copy_fragment([b]))
        count += 1
    assert widget.node_items[a] is item and item.visual is visual
    # 新增、粘贴和移动遵循引擎既有语义自动展开；撤销应恢复原有收起状态及计数。
    assert engine.undo() and not engine.get_node(a).expanded
    assert painted_labels(item.fold_control) == ["3"]
    assert engine.redo()
    engine.set_expanded(a, False)
    assert painted_labels(item.fold_control) == [str(count)]


def test_last_child_removal_hides_control_and_undo_restores_it(canvas):
    widget, _, b, _ = canvas
    control = widget.node_items[b].fold_control
    widget.engine.remove_nodes(widget.engine.get_node(b).children)
    assert not control.isVisible()
    assert widget.engine.undo()
    assert control.isVisible() and painted_labels(control) == ["1"]


def test_multi_digit_badge_has_complete_count_and_follows_canvas_background(canvas):
    widget, a, _, _ = canvas
    with widget.engine.transaction("生成三位数的直接子节点"):
        for index in range(120):
            widget.engine.add_child(a, f"计数主题 {index}")
        widget.engine.set_expanded(a, False)
    control = widget.node_items[a].fold_control
    assert painted_labels(control) == ["123"]
    assert control.button_rect().width() >= (
        QFontMetricsF(widget.node_items[a].visual.tag_font).horizontalAdvance("123") + 8
    )
    widget.scene.setBackgroundBrush(QColor("#182837"))
    image = QImage(100, 40, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    rect = control.button_rect()
    painter.translate(10 - rect.left(), 10 - rect.top())
    control.paint(painter, None)
    painter.end()
    assert image.pixelColor(round(10 + rect.width() / 2), 13).name() == "#182837"


@pytest.mark.parametrize("direction", ["right", "left", "both", "down"])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scale", [0.5, 1.0, 2.0])
def test_direction_theme_and_zoom_keep_controls_outside_body(canvas, direction, compact, dark, scale):
    widget, a, b, _ = canvas
    widget.engine.set_theme(Theme.dark() if dark else Theme())
    widget.engine.set_layout(LayoutConfig(direction=direction, compact=compact))
    widget.view.resetTransform()
    widget.view.scale(scale, scale)
    widget.select_nodes([widget.engine.root_id, a, b])
    sides = widget.engine.document.branch_sides()
    for key in (widget.engine.root_id, a, b):
        item = widget.node_items[key]
        control = item.fold_control
        expected_side = sides[key]
        rect = control.button_rect()
        assert control.direction == expected_side
        if expected_side == "left":
            assert rect.right() == -4
        elif expected_side == "down":
            assert rect.top() == item.visual.height + 4
        else:
            assert rect.left() == item.visual.width + 4
        assert item.boundingRect() == QRectF(-2, -2, item.visual.width + 4, item.visual.height + 4)
        assert not item.shape().contains(rect.center())
        widget.view.centerOn(item.sceneBoundingRect().center())
        position = widget.view.mapFromScene(control.mapToScene(rect.center()))
        mouse(widget.view, QEvent.Type.MouseMove, position)
        assert isinstance(widget.view.itemAt(position), FoldControlItem)
        assert widget.view.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor


@pytest.mark.parametrize("content", ["plain", "image", "tags"])
def test_parent_body_has_no_extra_padding_for_children(canvas, content):
    widget, a, _, children = canvas
    leaf = children[1]
    for key in (a, leaf):
        widget.engine.update_node(key, text="相同的正文", tags=["较长的标签内容"] if content == "tags" else [])
        if content == "image":
            image = QImage(180, 70, QImage.Format.Format_ARGB32)
            image.fill(QColor("#e95e38"))
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            assert image.save(buffer, "PNG")
            widget.engine.attach_image(key, bytes(buffer.data()))
    assert widget.node_items[a].visual.width == widget.node_items[leaf].visual.width
    assert widget.node_items[a].visual.height == widget.node_items[leaf].visual.height


@pytest.mark.parametrize("suffix", ["png", "svg", "pdf"])
@pytest.mark.parametrize("expand_all", [False, True])
def test_exports_never_paint_external_controls_or_change_fold_state(canvas, tmp_path, monkeypatch, suffix, expand_all):
    widget, a, _, _ = canvas
    widget.engine.set_expanded(a, False)
    before = widget.engine.document

    def forbidden(*args, **kwargs):
        raise AssertionError("图形导出不应绘制减号或数量按钮")

    # 先排空画布的待绘制事件；只在导出调用内禁用标记绘制，避免测试结束时
    # 正常界面重绘被误当成导出内容，同时仍能捕获误用当前交互场景的导出实现。
    widget.hide()
    QApplication.processEvents()
    target = tmp_path / f"折叠导出.{suffix}"
    with monkeypatch.context() as patch:
        patch.setattr(NodeVisual, "paint_fold_control", forbidden)
        widget.export(target, expand_all=expand_all)
    assert target.stat().st_size > 100
    assert widget.engine.document == before
