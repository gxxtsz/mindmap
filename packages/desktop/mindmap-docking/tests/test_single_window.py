"""单主窗口边界：移除入口、旧会话合并和拖出取消均使用隔离配置验证。"""

import copy

import pytest
from mindmap_docking import DockHost, DockPosition, DockTarget, ToolWindowSpec, ViewMode
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QPlainTextEdit


def tab_nodes(node):
    """按快照中的深度优先顺序枚举标签组，不依赖运行时容器实现。"""
    if node["kind"] == "tabs":
        yield node
    else:
        for child in node["children"]:
            yield from tab_nodes(child)


def legacy_snapshot(system):
    """构造包含分屏、重复标签、浮窗及延迟插件的真实版本 1 历史会话。"""
    manager, host = system.manager, system.host
    manager.add_document("second", "第二份", QPlainTextEdit("第二份内容"))
    manager.add_document("third", "第三份", QPlainTextEdit("第三份内容"))
    host.editor.split_token("second", Qt.Orientation.Horizontal)
    snapshot = manager.capture_layout()
    main = snapshot["hosts"]["main"]
    groups = list(tab_nodes(main["editor"]))
    first, target = groups
    first.update(tokens=["doc"], current=0)
    target.update(tokens=["second"], current=0)
    main["current_group"] = target["id"]
    auxiliary = copy.deepcopy(main)
    auxiliary["geometry"] = [999999, -999999, 1500, 900]
    auxiliary["editor"] = {"kind": "tabs", "id": "aux-tabs",
                           "tokens": ["second", "third", "tool:beta", "doc"], "current": 1}
    snapshot["hosts"]["old-aux"] = auxiliary
    snapshot["hosts"]["old-empty"] = copy.deepcopy(auxiliary)
    snapshot["hosts"]["old-empty"]["editor"]["tokens"] = []
    for item in snapshot["tools"]:
        item["geometry"] = [200, 160, 620, 420]
        if item["id"] == "alpha":
            item.update(host_id="old-aux", mode="float", visible=True,
                        position="left_top", last_position="right_bottom")
        elif item["id"] == "beta":
            item.update(host_id="old-aux", mode="window", visible=True,
                        position="editor", editor_group="aux-tabs")
        elif item["id"] == "gamma":
            item.update(host_id="old-empty", mode="window", visible=False)
    unknown = copy.deepcopy(snapshot["tools"][0])
    unknown.update(id="later", host_id="old-aux", mode="window", visible=False,
                   position="right_top", last_position="right_top")
    snapshot["tools"].append(unknown)
    snapshot["active"] = ""
    return snapshot


def test_legacy_hosts_merge_without_losing_selection_instances_or_hidden_state(docking):
    """保留主分屏及当前标签，辅助标签追加去重，浮窗和未注册工具统一迁回。"""
    manager = docking.manager
    legacy = legacy_snapshot(docking)
    original = copy.deepcopy(legacy)
    documents = {key: doc["widget"] for key, doc in manager.documents.items()}
    manager.restore_layout(legacy)
    assert legacy == original
    assert set(manager.hosts) == {"main"}
    groups = list(tab_nodes(manager.capture_layout()["hosts"]["main"]["editor"]))
    assert [group["tokens"] for group in groups] == [["doc"], ["second", "third", "tool:beta"]]
    assert [group["current"] for group in groups] == [0, 0]
    assert manager.states["alpha"].position == DockPosition.RIGHT_BOTTOM
    assert manager.states["alpha"].mode == ViewMode.DOCK_PINNED
    assert manager.states["alpha"].visible
    assert manager.states["beta"].editor_group == groups[1]["id"]
    assert not manager.states["gamma"].visible
    assert all(state.host_id == "main" for state in manager.states.values())
    for key, widget in documents.items():
        assert manager.documents[key]["widget"] is widget
        assert manager.documents[key]["host_id"] == "main"
    saved = manager.capture_layout()
    assert all("geometry" not in item and item["mode"] not in {"float", "window"}
               for item in saved["tools"])
    manager.register_tool_window(ToolWindowSpec("later", "延迟工具", lambda: QPlainTextEdit("保留")))
    assert manager.states["later"].host_id == "main"
    assert manager.states["later"].mode == ViewMode.DOCK_PINNED
    assert manager.states["later"].position == DockPosition.RIGHT_TOP
    assert not manager.states["later"].visible
    assert docking.calls["alpha"] == docking.calls["beta"] == 1


def test_legacy_session_is_cleaned_on_save_and_restores_after_restart(system_factory, tmp_path):
    """自动加载完成迁移，正常保存自然清除额外宿主和浮窗字段，重启不会复活旧能力。"""
    directory = tmp_path / "历史多窗口"
    first = system_factory(directory)
    snapshot = legacy_snapshot(first)
    first.manager.shutdown()
    first.manager.layout_store.save({"schema_version": 1, "session": snapshot})
    second = system_factory(directory)
    assert second.manager.load_session()
    second.manager.save_session()
    saved = second.manager.layout_store.load()
    assert set(saved) == {"schema_version", "session"}
    assert set(saved["session"]["hosts"]) == {"main"}
    assert all(item["host_id"] == "main" and "geometry" not in item
               for item in saved["session"]["tools"])
    second.manager.shutdown()
    third = system_factory(directory)
    assert third.manager.load_session()
    assert set(third.manager.hosts) == {"main"}
    assert third.manager.states["alpha"].position == DockPosition.RIGHT_BOTTOM
    assert not third.manager.states["gamma"].visible


