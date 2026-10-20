"""画布手势、光标、滚轮路由和工具栏状态的回归测试。"""

import pytest
from mindmap_engine import LayoutConfig, MindMapEngine, MindMapError, Theme
from mindmap_qt import MindMapWidget
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QFocusEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture
def canvas(qtbot):
    """每例使用独立引擎和视口，避免偏好、选择及鼠标捕获相互污染。"""
    engine = MindMapEngine()
    a = engine.add_child(engine.root_id, "第一分支")
    b = engine.add_child(engine.root_id, "第二分支")
    engine.add_child(a, "可折叠的子节点")
    engine.set_layout(LayoutConfig(direction="right"))
    engine.replace_document(engine.document)
    widget = MindMapWidget(engine)
    qtbot.addWidget(widget)
    widget.resize(1100, 750)
    widget.show()
    widget.activateWindow()
    widget.view.setFocus()
    QApplication.processEvents()
    widget.fit_to_view()
    yield widget, a, b
    widget.finish_edit(commit=False)
    widget.dispose()


def mouse(
    view,
    kind,
    position,
    button=Qt.MouseButton.NoButton,
    buttons=Qt.MouseButton.NoButton,
    modifiers=Qt.KeyboardModifier.NoModifier,
):
    """经真实视口事件路由派发输入，显式携带按钮状态，不移动系统鼠标。"""
    event = QMouseEvent(
        kind, QPointF(position), QPointF(view.viewport().mapToGlobal(position)), button, buttons, modifiers
    )
    QApplication.sendEvent(view.viewport(), event)


