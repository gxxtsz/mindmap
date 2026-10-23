"""侧栏分组回归：实际控件几何、绘制、拖放和持久化共同验证。"""

import pytest
from mindmap_docking import DockPosition, DockTarget
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication

POSITIONS = {
    "left": (DockPosition.LEFT_TOP, DockPosition.LEFT_BOTTOM, DockPosition.BOTTOM_LEFT),
    "right": (DockPosition.RIGHT_TOP, DockPosition.RIGHT_BOTTOM, DockPosition.BOTTOM_RIGHT),
}


def populate(docking, side, count=1):
    """卸载默认入口后注册指定侧的三组工具；不创建业务内容。"""
    for tool_id in list(docking.manager.states):
        docking.manager.unregister_tool_window(tool_id, keep_layout=False)
    members = {}
    for position in POSITIONS[side]:
        members[position] = [f"{position}_{n}" for n in range(count)]
        for tool_id in members[position]:
            docking.register(tool_id, position)
    docking.manager.refresh()
    QApplication.processEvents()
    return getattr(docking.host, f"{side}_rail"), members


def enter_tool(rail, tool_id):
    """返回 MIME 强引用；Qt 拖放事件不拥有传入的 QMimeData。"""
    mime = rail.manager.encode_drag("tool", tool_id)
    event = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    rail.dragEnterEvent(event)
    assert event.isAccepted()
    return mime


def drop_tool(rail, mime, point):
    event = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    rail.dropEvent(event)
    return event


def assert_no_overlap(rail):
    """更多入口与显示按钮均在栏内，且任意两个可操作矩形不相交。"""
    rectangles = [button.geometry() for button in rail._buttons.values() if button.isVisible()]
    rectangles.append(rail.more.geometry())
    for index, rect in enumerate(rectangles):
        assert rail.rect().contains(rect)
        assert all(not rect.intersects(other) for other in rectangles[index + 1:])


@pytest.mark.parametrize("position", list(DockPosition))
@pytest.mark.parametrize("focus_owner", ["panel", "editor", "other_tool"])
def test_stripe_click_closes_open_panel_regardless_of_focus(docking, qtbot, position, focus_owner):
    """六个停靠位置的入口均按显隐切换；焦点离开面板后也必须单击收起。"""
    manager, host = docking.manager, docking.host
    docking.register("target", position)
    other_position = DockPosition.RIGHT_TOP if position != DockPosition.RIGHT_TOP else DockPosition.LEFT_TOP
    docking.register("other", other_position)
    manager.refresh()
    rail = host.left_rail if position in POSITIONS["left"] else host.right_rail
    button = rail._buttons["target"]

    # 从真实按钮打开，随后将焦点移到画布或另一个工具，复现已经展开但不活动的状态。
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    panel = manager.panel("target")
    content = panel.content
    content.setPlainText("再次打开时保留内容")
    qtbot.waitUntil(lambda: QApplication.focusWidget() is content)
    if focus_owner == "editor":
        docking.document.setFocus()
        qtbot.waitUntil(lambda: manager.active_id == "")
    elif focus_owner == "other_tool":
        manager.activate("other")
        qtbot.waitUntil(lambda: QApplication.focusWidget() is manager.panel("other").content)
        qtbot.waitUntil(lambda: manager.active_id == "other")
    assert manager.states["target"].visible and panel.isVisible() and button.isChecked()

    # 按下与释放之间处理焦点回调，确保一次完整鼠标操作不会只激活面板或重新打开。
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert not manager.states["target"].visible
    assert not panel.isVisible()
    assert not host.zones[position].isVisible()
    assert not button.isChecked()
    if focus_owner == "other_tool":
        assert manager.states["other"].visible

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is content)
    assert manager.states["target"].visible and panel.isVisible() and button.isChecked()
    assert manager.panel("target").content is content
    assert content.toPlainText() == "再次打开时保留内容"
    assert docking.calls["target"] == 1


def test_stripe_keyboard_activation_closes_open_panel(docking, qtbot):
    """Tab 导航到入口后空格同样单次收起，不依赖工具是否仍为活动项。"""
    manager = docking.manager
    manager.activate("alpha")
    button = docking.host.left_rail._buttons["alpha"]
    button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is button and manager.active_id == "")
    assert manager.states["alpha"].visible and button.isChecked()

    qtbot.keyClick(button, Qt.Key.Key_Space)
    QApplication.processEvents()
    assert not manager.states["alpha"].visible
    assert not manager.panel("alpha").isVisible()
    assert not button.isChecked()


