"""通用化边界：宿主兼容、关闭决策、延迟插件与可替换基础设施。"""

import copy

import pytest
from mindmap_docking import (
    DockManager,
    DockPosition,
    DockTarget,
    DocumentSpec,
    Theme,
    ThemeTokens,
    ToolWindowSpec,
    ViewMode,
)
from mindmap_shell import ActionSpec, AppSpec, WorkbenchWindow
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit


def test_manager_does_not_modify_application_style_palette_or_font(qapp, tmp_path):
    original = (qapp.styleSheet(), qapp.palette(), qapp.font())
    outside = QLabel("宿主原有控件")
    outside_style = (outside.palette(), outside.font())
    manager = DockManager(storage_dir=tmp_path)
    manager.set_theme("dark")
    manager.set_option("compact", True)
    assert (qapp.styleSheet(), qapp.palette(), qapp.font()) == original
    assert (outside.palette(), outside.font()) == outside_style
    with pytest.raises(TypeError):
        manager.theme.apply(qapp)
    manager.shutdown()
    outside.deleteLater()


def test_custom_tokens_and_icons_survive_theme_compact_and_restore(docking):
    class Icons:
        def __init__(self):
            self.requests = []

        def get_icon(self, name, *, color, size):
            from PySide6.QtGui import QIcon
            self.requests.append((name, color, size))
            return QIcon()

    icons = Icons()
    manager = docking.manager
    manager.set_theme(Theme(tokens=ThemeTokens(light={"control-brand-border": "#CC5500"},
                                              dark={"control-brand-border": "#FF7700"},
                                              metrics={"gap": 11}), icons=icons))
    snapshot = manager.capture_layout()
    manager.set_theme("dark")
    manager.set_option("compact", True)
    assert manager.theme.icons is icons
    assert manager.theme.color("control-brand-border").name() == "#ff7700"
    manager.restore_layout(snapshot)
    assert manager.theme.color("control-brand-border").name() == "#cc5500"
    assert manager.theme.gap == 11
    assert manager.theme.icons is icons


def test_configuration_directories_are_isolated_by_application_id(qapp):
    one = DockManager(application_id="sample-one")
    two = DockManager(application_id="sample-two")
    assert one.storage_dir != two.storage_dir
    with pytest.raises(ValueError):
        DockManager(application_id="../outside")
    one.shutdown()
    two.shutdown()


def test_factory_failure_keeps_layout_and_allows_retry(docking):
    attempts = []
    def create():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("业务初始化失败")
        return QLabel("重试成功")
    manager = docking.manager
    manager.register_tool_window(ToolWindowSpec("retry", "可重试工具", create))
    before = manager.capture_layout()
    errors = []
    manager.tool_failed.connect(lambda *args: errors.append(args))
    with pytest.raises(RuntimeError):
        manager.activate("retry")
    assert manager.capture_layout() == before
    assert errors == [("retry", "业务初始化失败")]
    assert manager.activate("retry", focus=False)
    assert manager.panel("retry").content.text() == "重试成功"


def test_document_close_can_cancel_then_reopen_same_instance(docking):
    allowed, notifications = [False], []
    manager = docking.manager
    widget = manager.register_document(DocumentSpec("business", "业务表单", lambda: QPlainTextEdit("已有数据"),
                                                    can_close=lambda: allowed[0],
                                                    on_closed=lambda: notifications.append(True)))
    location = docking.host.workspace.find_token("business")
    location[0].close_token("business")
    assert docking.host.workspace.find_token("business") is not None
    allowed[0] = True
    assert manager.close_document("business")
    assert docking.host.workspace.find_token("business") is None
    manager.open_document("business")
    assert manager.documents["business"]["widget"] is widget
    assert widget.toPlainText() == "已有数据"
    assert notifications == [True]


def test_modified_document_without_decision_prevents_close(docking):
    manager = docking.manager
    manager.set_document_modified("doc")
    assert not manager.close_document("doc")
    assert not manager.can_close_documents()
    manager.set_document_modified("doc", False)
    assert manager.close_document("doc", dispose=True)
    assert "doc" not in manager.documents


def test_unregister_releases_content_and_restores_plugin_position(docking):
    manager = docking.manager
    manager.move("alpha", DockTarget(DockPosition.RIGHT_BOTTOM))
    manager.set_view_mode("alpha", ViewMode.UNDOCK)
    content = manager.panel("alpha").content
    released = manager.unregister_tool_window("alpha", dispose=False)
    assert released is content and released.parentWidget() is None
    assert "alpha" not in manager.states
    assert "tool:alpha" not in manager.actions
    manager.register_tool_window(ToolWindowSpec("alpha", "重新加载", lambda: released, shortcut="Alt+1"))
    assert manager.states["alpha"].position == DockPosition.RIGHT_BOTTOM
    assert manager.states["alpha"].mode == ViewMode.UNDOCK
    assert manager.panel("alpha").content is content