def wheel(view, *, angle=120, pixels=0, modifiers=Qt.KeyboardModifier.NoModifier, position=None):
    """模拟普通滚轮或触控板；返回事件以检查零增量是否被忽略。"""
    position = position if position is not None else view.viewport().rect().center() + QPoint(70, -60)
    event = QWheelEvent(
        QPointF(position),
        QPointF(view.viewport().mapToGlobal(position)),
        QPoint(0, pixels),
        QPoint(0, angle),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(view.viewport(), event)
    return event


def context(view, position, reason=QContextMenuEvent.Reason.Mouse):
    event = QContextMenuEvent(reason, position, view.viewport().mapToGlobal(position))
    QApplication.sendEvent(view.viewport(), event)


@pytest.mark.parametrize("target", ["blank", "node", "readonly"])
def test_right_drag_preserves_document_and_selection(canvas, monkeypatch, target):
    widget, a, b = canvas
    view = widget.view
    widget.select_nodes([a])
    start = QPoint(40, 40)
    if target in ("node", "readonly"):
        start = view.mapFromScene(widget.node_items[b].sceneBoundingRect().center())
        if target == "readonly":
            widget.set_editable(False)
    selected = set(widget.scene.selectedItems())
    before, revision = widget.engine.document, widget.engine.revision
    menus = []
    monkeypatch.setattr(widget, "context_menu", menus.append)
    old_x, old_y = view.horizontalScrollBar().value(), view.verticalScrollBar().value()
    end = start + QPoint(45, 35)

    mouse(view, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.RightButton, Qt.MouseButton.RightButton)
    # 同时覆盖在按下阶段就派发原生菜单事件的平台。
    context(view, start)
    assert not menus
    mouse(view, QEvent.Type.MouseMove, end, buttons=Qt.MouseButton.RightButton)
    assert view.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert view.horizontalScrollBar().value() == old_x - 45
    assert view.verticalScrollBar().value() == old_y - 35
    mouse(view, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.RightButton)
    context(view, end)

    assert not menus and not view.panning
    assert set(widget.scene.selectedItems()) == selected
    assert widget.engine.document == before and widget.engine.revision == revision
    assert view.viewport().cursor().shape() != Qt.CursorShape.ClosedHandCursor
    # 上一次拖动不会屏蔽键盘菜单。
    context(view, end, QContextMenuEvent.Reason.Keyboard)
    assert len(menus) == 1


def test_right_click_with_jitter_opens_one_menu_and_selects_target(canvas, monkeypatch):
    widget, a, b = canvas
    view = widget.view
    widget.select_nodes([a])
    start = view.mapFromScene(widget.node_items[b].sceneBoundingRect().center())
    end = start + QPoint(QApplication.startDragDistance() - 1, 0)
    menus = []
    monkeypatch.setattr(widget, "context_menu", menus.append)
    old_position = view.mapToScene(start)
    mouse(view, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.RightButton, Qt.MouseButton.RightButton)
    context(view, start)
    mouse(view, QEvent.Type.MouseMove, end, buttons=Qt.MouseButton.RightButton)
    assert not view.panning and widget.selected_node_ids() == [a]
    mouse(view, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.RightButton)
    context(view, end)
    assert len(menus) == 1 and widget.selected_node_ids() == [b]
    assert view.mapToScene(start) == old_position


@pytest.mark.parametrize(
    "button,modifiers",
    [
        (Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier),
        (Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier),
        (Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier),
    ],
)
def test_pan_ends_only_for_its_own_button(canvas, monkeypatch, button, modifiers):
    widget, _, _ = canvas
    view, start = widget.view, QPoint(40, 40)
    monkeypatch.setattr(widget, "context_menu", lambda position: None)
    mouse(view, QEvent.Type.MouseButtonPress, start, button, button, modifiers)
    mouse(view, QEvent.Type.MouseMove, start + QPoint(30, 20), buttons=button)
    other = Qt.MouseButton.LeftButton if button != Qt.MouseButton.LeftButton else Qt.MouseButton.MiddleButton
    mouse(view, QEvent.Type.MouseButtonRelease, start, other, button)
    assert view.panning
    mouse(view, QEvent.Type.MouseButtonRelease, start, button)
    assert not view.panning


@pytest.mark.parametrize("interruption", ["focus", "capture"])
def test_interrupted_pan_never_opens_menu(canvas, monkeypatch, interruption):
    widget, _, _ = canvas
    view, start = widget.view, QPoint(40, 40)
    menus = []
    monkeypatch.setattr(widget, "context_menu", menus.append)
    mouse(view, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.RightButton, Qt.MouseButton.RightButton)
    mouse(view, QEvent.Type.MouseMove, start + QPoint(40, 20), buttons=Qt.MouseButton.RightButton)
    if interruption == "focus":
        QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut))
    else:
        QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.UngrabMouse))
    assert not view.panning
    assert view.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    mouse(view, QEvent.Type.MouseButtonRelease, start, Qt.MouseButton.RightButton)
    context(view, start)
    assert not menus


def test_hover_cursors_follow_target_and_readonly_state(canvas):
    widget, a, b = canvas
    view = widget.view
    node = widget.node_items[a]
    body = view.mapFromScene(node.sceneBoundingRect().center())
    fold = view.mapFromScene(node.mapToScene(node.visual.fold_rect().center()))
    for position, shape in (
        (QPoint(30, 30), Qt.CursorShape.ArrowCursor),
        (body, Qt.CursorShape.OpenHandCursor),
        (fold, Qt.CursorShape.PointingHandCursor),
    ):
        mouse(view, QEvent.Type.MouseMove, position)
        assert view.viewport().cursor().shape() == shape
    mouse(view, QEvent.Type.MouseMove, body)
    widget.set_editable(False)
    assert view.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    widget.set_editable(True)
    assert view.viewport().cursor().shape() == Qt.CursorShape.OpenHandCursor
    widget.begin_edit(a)
    text = view.mapFromScene(widget.editor.sceneBoundingRect().center())
    mouse(view, QEvent.Type.MouseMove, text)
    assert view.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
    widget.finish_edit(commit=False)


