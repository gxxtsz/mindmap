"""同文件多标签的真实画布、共享保存、独立视图及正常退出恢复验收。"""

import json
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from mindmap_core.formats import load_document, save_document
from personal_workbench.app import build_window
from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent, QImage, QInputMethodEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu
from shiboken6 import isValid


@pytest.fixture
def windows(qtbot, tmp_path):
    """使用隔离的配置与文件；故障测试清理时恢复写入器并放弃未提交草稿。"""
    opened = []

    def clean(window):
        for session in window.plugins.instance("mindmap").sessions.values():
            session.panels.properties.discard_pending()
            session.panels.settings.discard_pending()
            session.canvas.finish_edit(commit=False)
            session.files.writer = session.document._write
            session.engine.mark_saved()

    def create(*, storage=None, restore=False, theme="light", compact=False):
        window = build_window(storage_dir=storage or tmp_path / "配置", restore=restore, theme=theme)
        qtbot.addWidget(window, before_close_func=clean)
        opened.append(window)
        window.resize(1200, 820)
        window.manager.set_option("compact", compact)
        window.show()
        QApplication.processEvents()
        return window

    yield create
    for window in reversed(opened):
        if not window._disposed:
            clean(window)
            window.close()


def new(window, path):
    assert window.new_document(path)
    return window.plugins.instance("mindmap").active_session


def duplicate(window, source):
    assert window.manager.duplicate_document(source.id)
    result = window.plugins.instance("mindmap").active_session
    assert result is not source
    QApplication.processEvents()
    return result


def assert_view(canvas, expected):
    """按公开快照验证页面、选择、倍率与场景中心，允许滚动条的逻辑像素舍入。"""
    actual = canvas.capture_page_view_state()
    assert actual["page_id"] == expected["page_id"]
    assert actual["selection"] == expected["selection"]
    assert actual["focus_id"] == expected["focus_id"]
    assert actual["zoom"] == pytest.approx(expected["zoom"])
    assert (QPointF(*actual["center"]) - QPointF(*expected["center"])).manhattanLength() < 4 / expected["zoom"]
    if "inspiration" in expected:
        assert actual["inspiration"]["root_id"] == expected["inspiration"]["root_id"]


@pytest.mark.parametrize("theme,compact", [("light", False), ("light", True), ("dark", False), ("dark", True)])
def test_copy_inactive_split_tab_uses_source_position_and_view(windows, tmp_path, theme, compact, monkeypatch):
    window = windows(theme=theme, compact=compact)
    source = new(window, tmp_path / "来源.mindmap")
    other = new(window, tmp_path / "其他.mindmap")
    window.host.editor.split_token(source.id, Qt.Orientation.Horizontal)
    source.canvas.view.zoom(1.6)
    source.canvas.view.update_navigation_rect(center=QPointF(90, -40))
    source.canvas.select_nodes([])
    source.canvas.set_editable(False)
    expected = source.canvas.capture_page_view_state()
    group, index = window.host.editor.find_token(source.id)
    window.manager.activate_document(other.id)
    recent = list(window.state.recent_files)
    monkeypatch.setattr("mindmap_qt_editor.canvas.files.load_document", lambda *a, **k: pytest.fail("复制不能重新读盘"))
    copied = duplicate(window, source)
    assert copied.document is source.document and copied.engine is source.engine
    assert copied.canvas is not source.canvas and copied.panels is not source.panels
    assert copied.files.state is source.files.state and copied.document.timer is source.document.timer
    assert window.host.editor.find_token(copied.id) == (group, index + 1)
    assert not copied.canvas.editable and not source.canvas.editable
    assert window.state.recent_files == recent
    assert_view(copied.canvas, expected)
    copied.canvas.set_editable(True)
    assert not source.canvas.editable
    again = duplicate(window, source)
    assert [group.tabBar().tabData(i) for i in range(group.count())] == [source.id, again.id, copied.id]
    assert window.open_path(source.files.path) and window.plugins.instance("mindmap").active_session is again
    window.manager.activate_document(other.id)
    assert window.open_path(source.files.path) and window.plugins.instance("mindmap").active_session is source


