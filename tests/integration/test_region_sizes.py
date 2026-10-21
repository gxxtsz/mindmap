"""区域共享尺寸：验证真实 Qt 几何、用户调整、迁移及旧布局兼容。"""

import copy
import json

import pytest
from mindmap_docking import DockPosition, DockTarget, ViewMode
from mindmap_shell import AppSpec, WorkbenchWindow
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

REGIONS = {
    "left": (DockPosition.LEFT_TOP, DockPosition.LEFT_BOTTOM),
    "right": (DockPosition.RIGHT_TOP, DockPosition.RIGHT_BOTTOM),
    "bottom": (DockPosition.BOTTOM_LEFT, DockPosition.BOTTOM_RIGHT),
}


def settle():
    """等 Qt 父子布局和合并的延迟布置完成，不模拟真实 Windows 输入。"""
    QApplication.processEvents()
    QApplication.processEvents()


def dimension(host, region):
    widget = getattr(host, region)
    return widget.height() if region == "bottom" else widget.width()


def drag_region(host, region, amount):
    """调整实际 QSplitter 并走对应信号处理器，等价于一次已完成的用户尺寸修改。"""
    settle()
    name = "vertical" if region == "bottom" else "horizontal"
    splitter = getattr(host, name)
    index = {"left": 0, "right": 2, "bottom": 1}[region]
    center = 0 if region == "bottom" else 1
    values = splitter.sizes()
    values[center] += values[index] - amount
    values[index] = amount
    splitter.setSizes(values)
    host._splitter_moved(name, 2 if region == "right" else 1)
    settle()
    assert abs(dimension(host, region) - amount) <= 1


def register_group(docking, region):
    top, lower = REGIONS[region]
    for tool_id, position in [("first", top), ("second", top), ("lower", lower)]:
        docking.register(tool_id, position)
    docking.manager.activate("first", focus=False)
    settle()


@pytest.mark.parametrize("region", REGIONS)
def test_switch_first_open_other_group_and_hidden_reopen_keep_latest_size(docking, region):
    manager, host = docking.manager, docking.host
    register_group(docking, region)
    drag_region(host, region, 220)
    manager.activate("second", focus=False)
    settle()
    assert dimension(host, region) == 220
    drag_region(host, region, 380)
    for tool_id in ["first", "second", "lower", "first"]:
        manager.activate(tool_id, focus=False)
        settle()
        assert dimension(host, region) == 380
    content = manager.panel("first").content
    for tool_id in ["first", "second", "lower"]:
        manager.hide(tool_id)
    other_region = "bottom" if region != "bottom" else "left"
    docking.register("other", REGIONS[other_region][0])
    manager.activate("other", focus=False)
    drag_region(host, other_region, 180)
    manager.activate("first", focus=False)
    settle()
    assert dimension(host, region) == 380
    assert manager.panel("first").content is content
    assert docking.calls["first"] == 1


def test_resizing_one_region_does_not_overwrite_other_or_hidden_regions(docking):
    manager, host = docking.manager, docking.host
    for region in REGIONS:
        docking.register(region, REGIONS[region][0])
        manager.activate(region, focus=False)
    for region, amount in [("left", 220), ("right", 240), ("bottom", 190)]:
        drag_region(host, region, amount)
    before = copy.deepcopy(host.region_sizes)
    drag_region(host, "left", 310)
    assert host.region_sizes["dock"]["right"] == before["dock"]["right"]
    assert host.region_sizes["dock"]["bottom"] == before["dock"]["bottom"]
    manager.hide("right")
    manager.hide("bottom")
    drag_region(host, "left", 350)
    manager.activate("right", focus=False)
    manager.activate("bottom", focus=False)
    settle()
    assert dimension(host, "right") == 240
    assert dimension(host, "bottom") == 190