def test_node_drag_cursor_and_interruption_discard_preview(canvas):
    widget, a, _ = canvas
    view = widget.view
    position = view.mapFromScene(widget.node_items[a].sceneBoundingRect().center())
    before = widget.engine.document
    mouse(view, QEvent.Type.MouseButtonPress, position, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    mouse(view, QEvent.Type.MouseMove, position + QPoint(30, 10), buttons=Qt.MouseButton.LeftButton)
    assert view.drag_ghost is not None
    assert view.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut))
    assert view.drag_ghost is None
    mouse(view, QEvent.Type.MouseButtonRelease, position, Qt.MouseButton.LeftButton)
    assert widget.engine.document == before
    assert view.viewport().cursor().shape() != Qt.CursorShape.ClosedHandCursor


def test_node_drag_release_over_blank_restores_arrow_and_releases_capture(canvas):
    widget, a, _ = canvas
    view = widget.view
    start = view.mapFromScene(widget.node_items[a].sceneBoundingRect().center())
    end = QPoint(40, 40)
    before = widget.engine.document
    mouse(view, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    mouse(view, QEvent.Type.MouseMove, end, buttons=Qt.MouseButton.LeftButton)
    assert view.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    mouse(view, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton)
    assert view.scene().mouseGrabberItem() is None
    assert view.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    assert widget.engine.document == before


def test_wheel_mode_api_menu_and_instance_isolation(canvas, qtbot):
    widget, _, _ = canvas
    modes = []
    widget.wheel_mode_changed.connect(modes.append)
    before, revision = widget.engine.document, widget.engine.revision
    assert widget.wheel_mode == "scroll" and widget.action("wheel_scroll").isChecked()
    widget.action("wheel_zoom").trigger()
    widget.set_wheel_mode("zoom")
    assert modes == ["zoom"] and not widget.action("wheel_scroll").isChecked()
    assert widget.action("wheel_zoom").isChecked()
    assert "滚轮缩放" in widget.action("wheel_mode").toolTip()
    assert widget.action("wheel_mode").menu() is widget.wheel_menu
    assert widget.toolbar.widgetForAction(widget.action("wheel_mode")) is None
    assert widget.engine.document == before and widget.engine.revision == revision
    assert not widget.engine.is_dirty
    with pytest.raises(AttributeError):
        widget.wheel_mode = "scroll"
    other = MindMapWidget(toolbar=False)
    qtbot.addWidget(other)
    assert other.wheel_mode == "scroll"
    other.dispose()


@pytest.mark.parametrize("invalid", ["", "invalid", None, ["zoom"]])
def test_invalid_wheel_mode_preserves_state(canvas, invalid):
    widget, _, _ = canvas
    with pytest.raises(MindMapError, match="滚轮模式"):
        widget.set_wheel_mode(invalid)
    assert widget.wheel_mode == "scroll" and widget.action("wheel_scroll").isChecked()


@pytest.mark.parametrize("mode,ctrl", [("scroll", False), ("scroll", True), ("zoom", False), ("zoom", True)])
@pytest.mark.parametrize("pixel_input", [False, True])
def test_wheel_routes_scroll_zoom_and_ctrl_override(canvas, mode, ctrl, pixel_input):
    widget, _, _ = canvas
    view = widget.view
    widget.set_wheel_mode(mode)
    before, revision = widget.engine.document, widget.engine.revision
    scale = view.transform().m11()
    old_y, old_x = view.verticalScrollBar().value(), view.horizontalScrollBar().value()
    position = view.viewport().rect().center() + QPoint(70, -60)
    anchor = view.mapToScene(position)
    modifiers = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    wheel(
        view,
        angle=0 if pixel_input else 120,
        pixels=30 if pixel_input else 0,
        modifiers=modifiers,
        position=position,
    )
    if mode == "zoom" or ctrl:
        assert view.transform().m11() == pytest.approx(scale * 1.15 ** ((30 if pixel_input else 120) / 120))
        assert (view.mapToScene(position) - anchor).manhattanLength() < 2 / scale
    else:
        assert view.transform().m11() == scale
        assert view.verticalScrollBar().value() < old_y
        assert view.horizontalScrollBar().value() == old_x
    assert widget.wheel_mode == mode
    assert widget.engine.document == before and widget.engine.revision == revision


def test_zoom_limits_zero_input_and_edit_commit(canvas):
    widget, a, _ = canvas
    view = widget.view
    widget.begin_edit(a)
    widget.editor.setPlainText("滚轮提交的文字")
    wheel(view, angle=120)
    assert widget.editor is not None
    widget.set_wheel_mode("zoom")
    assert all(widget.action(key).isEnabled() for key in ("wheel_mode", "wheel_scroll", "wheel_zoom"))
    assert not wheel(view, angle=0).isAccepted()
    assert widget.editor is not None
    wheel(view)
    assert widget.editor is None and widget.engine.get_node(a).text == "滚轮提交的文字"
    widget.engine.undo()
    assert not widget.engine.can_undo
    wheel(view, angle=12000)
    assert view.transform().m11() == pytest.approx(4.0)
    wheel(view, angle=-12000)
    assert view.transform().m11() == pytest.approx(0.1)
    widget.set_editable(False)
    widget.action("wheel_scroll").trigger()
    assert widget.wheel_mode == "scroll" and widget.action("wheel_mode").isEnabled()


@pytest.mark.parametrize("scale", [0.12, 0.3, 1.0, 3.8])
@pytest.mark.parametrize("angle", [120, -120])
def test_zoom_keeps_cursor_anchor_across_scale_range(canvas, scale, angle):
    widget, _, _ = canvas
    widget.set_wheel_mode("zoom")
    view = widget.view
    view.resetTransform()
    view.zoom(scale)
    view.centerOn(0, 0)
    position = view.viewport().rect().center() + QPoint(150, -100)
    before = view.mapToScene(position)
    wheel(view, angle=angle, position=position)
    assert (view.mapToScene(position) - before).manhattanLength() < 2 / scale


def test_right_pan_still_moves_canvas_at_minimum_zoom(canvas):
    widget, _, _ = canvas
    view = widget.view
    view.zoom(0.001)
    start = QPoint(40, 40)
    before = view.mapToScene(start)
    mouse(view, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.RightButton, Qt.MouseButton.RightButton)
    mouse(view, QEvent.Type.MouseMove, start + QPoint(45, 35), buttons=Qt.MouseButton.RightButton)
    mouse(view, QEvent.Type.MouseButtonRelease, start + QPoint(45, 35), Qt.MouseButton.RightButton)
    assert (view.mapToScene(start) - before).manhattanLength() > 100


def test_wheel_mode_survives_theme_refresh_undo_and_document_reset(canvas):
    widget, a, _ = canvas
    widget.set_wheel_mode("zoom")
    icon = widget.action("wheel_mode").icon().cacheKey()
    widget.engine.set_theme(Theme.dark())
    assert widget.action("wheel_mode").icon().cacheKey() != icon
    widget.engine.update_node(a, text="新名字")
    widget.engine.undo()
    widget.engine.replace_document(widget.engine.document)
    assert widget.wheel_mode == "zoom" and widget.action("wheel_zoom").isChecked()
    assert widget.action("wheel_mode").property("mindmap_toolbar_icon") == "wheel_zoom"


@pytest.mark.parametrize("scale", [0.1, 1.0, 4.0])
@pytest.mark.parametrize("size", [(720, 480), (1440, 900)])
def test_canvas_hides_scrollbars_and_leaves_one_screen_margin(canvas, scale, size):
    """大小导图在不同倍率和窗口尺寸下均保留一屏余量，视图操作不修改文档。"""
    widget, a, _ = canvas
    view = widget.view
    for large in (False, True):
        if large:
            with widget.engine.transaction("构造大导图"):
                for index in range(30):
                    widget.engine.add_child(a, f"自动扩展的节点 {index}")
        revision, document = widget.engine.revision, widget.engine.document
        widget.resize(*size)
        QApplication.processEvents()
        view.zoom(scale / view.transform().m11())
        assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert not view.horizontalScrollBar().isVisible()
        assert not view.verticalScrollBar().isVisible()
        # 将两种边界映射到屏幕尺度验证边距，避免只在某个场景单位或倍率下正确。
        content = view.transform().mapRect(widget.scene.sceneRect())
        navigation = view.transform().mapRect(view.sceneRect())
        assert content.left() - navigation.left() == pytest.approx(view.viewport().width())
        assert navigation.right() - content.right() == pytest.approx(view.viewport().width())
        assert content.top() - navigation.top() == pytest.approx(view.viewport().height())
        assert navigation.bottom() - content.bottom() == pytest.approx(view.viewport().height())
        assert widget.engine.revision == revision and widget.engine.document == document


@pytest.mark.parametrize("direction", ["left", "right", "both", "down"])
@pytest.mark.parametrize("operation", ["delete", "fold"])
def test_content_shrink_and_history_keep_valid_center_and_scale(canvas, direction, operation):
    widget, a, _ = canvas
    view = widget.view
    with widget.engine.transaction("构造可收缩的分支"):
        widget.engine.set_layout(LayoutConfig(direction=direction))
        for index in range(30):
            widget.engine.add_child(a, f"分支内容 {index}")
    expanded = view.sceneRect()
    # 根节点附近在收缩前后都属于有效导航区域，单独验证无需钳制时的中心保持。
    view.centerOn(widget.node_items[widget.engine.root_id].sceneBoundingRect().center() + QPointF(45, -20))
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    scale = view.transform().m11()
    if operation == "delete":
        widget.engine.remove_nodes([a])
    else:
        widget.engine.set_expanded(a, False)
    reduced = view.sceneRect()
    assert reduced.width() < expanded.width() or reduced.height() < expanded.height()
    for step, bounds in enumerate((reduced, expanded, reduced)):
        if step == 1:
            widget.engine.undo()
        elif step == 2:
            widget.engine.redo()
        assert view.sceneRect() == bounds
        assert view.transform().m11() == scale
        actual = view.mapToScene(view.viewport().rect()).boundingRect().center()
        assert (actual - center).manhattanLength() < 2 / scale


@pytest.mark.parametrize("operation", ["delete", "fold", "load"])
def test_shrink_clamps_an_outside_view_to_nearest_boundary(canvas, operation):
    widget, a, _ = canvas
    view = widget.view
    with widget.engine.transaction("构造远离根节点的内容"):
        widget.engine.set_layout(LayoutConfig(direction="down"))
        for index in range(40):
            widget.engine.add_child(a, f"节点 {index}")
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    before = view.mapToScene(view.viewport().rect()).boundingRect().center()
    scale = view.transform().m11()
    if operation == "delete":
        widget.engine.remove_nodes([a])
    elif operation == "fold":
        widget.engine.set_expanded(a, False)
    else:
        widget.engine.replace_document(MindMapEngine().document)
    visible = view.mapToScene(view.viewport().rect()).boundingRect()
    tolerance = 2 / scale
    assert view.sceneRect().adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(visible)
    assert view.horizontalScrollBar().value() == view.horizontalScrollBar().maximum()
    assert view.verticalScrollBar().value() == view.verticalScrollBar().maximum()
    assert (visible.center() - before).manhattanLength() > 100
    assert view.transform().m11() == scale


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize(
    "button,modifiers",
    [
        (Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier),
        (Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier),
        (Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier),
    ],
)
def test_repeated_pan_and_wheel_stop_at_content_margin(canvas, direction, button, modifiers):
    """连续平移不能扩大空白区域；只读状态下相同手势仍可导航。"""
    widget, _, _ = canvas
    widget.set_editable(False)
    view, start = widget.view, QPoint(40, 40)
    bounds = view.sceneRect()
    document, revision = widget.engine.document, widget.engine.revision
    end = start + QPoint(direction * 250, direction * 200)
    for _ in range(24):
        mouse(view, QEvent.Type.MouseButtonPress, start, button, button, modifiers)
        mouse(view, QEvent.Type.MouseMove, end, buttons=button, modifiers=modifiers)
        mouse(view, QEvent.Type.MouseButtonRelease, end, button, modifiers=modifiers)
        assert view.sceneRect() == bounds
    for bar in (view.horizontalScrollBar(), view.verticalScrollBar()):
        assert bar.value() == (bar.minimum() if direction == 1 else bar.maximum())
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    for _ in range(3):
        wheel(view, angle=0, pixels=direction * 10000)
    assert view.mapToScene(view.viewport().rect()).boundingRect().center() == center
    assert view.sceneRect() == bounds
    assert widget.engine.document == document and widget.engine.revision == revision


@pytest.mark.parametrize("edge", ["minimum", "maximum"])
def test_zoom_at_boundary_stays_inside_content_margin(canvas, edge):
    widget, _, _ = canvas
    view = widget.view
    widget.set_wheel_mode("zoom")
    view.zoom(1 / view.transform().m11())
    for bar in (view.horizontalScrollBar(), view.verticalScrollBar()):
        bar.setValue(getattr(bar, edge)())
    position = view.viewport().rect().center() + QPoint(150, -100)
    for angle in (120, 120, -120, -120):
        wheel(view, angle=angle, position=position)
        visible = view.mapToScene(view.viewport().rect()).boundingRect()
        tolerance = 2 / view.transform().m11()
        assert view.sceneRect().adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(visible)


def test_resize_and_refresh_preserve_view_center(canvas):
    widget, _, b = canvas
    view = widget.view
    view.centerOn(widget.node_items[b].sceneBoundingRect().center() + QPointF(40, -30))
    center = view.mapToScene(view.viewport().rect()).boundingRect().center()
    scale = view.transform().m11()
    for size in ((800, 600), (1600, 900), (720, 480), (1100, 750)):
        widget.resize(*size)
        QApplication.processEvents()
        widget.refresh()
        assert view.transform().m11() == scale
        actual = view.mapToScene(view.viewport().rect()).boundingRect().center()
        assert (actual - center).manhattanLength() < 2 / scale


def test_fit_focus_and_center_use_content_instead_of_navigation_margin(canvas):
    widget, a, b = canvas
    view = widget.view
    with widget.engine.transaction("构造适应窗口的大导图"):
        for index in range(40):
            widget.engine.add_child(a, f"完整展示的节点 {index}")
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    for focus in (None, b, None):
        widget.focus_node(focus)
        visible = view.mapToScene(view.viewport().rect()).boundingRect()
        assert visible.contains(widget.scene.sceneRect())
        assert widget.scene.sceneRect().width() < view.sceneRect().width()
        scale = view.transform().m11()
        widget.center_root()
        center = view.mapToScene(view.viewport().rect()).boundingRect().center()
        node_center = widget.node_items[focus or widget.engine.root_id].sceneBoundingRect().center()
        assert (center - node_center).manhattanLength() < 2 / scale
        assert view.transform().m11() == scale


@pytest.mark.parametrize("direction", ["left", "right", "down"])
def test_fit_capped_zoom_keeps_asymmetric_content_fully_visible(canvas, direction):
    """放大上限生效时仍居中整个内容，避免单向布局因根节点居中而裁掉远端。"""
    widget, a, _ = canvas
    widget.engine.set_layout(LayoutConfig(direction=direction))
    widget.engine.set_expanded(a, False)
    content = widget.scene.sceneRect()
    widget.resize(round(content.width() * 2.2), round(content.height() * 2.2) + widget.toolbar.height())
    QApplication.processEvents()
    widget.fit_to_view()
    assert widget.view.transform().m11() == pytest.approx(2)
    visible = widget.view.mapToScene(widget.view.viewport().rect()).boundingRect()
    assert visible.contains(content)