@pytest.mark.parametrize("entry", ["action", "shortcut"])
@pytest.mark.parametrize("focus_owner", ["panel", "editor", "other_tool"])
def test_tool_shortcut_closes_open_panel_regardless_of_focus(docking, qtbot, entry, focus_owner):
    """动作和真实快捷键均直接收起可见面板，保留其他控件焦点并复用业务内容。"""
    manager = docking.manager
    manager.activate("alpha")
    panel = manager.panel("alpha")
    content = panel.content
    content.setPlainText("快捷键收起后保留内容")
    button = docking.host.left_rail._buttons["alpha"]
    qtbot.waitUntil(lambda: QApplication.focusWidget() is content and manager.active_id == "alpha")
    expected_focus = docking.document
    expected_active = ""
    if focus_owner == "editor":
        docking.document.setFocus()
        qtbot.waitUntil(lambda: QApplication.focusWidget() is docking.document and manager.active_id == "")
    elif focus_owner == "other_tool":
        manager.activate("beta")
        expected_focus = manager.panel("beta").content
        expected_active = "beta"
        qtbot.waitUntil(lambda: QApplication.focusWidget() is expected_focus and manager.active_id == "beta")
    assert manager.states["alpha"].visible and panel.isVisible() and button.isChecked()

    def toggle():
        """分别覆盖菜单复用的 QAction 和当前焦点控件收到的真实 Alt+1 按键。"""
        if entry == "action":
            manager.actions["tool:alpha"].trigger()
        else:
            qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_1, Qt.KeyboardModifier.AltModifier)
        QApplication.processEvents()

    toggle()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is expected_focus and manager.active_id == expected_active)
    assert not manager.states["alpha"].visible
    assert not panel.isVisible() and not button.isChecked()
    if focus_owner == "other_tool":
        assert manager.states["beta"].visible and manager.panel("beta").isVisible()

    toggle()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is content and manager.active_id == "alpha")
    assert manager.states["alpha"].visible and panel.isVisible() and button.isChecked()
    assert manager.panel("alpha").content is content
    assert content.toPlainText() == "快捷键收起后保留内容" and docking.calls["alpha"] == 1

    toggle()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is docking.document and manager.active_id == "")
    assert not manager.states["alpha"].visible
    assert not panel.isVisible() and not button.isChecked()