def test_window_compression_does_not_change_remembered_targets(docking):
    manager, host = docking.manager, docking.host
    docking.window.resize(1450, 950)
    for region, amount in [("left", 380), ("right", 350), ("bottom", 300)]:
        docking.register(region, REGIONS[region][0])
        manager.activate(region, focus=False)
        drag_region(host, region, amount)
    before = copy.deepcopy(host.region_sizes)
    docking.window.resize(760, 420)
    settle()
    assert dimension(host, "left") < 380 or dimension(host, "right") < 350
    assert host.region_sizes == before
    manager.activate("left", focus=False)
    assert host.region_sizes == before
    docking.window.resize(1450, 950)
    settle()
    assert [dimension(host, p) for p in REGIONS] == [380, 350, 300]
    assert host.region_sizes == before


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("wide", [False, True])
def test_side_by_side_keeps_outer_width_and_internal_ratio_when_switching(docking, side, wide):
    manager, host = docking.manager, docking.host
    register_group(docking, side)
    manager.activate("lower", focus=False)
    manager.set_option("widescreen", wide)
    manager.set_option(side + "_side_by_side", True)
    drag_region(host, side, 390)
    inner = getattr(host, side)
    inner.setSizes([140, 242])
    host._splitter_moved(side, 1)
    ratio = list(host._sizes[side])
    manager.activate("second", focus=False)
    settle()
    assert dimension(host, side) == 390
    assert inner.sizes() == ratio
    manager.hide("lower")
    drag_region(host, side, 410)
    assert host._sizes[side] == ratio
    manager.activate("lower", focus=False)
    settle()
    assert dimension(host, side) == 410
    actual = inner.sizes()
    assert abs(actual[0] / sum(actual) - ratio[0] / sum(ratio)) < 0.01


@pytest.mark.parametrize("region", REGIONS)
def test_overlay_tools_share_separate_region_size_for_keyboard_and_edge_drag(docking, region):
    manager, host = docking.manager, docking.host
    register_group(docking, region)
    drag_region(host, region, 220)
    for tool_id in ["overlay_a", "overlay_b"]:
        docking.register(tool_id, REGIONS[region][0], mode=ViewMode.UNDOCK)
    manager.activate("overlay_a", focus=False)
    settle()
    assert dimension(host, region) == 220
    delta = {"left": (60, 0), "right": (-60, 0), "bottom": (0, -60)}[region]
    manager.resize_active(*delta)
    assert host.region_sizes["overlay"][region] == 360
    manager.activate("overlay_b", focus=False)
    settle()
    overlay = host.overlays["overlay_b"]
    assert (overlay.height() if region == "bottom" else overlay.width()) == 360
    point = (QPoint(overlay.width() - 2, 20) if region == "left" else
             QPoint(2, 20) if region == "right" else QPoint(20, 2))
    shift = {"left": QPoint(34, 0), "right": QPoint(-34, 0), "bottom": QPoint(0, -34)}[region]
    origin = overlay.mapToGlobal(point)
    overlay.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(point), QPointF(origin),
                                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                       Qt.KeyboardModifier.NoModifier))
    overlay.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(point + shift), QPointF(origin + shift),
                                      Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                      Qt.KeyboardModifier.NoModifier))
    overlay.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(point + shift),
                                         QPointF(origin + shift), Qt.MouseButton.LeftButton,
                                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert host.region_sizes["overlay"][region] == 394
    manager.activate("overlay_a", focus=False)
    settle()
    overlay = host.overlays["overlay_a"]
    assert (overlay.height() if region == "bottom" else overlay.width()) == 394
    assert dimension(host, region) == 220
    assert host.region_sizes["dock"][region] == 220