def test_missing_plugin_layout_is_preserved_until_registered(docking):
    manager = docking.manager
    snapshot = manager.capture_layout()
    unknown = copy.deepcopy(snapshot["tools"][0])
    unknown.update(id="later", position="right_bottom", visible=False)
    snapshot["tools"].append(unknown)
    manager.restore_layout(snapshot)
    assert any(item["id"] == "later" for item in manager.capture_layout()["tools"])
    manager.register_tool_window(ToolWindowSpec("later", "稍后加载", lambda: QLabel("插件")))
    assert manager.states["later"].position == DockPosition.RIGHT_BOTTOM




def test_shortcut_conflict_is_rejected_before_registration(docking):
    manager = docking.manager
    with pytest.raises(ValueError):
        manager.register_tool_window(ToolWindowSpec("conflict", "冲突", lambda: QLabel(), shortcut="Alt+1"))
    assert "conflict" not in manager.states
    manager.register_action("business.save", "保存", lambda: None, shortcut="Ctrl+S")
    with pytest.raises(ValueError):
        manager.set_shortcut("business.save", "Alt+1")
    assert manager.actions["business.save"].shortcut().toString() == "Ctrl+S"
    manager.set_shortcut("business.save", "")
    assert manager.actions["business.save"].shortcut().isEmpty()


def test_layout_store_can_be_replaced(docking):
    class Store:
        value = None
        def save(self, payload):
            self.value = copy.deepcopy(payload)
        def load(self):
            return copy.deepcopy(self.value)
    store = Store()
    manager = docking.manager
    manager.layout_store = store
    manager.activate("alpha", focus=False)
    manager.save_session()
    manager.hide("alpha")
    assert manager.load_session()
    assert manager.states["alpha"].visible


def test_workbench_keeps_brand_and_shared_business_actions(qapp, qtbot, tmp_path):
    calls = []
    window = WorkbenchWindow(AppSpec("custom-app", "定制业务软件", storage_dir=tmp_path,
                                    actions=(ActionSpec("business.add", "新增", lambda: calls.append(1),
                                                        icon="document", toolbar=True),)))
    qtbot.addWidget(window)
    window.show()
    assert window.spec.title == "定制业务软件"
    window.add_action(ActionSpec("business.late", "稍后加入", lambda: calls.append(2), toolbar=True))
    assert any(item.text() == "稍后加入" for item in window.title_bar.action_buttons)
    # 动态业务动作仍在窗口控制按钮之前，且复用菜单中的同一 QAction。
    bar = window.title_bar
    assert bar.box.itemAt(bar.box.count() - 1).layout() is bar.window_controls
    assert all(bar.box.indexOf(button) < bar.box.count() - 1 for button in bar.action_buttons)
    window.action("business.add").trigger()
    assert calls == [1]
    window.manager.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_two_managers_do_not_claim_each_others_tool_focus(system_factory):
    one, two = system_factory(), system_factory()
    one.manager.activate("alpha", focus=False)
    two.manager.activate("alpha", focus=False)
    two.window.activateWindow()
    two.manager.panel("alpha").content.setFocus()
    QApplication.processEvents()
    one.manager.active_id = ""
    one.manager._process_focus()
    assert one.manager.active_id == ""


@pytest.mark.parametrize("operation", ["move", "restore"])
def test_factory_preflight_prevents_partial_move_or_restore(docking, operation):
    def fail():
        raise RuntimeError("暂时不可用")
    manager = docking.manager
    manager.register_tool_window(ToolWindowSpec("bad", "失败工具", fail))
    before = manager.capture_layout()
    with pytest.raises(RuntimeError):
        if operation == "move":
            manager.move("bad", DockTarget(DockPosition.RIGHT_TOP))
        else:
            snapshot = copy.deepcopy(before)
            next(item for item in snapshot["tools"] if item["id"] == "bad")["visible"] = True
            manager.restore_layout(snapshot)
    assert manager.capture_layout() == before


def test_close_notification_failure_does_not_undo_closed_state(docking):
    def fail():
        raise RuntimeError("业务通知错误")
    manager = docking.manager
    manager.add_document("notify", "关闭通知", QLabel("页面"), on_closed=fail)
    assert manager.close_document("notify")
    assert manager.documents["notify"]["closed"]
    assert docking.host.workspace.find_token("notify") is None


def test_native_cancel_with_mouse_held_never_detaches_outside(docking, monkeypatch):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QCursor, QDrag
    manager = docking.manager
    manager.activate("alpha", focus=False)
    before = manager.capture_layout()
    monkeypatch.setattr(QDrag, "exec", lambda *args: Qt.DropAction.IgnoreAction)
    monkeypatch.setattr(QCursor, "pos", lambda: QPoint(-99999, -99999))
    monkeypatch.setattr(QApplication, "mouseButtons", lambda: Qt.MouseButton.LeftButton)
    manager.start_tool_drag("alpha", manager.panel("alpha").header, QPoint(4, 4))
    assert manager.capture_layout() == before


@pytest.mark.parametrize("name", ["light", "dark"])
def test_scrolling_document_uses_its_local_theme_surface(docking, name):
    from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget
    page = QWidget()
    QVBoxLayout(page).addWidget(QLabel("业务滚动内容"))
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(page)
    manager = docking.manager
    manager.add_document("scrolling", "滚动文档", scroll)
    manager.set_theme(name)
    QApplication.processEvents()
    assert page.grab().toImage().pixelColor(2, 2) == manager.theme.color("tool-window-bg")
