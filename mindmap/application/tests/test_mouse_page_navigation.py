"""用主窗口 Qt 鼠标事件验证标签内的前进与后退。"""

import pytest
from mindmap_app.app import build_window
from mindmap_core import NodeGraph
from mindmap_core.node_pages import extension_page_id, graph_page_id
from mindmap_qt_editor.node_pages import NodePageProvider, NodePageWidget
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu

BACK = Qt.MouseButton.BackButton
FORWARD = Qt.MouseButton.ForwardButton
MAIN = (None, None)


@pytest.fixture
def open_window(qtbot, tmp_path):
    def create(*, restore=False):
        window = build_window(storage_dir=tmp_path / "配置", restore=restore)
        qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
        window.resize(1200, 800)
        window.show()
        window.activateWindow()
        QApplication.processEvents()
        return window

    return create


def new_document(window, path):
    assert window.new_document(path)
    QApplication.processEvents()
    return window.application.active_session


def add_submap(session, title):
    owner = session.engine.add_child(session.engine.root_id, title)
    return owner, session.engine.create_submap(owner)


def click_side(window, button, *, widget=None, double=False):
    """发送到 QWindow，覆盖 Qt 在子控件获得鼠标焦点之前的事件路由。"""
    if widget is None:
        widget = window.application.active_session.widget.currentWidget()
    position = widget.mapTo(window, widget.rect().center())
    assert window.rect().contains(position)
    click = QTest.mouseDClick if double else QTest.mouseClick
    click(window.windowHandle(), button, pos=position)
    QApplication.processEvents()


def test_side_buttons_replay_all_visible_pages(open_window, tmp_path, qtbot):
    window = open_window()
    session = new_document(window, tmp_path / "多种页面")
    engine, canvas = session.engine, session.canvas
    owner, submap = add_submap(session, "子导图入口")
    child = engine.add_child(engine.page_document(submap).root_id, "子导图选中节点")
    detail = engine.create_detail_page(owner)
    engine.set_node_graph(owner, NodeGraph())
    ideas = [engine.add_inspiration("同名想法", position=(index * 200, 0)) for index in range(2)]
    session.pages.registry.register(NodePageProvider("example", "示例", lambda _, parent: NodePageWidget(parent), dict))
    engine.set_extension_page(owner, "example", {"内容": "保留"})
    assert window.save()
    revision = engine.revision
    canvas.set_editable(False)

    canvas.navigate_to(submap)
    qtbot.waitUntil(lambda: not canvas._view_reset_pending)
    canvas.view.zoom(1.4)
    canvas.select_nodes([child])
    canvas.view.centerOn(canvas.node_items[child])
    expected_view = canvas.capture_page_view_state()
    canvas.navigate_to(detail)
    canvas.navigate_to(graph_page_id(owner))
    for idea in ideas:
        canvas.show_inspiration(idea)
    # 扩展的拥有者在主导图，打开时需要定位隐藏画布，历史应直接从第二个想法进入扩展。
    assert session.pages.open_page(owner, "example")
    sequence = (MAIN, (submap, None), (detail, None), (graph_page_id(owner), None),
                (engine.inspiration_id, ideas[0]), (engine.inspiration_id, ideas[1]),
                (extension_page_id(owner, "example"), None))
    assert session.current_page_key() == sequence[-1]

    remove_guard = canvas.register_navigation_guard(lambda: False)
    try:
        click_side(window, BACK)
        assert session.pages.active and session.current_page_key() == sequence[-1]
    finally:
        remove_guard()

    for target in reversed(sequence[:-1]):
        click_side(window, BACK)
        assert session.current_page_key() == target
        if target == (submap, None):
            actual = canvas.capture_page_view_state()
            assert actual["zoom"] == pytest.approx(expected_view["zoom"])
            assert actual["selection"] == [child]
            assert actual["center"] == pytest.approx(expected_view["center"], abs=3)
    click_side(window, BACK)
    assert session.current_page_key() == MAIN

    for target in sequence[1:]:
        click_side(window, FORWARD)
        assert session.current_page_key() == target
    click_side(window, FORWARD)
    assert session.current_page_key() == session.recent_pages.current_key == sequence[-1]
    assert engine.revision == revision and not engine.is_dirty


def test_rapid_presses_and_new_branch(open_window, tmp_path):
    window = open_window()
    session = new_document(window, tmp_path / "访问路线")
    owner, first = add_submap(session, "第一站")
    _, second = add_submap(session, "第二站")
    _, third = add_submap(session, "第三站")
    detail = session.engine.create_detail_page(owner)
    canvas = session.canvas
    for page in (first, second, third):
        canvas.navigate_to(page)

    click_side(window, BACK, double=True)
    assert session.current_page_key() == (first, None)
    canvas.navigate_to(first)
    click_side(window, FORWARD)
    assert session.current_page_key() == (second, None)
    click_side(window, BACK)
    canvas.navigate_to(detail)
    click_side(window, FORWARD)
    assert session.current_page_key() == (detail, None)
    canvas.navigate_to(None)
    for target in ((detail, None), (first, None), MAIN):
        click_side(window, BACK)
        assert session.current_page_key() == target