def test_cross_region_move_uses_destination_size_and_preserves_content(docking):
    manager, host = docking.manager, docking.host
    manager.activate("alpha", focus=False)
    drag_region(host, "left", 380)
    content = manager.panel("alpha").content
    manager.move("alpha", DockTarget(DockPosition.RIGHT_TOP))
    settle()
    assert host.right.width() == 285
    drag_region(host, "right", 250)
    manager.move("peer", DockTarget(DockPosition.RIGHT_TOP))
    settle()
    assert host.right.width() == 250
    manager.move("alpha", DockTarget(DockPosition.LEFT_TOP))
    settle()
    assert host.left.width() == 380
    assert host.region_sizes["dock"]["right"] == 250
    assert manager.panel("alpha").content is content
    assert docking.calls["alpha"] == 1


@pytest.mark.parametrize("region", REGIONS)
def test_overlay_shortcut_resizes_from_actual_geometry_after_compression(docking, region):
    manager, host = docking.manager, docking.host
    docking.register("overlay", REGIONS[region][0], mode=ViewMode.UNDOCK)
    manager.activate("overlay", focus=False)
    host._set_region_extent(REGIONS[region][0], 1500, overlay=True)
    settle()
    overlay = host.overlays["overlay"]
    actual = overlay.height() if region == "bottom" else overlay.width()
    assert actual < 1500
    manager.resize_active(*{"left": (-36, 0), "right": (36, 0), "bottom": (0, 36)}[region])
    assert host.region_sizes["overlay"][region] == actual - 36
    assert (overlay.height() if region == "bottom" else overlay.width()) == actual - 36



@pytest.mark.parametrize("old_option", [False, True])
@pytest.mark.parametrize("priority", ["active", "visible", "saved"])
def test_legacy_sizes_migrate_with_host_then_active_visible_and_saved_order_priority(docking, old_option, priority):
    manager = docking.manager
    docking.register("right", DockPosition.RIGHT_TOP)
    legacy = manager.capture_layout()
    legacy["settings"]["remember_size"] = old_option
    host_data = legacy["hosts"]["main"]
    host_data.pop("region_sizes")
    host_data["sizes"]["horizontal"] = [245, 650, 0]
    host_data["sizes"]["vertical"] = [0, 0]
    legacy["active"] = "alpha" if priority == "active" else ""
    for item in legacy["tools"]:
        item["extent"] = {"alpha": 410, "peer": 460, "beta": 470, "gamma": 315, "right": 530}[item["id"]]
        item["visible"] = priority != "saved" and item["id"] == "peer"
    manager.restore_layout(legacy)
    assert docking.host.region_sizes == {"dock": {"left": 245, "right": 530, "bottom": 315},
                                         "overlay": {"left": 460 if priority == "visible" else 410,
                                                     "right": 530, "bottom": 315}}
    normalized = manager.capture_layout()
    assert "remember_size" not in normalized["settings"]
    assert all("extent" not in item for item in normalized["tools"])


def test_partial_region_fields_and_missing_legacy_data_use_defined_defaults(docking):
    manager = docking.manager
    snapshot = manager.capture_layout()
    host = snapshot["hosts"]["main"]
    host.pop("sizes")
    host["region_sizes"] = {"dock": {"left": 345}, "overlay": {"bottom": 375}}
    manager.restore_layout(snapshot)
    assert docking.host.region_sizes == {"dock": {"left": 345, "right": 285, "bottom": 260},
                                         "overlay": {"left": 300, "right": 300, "bottom": 375}}


@pytest.mark.parametrize("invalid", [None, [], {"floating": {}}, {"dock": None}, {"dock": {"center": 300}},
                                     {"dock": {"left": 0}}, {"dock": {"right": -1}},
                                     {"overlay": {"bottom": 1000001}}, {"dock": {"left": True}},
                                     {"dock": {"left": "300"}}, {"dock": {"left": 300.5}}])
def test_invalid_region_sizes_are_rejected_before_mutating_layout(docking, invalid):
    manager = docking.manager
    before = manager.capture_layout()
    damaged = copy.deepcopy(before)
    damaged["hosts"]["main"]["region_sizes"] = invalid
    with pytest.raises(ValueError):
        manager.restore_layout(damaged)
    assert manager.capture_layout() == before
    assert docking.calls == {}