@pytest.mark.parametrize("side", POSITIONS)
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
def test_group_separator_is_painted_and_bottom_stays_at_bottom(docking, side, theme, compact):
    rail, members = populate(docking, side)
    docking.manager.set_theme(theme)
    docking.manager.set_option("compact", compact)
    QApplication.processEvents()
    top, lower, bottom = [rail._buttons[members[p][0]] for p in POSITIONS[side]]
    height = 34 if compact else 40
    # 工具和更多入口均只显示图标，占用相同方格。
    for button in [top, lower, bottom, rail.more]:
        assert (button.width(), button.height()) == (height, height)
    assert rail.more.text() == ""
    assert top.text() == ""
    assert rail.more.icon_name == "more"
    separator_height = 8 if compact else 12
    assert lower.y() == top.y() + top.height() + separator_height
    assert rail.more.y() == lower.y() + lower.height()
    assert bottom.y() + bottom.height() == rail.height() - 4
    assert_no_overlap(rail)
    image = rail.grab().toImage()
    dpr = image.devicePixelRatio()
    line_y = top.y() + top.height() + separator_height // 2
    assert image.pixelColor(int(rail.width() / 2 * dpr), int(line_y * dpr)) == (
        docking.manager.theme.color("tool-window-border"))
    line_start = (rail.width() - 24) // 2
    assert image.pixelColor(int(line_start * dpr), int(line_y * dpr)) == (
        docking.manager.theme.color("tool-window-border"))
    assert docking.calls == {}


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("active", [False, True])
def test_rendered_icons_use_actual_canvas_size_including_active_modes(
        docking, tmp_path, theme, compact, active):
    """用满画布 SVG 检查真实像素，防止资源请求变小而绘制或选中着色仍放大。"""
    rail, members = populate(docking, "left")
    manager = docking.manager
    source = tmp_path / "画布.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
                      '<rect width="20" height="20" fill="currentColor"/></svg>', encoding="utf-8")
    for name in ["panel", "more"]:
        manager.theme.icons.register(name, source)
    manager.set_theme(theme)
    manager.set_option("compact", compact)
    tool_id = members[DockPosition.LEFT_TOP][0]
    if active:
        manager.activate(tool_id, focus=False)
    QApplication.processEvents()
    icon_size = 16 if compact else 20
    for button in [rail._buttons[tool_id], rail.more]:
        image = button.grab().toImage()
        dpr = image.devicePixelRatio()
        color = manager.theme.color("ToolWindow.Button.selectedForeground" if active and button is not rail.more
                                    else "text-muted")

        def matches(x, y):
            return image.pixelColor(int((x + 0.5) * dpr), int((y + 0.5) * dpr)) == color

        left = (button.width() - icon_size) // 2
        top = (button.height() - icon_size) // 2
        # 横向和纵向分别检查图标中心，确认实际绘制尺寸与主题一致。
        columns = [x for x in range(button.width())
                   if matches(x, button.height() // 2)]
        rows = [y for y in range(button.height()) if matches(left + icon_size // 2, y)]
        assert columns == list(range(left, left + icon_size))
        assert rows == list(range(top, top + icon_size))


@pytest.mark.parametrize("fail", [False, True])
def test_more_menu_releases_pressed_feedback_on_close_and_error(docking, monkeypatch, fail):
    """菜单的同步嵌套事件循环保留按下反馈；普通关闭和异常都不能留下按下状态。"""
    rail = docking.host.left_rail
    released = []

    class Menu:
        def exec(self, point):
            assert rail.more.isDown()
            if fail:
                raise RuntimeError("菜单执行失败")

        def deleteLater(self):
            released.append(True)

    monkeypatch.setattr(rail, "_build_more_menu", Menu)
    if fail:
        with pytest.raises(RuntimeError, match="菜单执行失败"):
            rail.show_more()
    else:
        rail.show_more()
    assert not rail.more.isDown()
    assert released == [True]


@pytest.mark.parametrize("side", POSITIONS)
def test_closed_tool_keeps_group_but_disabled_and_unregistered_tools_do_not(docking, side):
    rail, members = populate(docking, side)
    top, lower, _ = [members[p][0] for p in POSITIONS[side]]
    docking.manager.activate(lower, focus=False)
    docking.manager.hide(lower)
    assert rail._buttons[lower].isVisible()
    assert not rail._layout_data.separator.isEmpty()
    docking.manager.set_available(top, False)
    assert rail._layout_data.separator.isEmpty()
    assert rail._buttons[lower].y() == 0
    docking.manager.set_available(top, True)
    assert not rail._layout_data.separator.isEmpty()
    docking.manager.unregister_tool_window(lower)
    assert rail._layout_data.separator.isEmpty()
    assert lower not in rail._buttons


@pytest.mark.parametrize("side", POSITIONS)
def test_overflow_reserves_each_group_and_active_tool_without_inflating_window(docking, side):
    minimum = docking.window.minimumSizeHint().height()
    rail, members = populate(docking, side, 6)
    assert docking.window.minimumSizeHint().height() <= minimum
    docking.window.resize(800, 400)
    QApplication.processEvents()
    assert docking.window.height() == 400
    counts = [sum(rail._buttons[tool_id].isVisible() for tool_id in ids
                  if tool_id in rail._buttons) for ids in members.values()]
    assert min(counts) >= 1
    assert max(counts) - min(counts) <= 1
    active = members[POSITIONS[side][2]][-1]
    docking.manager.mark_active(active)
    assert rail._buttons[active].isVisible()
    assert docking.calls == {}
    assert_no_overlap(rail)
    docking.window.resize(800, 1100)
    QApplication.processEvents()
    assert all(rail._buttons[tool_id].isVisible() for ids in members.values() for tool_id in ids)


@pytest.mark.parametrize("height", [1, 20, 28, 35, 90, 140, 180])
def test_tiny_rail_keeps_more_inside_without_overlapping_buttons(docking, height):
    rail, _ = populate(docking, "left", 5)
    rail.resize(rail.width(), height)
    assert rail.more.isVisible()
    assert_no_overlap(rail)
    assert rail.drop_target(rail.more.geometry().center()) is None


@pytest.mark.parametrize("side", POSITIONS)
def test_more_menu_lists_grouped_overflow_then_every_other_tool_once(docking, side):
    rail, members = populate(docking, side, 6)
    docking.window.resize(800, 300)
    QApplication.processEvents()
    menu = rail._build_more_menu()
    try:
        actions = [action for action in menu.actions() if action.data() is not None]
        ids = [action.data() for action in actions]
        expected = [tool_id for group in members.values() for tool_id in group]
        assert len(ids) == len(set(ids)) == len(expected)
        assert set(ids) == set(expected)
        overflow = [tool_id for group in members.values() for tool_id in group
                    if tool_id not in rail._layout_data.buttons]
        assert ids[:len(overflow)] == overflow
        actions[0].trigger()
        assert docking.manager.active_id == ids[0]
        assert rail._buttons[ids[0]].isVisible()
    finally:
        menu.deleteLater()


@pytest.mark.parametrize("height", [76, 83, 84, 90, 140, 175])
@pytest.mark.parametrize("group_index", [0, 1, 2])
def test_tiny_drag_placeholders_do_not_evict_active_or_group_representatives(docking, height, group_index):
    """临时占位只能消耗真实入口之外的余量，拖动本身不能挤掉活动工具。"""
    rail, members = populate(docking, "left", 5)
    active = members[POSITIONS["left"][group_index]][-1]
    docking.manager.mark_active(active)
    rail.resize(rail.width(), height)
    # 普通模式至少需要 40 工具入口 + 40 更多入口 + 4 底部留白；更小时由更多访问。
    fits_active = height >= 84
    assert rail._buttons[active].isVisible() == fits_active
    representatives = {ids[0] for ids in rail._layout_data.visible.values() if ids}
    # 用 Qt 进入事件暴露临时组；MIME 的 Python 强引用保留到离开事件处理完毕。
    mime = enter_tool(rail, active)
    assert rail._buttons[active].isVisible() == fits_active
    assert representatives <= set(rail._layout_data.buttons)
    assert len(rail._layout_data.buttons) >= (2 if height >= 140 else int(fits_active))
    assert_no_overlap(rail)
    for rect in rail._layout_data.groups.values():
        assert rect.isEmpty() or rail.rect().contains(rect)
    rail.grab()  # 覆盖很薄的临时占位绘制，防止负尺寸边框越过分组边界。
    rail.dragLeaveEvent(QDragLeaveEvent())
    assert rail._buttons[active].isVisible() == fits_active
    assert mime.hasFormat("application/x-mindmap-dock-v1")


@pytest.mark.parametrize("side", POSITIONS)
@pytest.mark.parametrize("group_index", [0, 1, 2])
def test_empty_groups_accept_tool_drop_and_clear_temporary_geometry(docking, side, group_index):
    rail, members = populate(docking, side)
    position = POSITIONS[side][group_index]
    docking.manager.set_available(members[position][0], False)
    # 在另一侧注册来源，保持被测组为空；内容实例必须在迁移后保留。
    source_position = POSITIONS["right" if side == "left" else "left"][0]
    docking.register("alpha", source_position)
    docking.manager.activate("alpha", focus=False)
    content = docking.manager.panel("alpha").content
    content.setPlainText("拖放中保留业务内容")
    before_drag = docking.manager.capture_layout()
    mime = enter_tool(rail, "alpha")
    point = rail._layout_data.groups[position].center()
    move = QDragMoveEvent(point, Qt.DropAction.MoveAction, mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    frozen = rail._layout_data
    rail.dragMoveEvent(move)
    assert move.isAccepted()
    assert rail._layout_data is frozen
    assert rail.drop_target(point).position == position
    assert docking.manager.capture_layout() == before_drag
    event = drop_tool(rail, mime, point)
    assert event.isAccepted()
    assert docking.manager.states["alpha"].position == position
    assert docking.manager.panel("alpha").content is content
    assert content.toPlainText() == "拖放中保留业务内容"
    assert docking.calls["alpha"] == 1
    assert rail._drag_tool_id is None
    assert rail._insert_line is None


@pytest.mark.parametrize("side", POSITIONS)
def test_separator_halves_and_blank_midpoint_resolve_to_adjacent_groups(docking, side):
    rail, members = populate(docking, side)
    mime = enter_tool(rail, members[POSITIONS[side][0]][0])
    separator = rail._layout_data.separator
    x = rail.width() // 2
    top = rail.drop_target(QPoint(x, separator.top()))
    lower = rail.drop_target(QPoint(x, separator.bottom()))
    assert (top.position, top.index) == (POSITIONS[side][0], 1)
    assert (lower.position, lower.index) == (POSITIONS[side][1], 0)
    lower_rect, bottom_rect = [rail._layout_data.groups[p] for p in POSITIONS[side][1:]]
    midpoint = (lower_rect.bottom() + 1 + bottom_rect.top()) // 2
    assert rail.drop_target(QPoint(x, midpoint - 1)).position == POSITIONS[side][1]
    assert rail.drop_target(QPoint(x, midpoint)).position == POSITIONS[side][2]
    assert not drop_tool(rail, mime, rail.more.geometry().center()).isAccepted()


def test_drop_order_uses_complete_group_with_overflow_and_disabled_members(docking):
    rail, members = populate(docking, "left", 6)
    docking.window.resize(800, 220)
    top = members[DockPosition.LEFT_TOP]
    source = members[DockPosition.LEFT_BOTTOM][0]
    docking.manager.set_available(top[2], False)
    docking.manager.mark_active(top[-1])
    QApplication.processEvents()
    anchor = rail._buttons[top[-1]]
    point = anchor.pos() + QPoint(anchor.width() // 2, anchor.height() // 4)
    assert rail.drop_target(point).index == 5
    mime = docking.manager.encode_drag("tool", source)
    assert drop_tool(rail, mime, point).isAccepted()
    peers = sorted((s for s in docking.manager.states.values() if s.position == DockPosition.LEFT_TOP),
                   key=lambda state: state.order)
    assert [s.id for s in peers] == top[:-1] + [source, top[-1]]
    # 同组从前向后移动时再扣除来源，隐藏成员仍保持在原相对顺序中。
    docking.manager.mark_active(top[-1])
    point = rail._buttons[top[-1]].pos() + QPoint(15, anchor.height() * 3 // 4)
    mime = docking.manager.encode_drag("tool", top[0])
    assert drop_tool(rail, mime, point).isAccepted()
    peers = sorted((s for s in docking.manager.states.values() if s.position == DockPosition.LEFT_TOP),
                   key=lambda state: state.order)
    assert [s.id for s in peers] == top[1:-1] + [source, top[-1], top[0]]


def test_drag_rejects_document_foreign_session_and_unavailable_tool(docking, system_factory):
    rail = docking.host.left_rail
    foreign = system_factory()
    docking.manager.set_available("peer", False)
    for mime in [docking.manager.encode_drag("document", "doc"),
                 foreign.manager.encode_drag("tool", "alpha"),
                 docking.manager.encode_drag("tool", "peer")]:
        before = docking.manager.capture_layout()
        event = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.MoveAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        rail.dragEnterEvent(event)
        assert not event.isAccepted()
        assert not drop_tool(rail, mime, QPoint(10, 10)).isAccepted()
        assert docking.manager.capture_layout() == before


def test_leave_resize_and_native_cancel_clear_hints_without_committing(docking, monkeypatch):
    rail = docking.host.right_rail
    original = docking.manager.capture_layout()
    mime = enter_tool(rail, "alpha")
    assert all(not rect.isEmpty() for rect in rail._layout_data.groups.values())
    before = rail._layout_data
    rail.resize(rail.width(), rail.height() - 50)
    assert rail._layout_data is not before
    assert_no_overlap(rail)
    rail.dragLeaveEvent(QDragLeaveEvent())
    assert rail._layout_data.separator.isEmpty()
    assert all(rect.isEmpty() for rect in rail._layout_data.groups.values())
    assert docking.manager.capture_layout() == original

    class CancelDrag:
        """模拟平台取消且未发出离开事件，验证管理器 finally 的清理职责。"""

        def __init__(self, source):
            pass

        def setMimeData(self, mime):
            pass

        def setPixmap(self, pixmap):
            pass

        def setHotSpot(self, point):
            pass

        def exec(self, action):
            rail.dragEnterEvent(QDragEnterEvent(QPoint(10, 10), action, mime,
                                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            return Qt.DropAction.IgnoreAction

        def deleteLater(self):
            pass

    monkeypatch.setattr("mindmap_docking.manager.QDrag", CancelDrag)
    docking.manager.start_tool_drag("alpha", docking.host.left_rail._buttons["alpha"], QPoint())
    assert rail._drag_tool_id is None
    assert rail._insert_line is None
    assert rail._layout_data.separator.isEmpty()
    assert docking.manager.capture_layout() == original


def test_editor_return_region_move_and_restore_keep_content_and_group(docking):
    manager = docking.manager
    manager.activate("beta", focus=False)
    content = manager.panel("beta").content
    manager.move("beta", DockTarget("editor"))
    rail = docking.host.left_rail
    assert rail._buttons["beta"].isVisible()
    assert not rail._layout_data.separator.isEmpty()
    snapshot = manager.capture_layout()
    manager.move("beta", DockTarget(DockPosition.RIGHT_BOTTOM))
    assert not rail._buttons["beta"].isVisible()
    assert rail._layout_data.separator.isEmpty()
    assert docking.host.right_rail._layout_data.members[DockPosition.RIGHT_BOTTOM] == ("beta",)
    manager.restore_layout(snapshot)
    assert rail._buttons["beta"].isVisible()
    assert not rail._layout_data.separator.isEmpty()
    assert manager.panel("beta").content is content
    assert docking.calls["beta"] == 1