def test_legacy_visible_panels_keep_visibility_when_dock_slots_are_full(docking):
    """合并多窗口时保留原可见面板；六个位置用满后使用主窗口编辑器标签。"""
    manager = docking.manager
    identifiers = ["legacy-" + str(index) for index in range(7)]
    for identifier in identifiers:
        manager.register_tool_window(ToolWindowSpec(identifier, identifier, lambda: QPlainTextEdit("内容")))
    snapshot = manager.capture_layout()
    for item in snapshot["tools"]:
        if item["id"] in identifiers:
            item.update(mode="float", visible=True)
    manager.restore_layout(snapshot)
    assert all(manager.states[identifier].visible for identifier in identifiers)
    positions = [manager.states[identifier].position for identifier in identifiers]
    assert set(positions) == {position.value for position in DockPosition} | {"editor"}
    # 标签可保留打开状态而不抢走当前文档；非当前标签的 QWidget 不应强制显示。
    for identifier in identifiers:
        if manager.states[identifier].position == "editor":
            assert docking.host.editor.find_token("tool:" + identifier) is not None
        else:
            assert manager.panel(identifier).isVisible()
    assert set(manager.hosts) == {"main"}


@pytest.mark.parametrize("damage", ["mode", "geometry", "host", "editor"])
def test_invalid_legacy_snapshot_does_not_change_layout_or_create_content(docking, damage):
    """兼容读取不会放宽其他校验；预检失败前既不移动控件也不调用延迟工厂。"""
    manager = docking.manager
    invalid = manager.capture_layout()
    invalid["tools"][0]["mode"] = "float"
    if damage == "mode":
        invalid["tools"][0]["mode"] = "unknown"
    elif damage == "geometry":
        invalid["tools"][0]["geometry"] = [1, 2, -3, 4]
    elif damage == "host":
        invalid["tools"][0]["host_id"] = "missing"
    else:
        invalid["hosts"]["main"]["editor"] = {"kind": "unknown"}
    before = manager.capture_layout()
    with pytest.raises(ValueError):
        manager.restore_layout(invalid)
    assert manager.capture_layout() == before
    assert docking.calls == {}


def test_removed_runtime_interfaces_and_modes_are_rejected(docking):
    """废弃能力无兼容空接口；只在历史快照读取层识别旧模式字符串。"""
    manager = docking.manager
    before = manager.capture_layout()
    for name in ("create_frame", "detach_token", "close_frame", "frame_factory", "frames", "floating",
                 "toggle_maximize", "hide_restore_all", "hide_active", "focus_editor", "focus_workspace",
                 "focus_last_tool", "last_active_id", "maximized_id", "place_floating"):
        assert not hasattr(manager, name)
    assert not ({"hide_active", "hide_all", "editor_focus", "last_tool", "maximize"} & manager.actions.keys())
    for mode in ("float", "window"):
        with pytest.raises(ValueError):
            manager.set_view_mode("alpha", mode)
        with pytest.raises(ValueError):
            ToolWindowSpec("old", "旧模式", lambda: QPlainTextEdit(), mode=mode)
    with pytest.raises(ValueError):
        DockTarget("editor", "other")
    with pytest.raises(ValueError):
        DockHost(manager, "other")
    assert manager.capture_layout() == before


def test_panel_menus_and_double_click_have_no_removed_actions(docking, qtbot):
    """标题和文档标签双击不再改变布局；面板右键只提供主窗口内操作。"""
    manager = docking.manager
    manager.activate("alpha", focus=False)
    menu = manager.tool_menu("alpha")

    def labels(menu):
        for action in menu.actions():
            yield action.text()
            if action.menu():
                yield from labels(action.menu())

    assert not ({"最大化 / 恢复", "浮动", "窗口", "新框架"} & set(labels(menu)))
    before = manager.capture_layout()
    qtbot.mouseDClick(manager.panel("alpha").header, Qt.MouseButton.LeftButton)
    group, index = docking.host.editor.find_token("doc")
    qtbot.mouseDClick(group.tabBar(), Qt.MouseButton.LeftButton, pos=group.tabBar().tabRect(index).center())
    assert manager.capture_layout() == before
    assert docking.host.editor.isVisible()
    menu.deleteLater()


@pytest.mark.parametrize("kind", ["tool", "document"])
def test_drag_outside_window_or_cancel_keeps_original_layout(docking, monkeypatch, kind):
    """驱动真实拖动入口的 IgnoreAction 返回路径，覆盖工具和文档且不创建新窗口。"""
    from mindmap_docking import manager as manager_module

    manager = docking.manager
    manager.activate("alpha", focus=False)
    before = copy.deepcopy(manager.capture_layout())
    windows = set(QApplication.topLevelWidgets())

    class IgnoredDrag:
        """模拟平台拖动循环返回；状态只能由合法落点提交，不能由释放位置推断。"""

        def __init__(self, source):
            self.source = source

        def setMimeData(self, mime):
            assert manager.decode_drag(mime)["kind"] == kind

        def setPixmap(self, pixmap):
            pass

        def setHotSpot(self, hotspot):
            pass

        def exec(self, action):
            return Qt.DropAction.IgnoreAction

        def deleteLater(self):
            pass

    monkeypatch.setattr(manager_module, "QDrag", IgnoredDrag)
    if kind == "tool":
        manager.start_tool_drag("alpha", manager.panel("alpha").header, QPoint(5, 5))
    else:
        group, _ = docking.host.editor.find_token("doc")
        manager.start_document_drag("doc", group.tabBar(), QPoint(5, 5))
    assert manager.capture_layout() == before
    assert set(QApplication.topLevelWidgets()) == windows
    assert not manager._dragging