def test_empty_inspiration_and_stale_targets(open_window, tmp_path):
    window = open_window()
    session = new_document(window, tmp_path / "失效历史")
    engine, canvas = session.engine, session.canvas
    canvas.show_inspiration()
    canvas.navigate_to(None)
    click_side(window, BACK)
    assert session.current_page_key() == (engine.inspiration_id, None)
    click_side(window, FORWARD)
    assert session.current_page_key() == MAIN

    idea = engine.add_inspiration("将归并的想法", position=(0, 0))
    click_side(window, BACK)
    assert session.current_page_key() == MAIN
    _, first = add_submap(session, "有效子导图")
    owner, deleted = add_submap(session, "将删除的子导图")
    canvas.navigate_to(first)
    canvas.navigate_to(deleted)
    canvas.show_inspiration(idea)
    canvas.navigate_to(None)
    engine.remove_submap(owner)
    engine.attach_inspirations([idea], engine.root_id)

    for button, target in ((BACK, first), (BACK, None), (FORWARD, first), (FORWARD, None)):
        click_side(window, button)
        assert session.current_page_key() == (target, None)


@pytest.mark.parametrize("source", ["canvas", "properties"])
def test_side_button_waits_for_input_method(open_window, tmp_path, source):
    window = open_window()
    session = new_document(window, tmp_path / "输入草稿")
    owner, page = add_submap(session, "等待输入")
    canvas = session.canvas
    canvas.navigate_to(page)
    canvas.navigate_to(None)
    canvas.select_nodes([owner])

    if source == "canvas":
        canvas.begin_edit(owner)
        canvas.editor.setPlainText("待提交的草稿")
        receiver, send_event = canvas.editor, canvas.scene.sendEvent
    else:
        window.manager.activate("properties", focus=False)
        receiver = session.panels.properties.fields["style.font_family"].lineEdit()
        receiver.setFocus()
        send_event = QApplication.sendEvent
    send_event(receiver, QInputMethodEvent("候选", []))
    try:
        click_side(window, BACK)
        assert session.is_composing and session.current_page_key() == MAIN
        assert "候选" in window.status.currentMessage()
        if source == "canvas":
            assert canvas.editor is receiver and receiver.toPlainText() == "待提交的草稿"
    finally:
        send_event(receiver, QInputMethodEvent())

    click_side(window, BACK)
    assert session.current_page_key() == (page, None)
    click_side(window, FORWARD)
    assert session.current_page_key() == MAIN
    if source == "canvas":
        assert session.engine.get_node(owner).text == "待提交的草稿"


def test_side_click_preserves_active_split_and_copied_history_is_independent(open_window, tmp_path):
    window = open_window()
    source = new_document(window, tmp_path / "分屏")
    _, page = add_submap(source, "复制起点")
    source.canvas.navigate_to(page)
    assert window.manager.duplicate_document(source.id)
    copied = window.application.active_session
    window.host.editor.split_token(copied.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(copied.id)
    QApplication.processEvents()
    assert source.widget.isVisible() and copied.widget.isVisible()

    click_side(window, BACK, widget=source.canvas.view.viewport())
    assert window.application.active_session is copied
    assert source.current_page_key() == copied.current_page_key() == (page, None)
    copied.canvas.navigate_to(None)
    click_side(window, BACK, widget=source.canvas.view.viewport())
    assert copied.current_page_key() == (page, None)

    window.manager.activate_document(source.id)
    window.manager.activate("recent_pages", focus=False)
    QApplication.processEvents()
    click_side(window, BACK, widget=copied.canvas.view.viewport())
    assert source.current_page_key() == MAIN and copied.current_page_key() == (page, None)
    assert window.application.active_session is source
    click_side(window, FORWARD, widget=source.recent_pages.list_view.viewport())
    assert source.current_page_key() == (page, None) and window.application.active_session is source


def test_popup_and_modal_keep_their_own_events(open_window, tmp_path):
    window = open_window()
    session = new_document(window, tmp_path / "弹窗")
    _, page = add_submap(session, "弹窗之前")
    session.canvas.navigate_to(page)
    menu = QMenu(window)
    menu.addAction("测试")
    menu.popup(window.mapToGlobal(window.rect().center()))
    QApplication.processEvents()
    assert QApplication.activePopupWidget() is menu
    QTest.mouseClick(menu.windowHandle(), BACK, pos=menu.rect().center())
    assert session.current_page_key() == (page, None)
    menu.close()

    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    QApplication.processEvents()
    assert QApplication.activeModalWidget() is dialog
    click_side(window, BACK)
    assert session.current_page_key() == (page, None)
    dialog.close()
    click_side(window, BACK)
    assert session.current_page_key() == MAIN


def test_restart_reopen_and_reload_begin_at_actual_page(open_window, tmp_path):
    window = open_window()
    path = tmp_path / "重新打开"
    session = new_document(window, path)
    _, page = add_submap(session, "恢复页面")
    session.canvas.navigate_to(page)
    assert window.save()
    assert session.document.reload_preserving_views()
    click_side(window, BACK)
    assert session.current_page_key() == (page, None)
    session.canvas.navigate_to(None)
    session.canvas.navigate_to(page)
    assert window.close()

    restarted = open_window(restore=True)
    restored = restarted.application.active_session
    click_side(restarted, BACK)
    assert restored.current_page_key() == (page, None)
    assert restarted.manager.close_document(restored.id)
    assert restarted.open_path(path)
    click_side(restarted, FORWARD)
    assert restarted.application.active_session.current_page_key() == MAIN