def test_tab_context_menu_copies_hit_tab_without_activating_it_first(windows, tmp_path):
    window = windows()
    first = new(window, tmp_path / "甲.mindmap")
    second = new(window, tmp_path / "乙.mindmap")
    group, index = window.host.editor.find_token(first.id)
    bar = group.tabBar()

    def choose():
        menu = bar.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly)[-1]
        try:
            assert window.plugins.instance("mindmap").active_session is second
            action = next(action for action in menu.actions() if action.text() == "复制标签")
            action.trigger()
        finally:
            menu.close()

    QTimer.singleShot(0, choose)
    point = bar.tabRect(index).center()
    bar.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, bar.mapToGlobal(point)))
    assert window.plugins.instance("mindmap").active_session.document is first.document
    assert window.plugins.instance("mindmap").active_session is not first


def test_tab_menu_opens_a_single_document_in_a_new_split_and_releases_menu(windows, tmp_path):
    window = windows()
    source = new(window, tmp_path / "同一份导图.mindmap")
    group, index = window.host.editor.find_token(source.id)
    bar = group.tabBar()
    opened = []

    def choose():
        menu = bar.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly)[-1]
        try:
            opened.append(menu)
            assert not next(action for action in menu.actions() if action.text() == "移到新分屏").isEnabled()
            split = next(action.menu() for action in menu.actions() if action.text() == "在新分屏打开同一文档")
            split.actions()[0].trigger()
        finally:
            menu.close()

    QTimer.singleShot(0, choose)
    point = bar.tabRect(index).center()
    bar.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, bar.mapToGlobal(point)))
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    copied = window.documents.active
    assert copied is not source and copied.document is source.document
    assert window.host.editor.find_token(source.id)[0] is group
    assert window.host.editor.find_token(copied.id)[0] is not group and len(window.host.editor.groups) == 2
    assert opened and not isValid(opened[0])


def test_failed_copy_rolls_back_registered_view_without_releasing_document(windows, tmp_path, monkeypatch):
    window = windows()
    source = new(window, tmp_path / "复制失败.mindmap")
    install = window.plugins.instance("mindmap").sessions._install

    def fail(candidate, **options):
        install(candidate, **options)
        raise OSError("模拟视图安装失败")

    monkeypatch.setattr(window.plugins.instance("mindmap").sessions, "_install", fail)
    assert not window.manager.duplicate_document(source.id)
    assert window.plugins.instance("mindmap").sessions.values() == (source,)
    assert list(window.manager.documents) == [source.id]
    assert source.document.views == [source] and not source.document.disposed
    assert not source.canvas.shared_views and window.plugins.instance("mindmap").active_session is source


@pytest.mark.parametrize("page_kind", ["main", "submap", "detail", "inspiration"])
def test_edit_and_history_sync_content_without_changing_other_view(windows, tmp_path, page_kind):
    window = windows()
    first = new(window, tmp_path / "独立视图.mindmap")
    engine = first.engine
    owner = engine.add_child(engine.root_id, "页面入口")
    pages = {"main": None, "submap": engine.create_submap(owner),
             "detail": engine.create_detail_page(owner), "inspiration": engine.inspiration_id}
    inspiration = engine.add_inspiration("想法", position=(0, 0))
    assert first.document.save()
    second = duplicate(window, first)
    if page_kind == "inspiration":
        second.canvas.show_inspiration(inspiration)
    else:
        second.canvas.navigate_to(pages[page_kind])
    second.canvas.view.zoom(1.4)
    second.canvas.select_nodes([])
    QApplication.processEvents()
    expected = second.canvas.capture_page_view_state()
    window.operation_log.clear()
    first.canvas.call(lambda: engine.add_child(engine.root_id, "从另一个标签添加"))
    assert_view(second.canvas, expected)
    assert len(window.operation_log.plain_text().splitlines()) == 1
    first.canvas.action("undo").trigger()
    assert_view(second.canvas, expected)
    first.canvas.action("redo").trigger()
    assert_view(second.canvas, expected)
    first.canvas.navigate_to(pages["submap"])
    first_state = first.canvas.capture_page_view_state()
    second.canvas.action("undo").trigger()
    assert second.canvas.current_page_id is None
    assert_view(first.canvas, first_state)
    assert first.engine.can_redo == second.engine.can_redo