def test_legacy_session_and_pending_plugin_are_normalized_on_save(system_factory, tmp_path):
    directory = tmp_path / "旧版区域布局"
    first = system_factory(directory)
    legacy = first.manager.capture_layout()
    legacy["settings"]["remember_size"] = True
    legacy["hosts"]["main"].pop("region_sizes")
    unknown = copy.deepcopy(legacy["tools"][0])
    unknown.update(id="later", position="right_bottom", extent=470, visible=False)
    legacy["tools"].append(unknown)
    first.manager.shutdown()
    first.manager.layout_store.save({"schema_version": 1, "current_layout": "旧布局", "session": legacy,
                                     "layouts": {"旧布局": copy.deepcopy(legacy)}})
    second = system_factory(directory)
    assert second.manager.load_session()
    second.manager.save_session()
    payload = json.loads(second.manager.storage_file.read_text("utf-8"))
    assert set(payload) == {"schema_version", "session"}
    snapshot = payload["session"]
    assert "remember_size" not in snapshot["settings"]
    assert all("extent" not in item for item in snapshot["tools"])
    assert "region_sizes" in snapshot["hosts"]["main"]
    second.register("later", DockPosition.LEFT_TOP)
    assert second.manager.states["later"].position == DockPosition.RIGHT_BOTTOM
    assert second.host.region_sizes["overlay"]["right"] == 470


def test_shared_dock_and_overlay_sizes_round_trip_with_snapshot(docking):
    manager, host = docking.manager, docking.host
    manager.activate("alpha", focus=False)
    drag_region(host, "left", 380)
    host._set_region_extent(DockPosition.RIGHT_BOTTOM, 420, overlay=True)
    snapshot = manager.capture_layout()
    host._set_region_extent(DockPosition.LEFT_TOP, 230)
    host._set_region_extent(DockPosition.RIGHT_TOP, 270, overlay=True)
    manager.restore_layout(snapshot)
    settle()
    assert host.left.width() == 380
    assert host.region_sizes["overlay"]["right"] == 420


def test_new_session_keeps_shared_sizes_including_hidden_regions(system_factory, tmp_path):
    directory = tmp_path / "新区域布局"
    first = system_factory(directory)
    first.manager.activate("alpha", focus=False)
    drag_region(first.host, "left", 340)
    first.manager.activate("gamma", focus=False)
    drag_region(first.host, "bottom", 210)
    first.host._set_region_extent(DockPosition.RIGHT_TOP, 310)
    first.host._set_region_extent(DockPosition.BOTTOM_RIGHT, 420, overlay=True)
    expected = copy.deepcopy(first.host.region_sizes)
    first.manager.save_session()
    first.manager.shutdown()
    second = system_factory(directory)
    assert second.manager.load_session()
    settle()
    assert second.host.region_sizes == expected
    assert second.host.left.width() == 340
    assert second.host.bottom.height() == 210
    second.manager.activate("peer", focus=False)
    second.register("right", DockPosition.RIGHT_BOTTOM)
    second.manager.activate("right", focus=False)
    settle()
    assert second.host.left.width() == 340
    assert second.host.right.width() == 310
    assert second.host.region_sizes["overlay"]["bottom"] == 420


def test_removed_size_option_is_absent_and_rejected(docking, qtbot, tmp_path):
    manager = docking.manager
    before = manager.capture_layout()
    with pytest.raises(KeyError):
        manager.set_option("remember_size", True)
    assert manager.capture_layout() == before
    assert not hasattr(manager.settings, "remember_size")
    assert all(not hasattr(state, "extent") for state in manager.states.values())
    window = WorkbenchWindow(AppSpec("region-menu", "尺寸菜单", storage_dir=tmp_path / "菜单"))
    qtbot.addWidget(window)
    try:
        menu = window.appearance_menu()
        assert all("记住" not in action.text() for action in menu.actions())
        menu.deleteLater()
    finally:
        window.manager.shutdown()