@pytest.mark.parametrize("shared_document", [False, True])
@pytest.mark.parametrize("copy_text", [False, True])
def test_copy_from_right_split_then_paste_into_clicked_left_node(
    windows, tmp_path, qtbot, shared_document, copy_text,
):
    """跨分屏复制文字或分支后，真实点击决定粘贴目标，失焦提交不得抢回键盘焦点。"""
    window = windows()
    target = new(window, tmp_path / "粘贴目标.mindmap")
    target_node = target.engine.add_child(target.engine.root_id, "左侧目标")
    source = duplicate(window, target) if shared_document else new(window, tmp_path / "复制来源.mindmap")
    owner = source.engine.add_child(source.engine.root_id, "右侧入口")
    page = source.engine.create_detail_page(owner)
    source.canvas.navigate_to(page)
    source_node = source.engine.add_child(source.canvas.document_view.root_id, "待复制文字")
    window.host.editor.split_token(source.id, Qt.Orientation.Horizontal)
    window.activateWindow()
    QApplication.processEvents()
    for session, node in ((target, target_node), (source, source_node)):
        session.canvas.view.centerOn(session.canvas.node_items[node])
    source.canvas.view.setFocus()
    source.canvas.select_nodes([source_node])
    if copy_text:
        source.canvas.begin_edit(source_node)
    QApplication.processEvents()
    qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    if copy_text:
        assert QApplication.clipboard().text() == "待复制文字"

    item = target.canvas.node_items[target_node]
    point = target.canvas.view.mapFromScene(item.sceneBoundingRect().center())
    assert target.canvas.view.viewport().rect().contains(point)
    qtbot.mouseClick(target.canvas.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()
    assert source.canvas.editor is None
    assert target.canvas.selected_node_ids() == [target_node]
    # 不调用 setFocus 或直接触发粘贴动作，防止测试掩盖用户点击后被旧编辑器抢焦点的问题。
    assert QApplication.focusWidget() is target.canvas.view
    assert window.plugins.instance("mindmap").active_session is target
    qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    children = target.engine.get_node(target_node).children
    assert len(children) == 1 and target.engine.get_node(children[0]).text == "待复制文字"
    assert source.engine.get_node(source_node).children == []
    assert target.canvas.current_page_id is None and source.canvas.current_page_id == page


def test_inspiration_transfer_keeps_other_root_and_unrelated_page(windows, tmp_path):
    window = windows()
    first = new(window, tmp_path / "迁移.mindmap")
    branch = first.engine.add_child(first.engine.root_id, "可迁移")
    root = first.engine.add_inspiration("保留的想法", position=(0, 0))
    second = duplicate(window, first)
    third = duplicate(window, first)
    second.canvas.show_inspiration(root)
    third.canvas.select_nodes([])
    second_state = second.canvas.capture_page_view_state()
    third_state = third.canvas.capture_page_view_state()
    first.canvas.select_nodes([branch])
    first.canvas.move_selected_to_inspiration()
    assert first.canvas.current_inspiration_root_id == branch
    assert_view(second.canvas, second_state)
    assert_view(third.canvas, third_state)
    first.canvas.action("undo").trigger()
    assert branch not in first.engine.document.inspiration.root_ids
    assert_view(second.canvas, second_state)
    assert_view(third.canvas, third_state)


def test_removed_page_falls_back_and_can_be_undone_from_other_tab(windows, tmp_path):
    window = windows()
    first = new(window, tmp_path / "删除页面.mindmap")
    owner = first.engine.add_child(first.engine.root_id, "入口")
    page = first.engine.create_submap(owner)
    second = duplicate(window, first)
    second.canvas.navigate_to(page)
    first.canvas.call(lambda: first.engine.remove_submap(owner))
    assert second.canvas.current_page_id is None
    assert second.canvas.document_view.root_id == first.engine.root_id
    second.canvas.action("undo").trigger()
    assert first.engine.get_node(owner).submap_id == page
    assert "操作失败" not in window.operation_log.plain_text()


def test_save_all_commits_all_drafts_and_writes_each_file_once(windows, tmp_path, monkeypatch):
    window = windows()
    first = new(window, tmp_path / "共享保存.mindmap")
    child = first.engine.add_child(first.engine.root_id, "子节点")
    assert first.document.save()
    second = duplicate(window, first)
    first.canvas.select_nodes([first.engine.root_id])
    second.canvas.select_nodes([child])
    for view, size in ((first, 26), (second, 32)):
        view.panels.properties.fields["style.font_size"].setValue(size)
        view.panels.properties.timer.stop()
    writes = []

    def write(document, path, **options):
        writes.append(path)
        save_document(document, path, **options)

    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", write)
    assert window.save_all()
    saved = load_document(first.files.path)
    assert saved.root.style["font_size"] == 26 and saved.nodes[child].style["font_size"] == 32
    assert writes == [first.files.path]
    assert not first.engine.is_dirty and not second.files.is_dirty
    assert "共 1 份文档" in window.operation_log.plain_text()


def test_save_as_rebinds_all_views_and_rejects_other_open_file(windows, tmp_path, monkeypatch):
    window = windows()
    first = new(window, tmp_path / "原文件.mindmap")
    second = duplicate(window, first)
    occupied = new(window, tmp_path / "占用.mindmap")
    window.manager.activate_document(second.id)
    original = first.files.path
    before = original.read_bytes()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(occupied.files.path), ""))
    assert not window.save(save_as=True)
    assert first.files.path == second.files.path == original
    target = tmp_path / "统一另存.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(target), ""))
    assert window.save(save_as=True)
    assert first.files.path == second.files.path == target
    assert not first.document.last_error and not second.document.last_error
    assert all(window.manager.documents[view.id]["title"] == target.stem for view in (first, second))
    second.canvas.call(lambda: second.engine.update_node(second.engine.root_id, text="新路径修改"))
    assert first.document.save()
    assert original.read_bytes() == before
    assert load_document(target).root.text == "新路径修改"


def test_shared_save_failure_preserves_all_close_targets_and_retry(windows, tmp_path, monkeypatch):
    window = windows()
    first = new(window, tmp_path / "保存失败.mindmap")
    second = duplicate(window, first)
    first.canvas.call(lambda: first.engine.update_node(first.engine.root_id, text="不能丢失"))
    before = first.files.path.read_bytes()
    window.operation_log.clear()

    def fail(*a, **k):
        raise OSError("模拟磁盘失败")

    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", fail)
    assert not window.manager.close_documents([first.id, second.id])
    assert len(window.plugins.instance("mindmap").sessions) == 2 and not first.disposed and not second.disposed
    assert first.document.last_error == second.document.last_error and "模拟磁盘失败" in first.document.last_error
    assert first.files.path.read_bytes() == before
    assert len(window.operation_log.plain_text().splitlines()) == 1
    monkeypatch.setattr("workbench_plugin_mindmap.documents.save_document", save_document)
    assert window.manager.close_document(first.id)
    assert first.disposed and not first.document.disposed and not second.disposed
    second.canvas.call(lambda: second.engine.add_child(second.engine.root_id, "继续编辑"))
    assert second.document.timer.isActive() and second.document.save()
    assert load_document(second.files.path).root.children
    document = second.document
    assert window.manager.close_document(second.id)
    assert document.disposed and not document.timer.isActive()


def test_background_save_is_shared_and_newer_changes_remain_dirty(windows, tmp_path, monkeypatch):
    window = windows()
    first = new(window, tmp_path / "共享后台.mindmap")
    second = duplicate(window, first)
    submitted, future = [], Future()

    def submit(function, document, path, **kwargs):
        submitted.append((document, path))
        return future

    monkeypatch.setattr("workbench_plugin_mindmap.documents.BACKGROUND_SAVE_NODE_LIMIT", 0)
    monkeypatch.setattr("workbench_plugin_mindmap.documents._save_pool", lambda: SimpleNamespace(submit=submit))
    first.canvas.call(lambda: first.engine.update_node(first.engine.root_id, text="后台版本"))
    assert first.document.autosave(background=True) and second.document.autosave(background=True)
    assert len(submitted) == 1 and first.document._background_save is second.document._background_save
    second.canvas.call(lambda: second.engine.update_node(second.engine.root_id, text="后续版本"))
    save_document(*submitted[0])
    future.set_result(None)
    first.document._finish_background_save()
    assert first.engine.is_dirty and second.engine.is_dirty and second.document.timer.isActive()
    assert load_document(second.files.path).root.text == "后台版本"
    assert window.manager.close_document(first.id)
    assert not second.document.disposed and second.document.save()
    assert load_document(second.files.path).root.text == "后续版本"


@pytest.mark.parametrize("composing_source", [True, False])
def test_copy_rejects_source_or_active_ime_without_creating_view(windows, tmp_path, composing_source):
    window = windows()
    first = new(window, tmp_path / "来源.mindmap")
    active = new(window, tmp_path / "当前.mindmap")
    target = first if composing_source else active
    target.canvas.begin_edit(target.engine.root_id)
    editor = target.canvas.editor
    editor.inputMethodEvent(QInputMethodEvent("候选", []))
    count = len(window.plugins.instance("mindmap").sessions)
    assert not window.manager.duplicate_document(first.id)
    assert len(window.plugins.instance("mindmap").sessions) == count and target.canvas.editor is editor and editor.composing


def test_shared_save_rejects_other_tab_ime_and_autosave_keeps_draft(windows, tmp_path):
    window = windows()
    first = new(window, tmp_path / "共享候选.mindmap")
    second = duplicate(window, first)
    first.canvas.call(lambda: first.engine.update_node(first.engine.root_id, text="已提交"))
    second.canvas.begin_edit(second.engine.root_id)
    editor = second.canvas.editor
    editor.setPlainText("尚未提交")
    editor.inputMethodEvent(QInputMethodEvent("候选", []))
    assert first.document.autosave()
    assert second.canvas.editor is editor and editor.composing
    assert load_document(first.files.path).root.text == "已提交"
    assert not first.document.save() and len(window.plugins.instance("mindmap").sessions) == 2
    assert not window.manager.close_documents([first.id, second.id])
    assert second.canvas.editor is editor


def test_page_reference_edit_menu_shared_tab_and_autosave(windows, tmp_path, monkeypatch):
    """从工作台编辑菜单创建引用；共享标签改名后同步显示并保存原始正文。"""
    window = windows()
    path = tmp_path / "页面引用.mindmap"
    first = new(window, path)
    engine = first.engine
    source = engine.add_child(engine.root_id, "保留的原文")
    owner = engine.add_child(engine.root_id, "章节")
    page = engine.create_submap(owner)
    root = engine.page_document(page).root_id
    first.canvas.select_nodes([source])
    menu = window.main_menu()
    edit_menu = next(action.menu() for action in menu.actions() if action.text() == "编辑")
    page_menu = next(action.menu() for action in edit_menu.actions() if action.text() == "页面")
    action = first.canvas.action("set_page_reference")
    assert action in page_menu.actions() and action.isEnabled()
    monkeypatch.setattr("mindmap_qt_editor.canvas.navigation.choose_reference_root", lambda *a, **k: root)
    action.trigger()
    assert first.canvas.document_view.nodes[source].text == "章节"
    assert "设置页面引用" in window.operation_log.plain_text()
    menu.deleteLater()

    second = duplicate(window, first)
    second.canvas.open_page_reference(source)
    assert second.canvas.current_page_id == page and first.canvas.current_page_id is None
    second.canvas.begin_edit(root)
    second.canvas.editor.setPlainText("另一标签中的新标题")
    second.canvas.call(second.canvas.finish_edit)
    assert first.canvas.document_view.nodes[source].text == "另一标签中的新标题"
    assert first.document.autosave()
    saved = load_document(path)
    assert saved.nodes[source].text == "保留的原文"
    assert saved.nodes[source].page_reference_root_id == root
    assert saved.nodes[root].text == "另一标签中的新标题"
    second.canvas.return_to_parent_page()
    assert second.canvas.current_page_id is None and second.canvas.selected_node_ids() == [source]
    assert not engine.is_dirty

    reopened = windows(storage=tmp_path / "重开配置")
    assert reopened.open_path(path)
    restored = reopened.plugins.instance("mindmap").active_session.canvas
    assert restored.document_view.nodes[source].text == "另一标签中的新标题"
    restored.open_page_reference(source)
    assert restored.current_page_id == page and restored.selected_node_ids() == [root]


@pytest.mark.parametrize("inspiration", [False, True])
def test_other_view_updates_keep_live_text_and_input_method_draft(windows, tmp_path, inspiration):
    window = windows()
    first = new(window, tmp_path / "保留编辑.mindmap")
    if inspiration:
        root = first.engine.add_inspiration("原想法", position=(0, 0))
        first.canvas.show_inspiration(root)
    else:
        root = first.engine.root_id
    second = duplicate(window, first)
    first.canvas.begin_edit(root)
    editor = first.canvas.editor
    editor.setPlainText("尚未确认的正文")
    editor.inputMethodEvent(QInputMethodEvent("输入法候选", []))
    second.canvas.call(lambda: second.engine.update_node(root, text="来自另一标签的内容"))
    assert first.canvas.editor is editor and editor.composing
    assert editor.toPlainText() == "尚未确认的正文"
    editor.inputMethodEvent(QInputMethodEvent())
    first.canvas.finish_edit(commit=True)
    assert first.engine.get_node(root).text == "尚未确认的正文"
    assert second.canvas.document_view.nodes[root].text == "尚未确认的正文"


def test_image_completion_and_properties_keep_other_view_selection(windows, tmp_path):
    window = windows()
    first = new(window, tmp_path / "共享图片.mindmap")
    child = first.engine.add_child(first.engine.root_id, "选中主题")
    second = duplicate(window, first)
    second.canvas.select_nodes([child])
    state = second.canvas.capture_page_view_state()
    data = QBuffer()
    assert data.open(QIODevice.OpenModeFlag.WriteOnly)
    image = QImage(32, 24, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(data, "PNG")
    # 此提交入口也由网络下载完成回调调用，不能借用当前活动标签作为来源。
    key = first.canvas._insert_image_child(first.engine.root_id, bytes(data.data()), "image/png")
    assert second.canvas.document_view.nodes[key].is_image_node
    assert_view(second.canvas, state)
    first.canvas.select_nodes([child])
    first.panels.properties.fields["style.font_size"].setValue(31)
    first.panels.properties.flush()
    assert second.engine.get_node(child).style["font_size"] == 31
    assert_view(second.canvas, state)


@pytest.mark.parametrize("replace", [False, True])
def test_deleted_or_replaced_document_clears_invalid_shared_editor(windows, tmp_path, replace):
    """另一个标签删除编辑目标或替换文档时安全结束失效交互，不查询已删除实体。"""
    from mindmap_core import MindMapDocument

    window = windows()
    first = new(window, tmp_path / "失效编辑.mindmap")
    child = first.engine.add_child(first.engine.root_id, "将被删除")
    second = duplicate(window, first)
    second.canvas.begin_edit(child)
    if replace:
        first.canvas.call(lambda: first.engine.replace_document(MindMapDocument.new("新文档")))
    else:
        first.canvas.call(lambda: first.engine.remove_nodes([child]))
    assert second.canvas.editor is None
    assert child not in second.canvas.document_view.nodes
    assert second.panels.properties._ids == second.canvas.selected_node_ids()


def test_restart_restores_all_shared_views_and_loads_file_once(windows, tmp_path, monkeypatch):
    window = windows()
    first = new(window, tmp_path / "恢复共享.mindmap")
    owner = first.engine.add_child(first.engine.root_id, "子图入口")
    page = first.engine.create_submap(owner)
    second = duplicate(window, first)
    third = duplicate(window, second)
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    second.canvas.navigate_to(page)
    third.canvas.view.zoom(1.7)
    third.canvas.select_nodes([])
    window.manager.activate_document(third.id)
    QApplication.processEvents()
    expected = {view.id: view.canvas.capture_page_view_state() for view in (first, second, third)}
    order = [view.id for view in window.plugins.instance("mindmap").sessions.ordered()]
    locations = {key: (window.host.editor.find_token(key)[0].group_id,
                       window.host.editor.find_token(key)[1]) for key in expected}
    storage = window.manager.storage_dir
    assert window.close()
    payload = json.loads(window.state.store.path.read_text("utf-8"))
    assert payload["schema_version"] == 1 and len(payload["views"]) == 3
    assert len({item["path"] for item in payload["views"]}) == 1
    from mindmap_qt_editor.canvas import files

    original, loads = files.load_document, []

    def load(path, **options):
        loads.append(path)
        return original(path, **options)

    monkeypatch.setattr(files, "load_document", load)
    restored = windows(storage=storage, restore=True)
    assert len(loads) == 1 and len(restored.plugins.instance("mindmap").sessions) == 3
    assert restored.plugins.instance("mindmap").active_session.id == third.id
    assert [view.id for view in restored.plugins.instance("mindmap").sessions.ordered()] == order
    assert len({id(view.engine) for view in restored.plugins.instance("mindmap").sessions.values()}) == 1
    for key, state in expected.items():
        assert_view(restored.plugins.instance("mindmap").sessions.get(key).canvas, state)
        group, index = restored.host.editor.find_token(key)
        assert (group.group_id, index) == locations[key]


def test_restore_preserves_each_view_for_unavailable_shared_path(windows, tmp_path):
    window = windows()
    source = new(window, tmp_path / "丢失.mindmap")
    duplicate(window, source)
    records = window.documents.snapshot()
    assert window.close()
    source.files.path.unlink()
    restored = windows(restore=False)
    assert len(restored.documents.restore(records, source.id)) == len(records)
    assert not len(restored.plugins.instance("mindmap").sessions)
    assert restored.documents.snapshot() == records
