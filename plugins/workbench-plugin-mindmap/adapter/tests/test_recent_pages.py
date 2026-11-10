"""最近访问侧栏的文件级共享、实际导航、持久化及布局升级验收。"""

import json

import pytest
from personal_workbench.app import build_window
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QFileDialog
from workbench_docking import DockPosition, DockTarget
from workbench_plugin_mindmap.page_history import RECENT_PAGE_LIMIT, PageHistoryState
from workbench_plugin_mindmap.recent_pages import RecentPageHistory
from workbench_runtime.state import StateStore


@pytest.fixture
def windows(qtbot, tmp_path):
    opened = []

    def create(*, restore=False):
        window = build_window(storage_dir=tmp_path / "配置", restore=restore)
        qtbot.addWidget(window)
        opened.append(window)
        window.resize(1200, 800)
        window.show()
        QApplication.processEvents()
        return window

    yield create
    for window in reversed(opened):
        if not window._disposed:
            for session in window.plugins.instance("mindmap").sessions.values():
                session.panels.properties.discard_pending()
                session.panels.settings.discard_pending()
                session.canvas.finish_edit(commit=False)
            window.close()


def create_document(window, path):
    assert window.new_document(path)
    return window.plugins.instance("mindmap").active_session


def add_pages(session):
    """同名但身份不同的子图、详情页与两个想法，便于检查键是否完整。"""
    engine = session.engine
    owner = engine.add_child(engine.root_id, "同名页面")
    page = engine.create_submap(owner)
    detail = engine.create_detail_page(owner)
    ideas = [engine.add_inspiration("同名页面", position=(index * 200, 0)) for index in range(2)]
    return owner, page, detail, ideas


def keys(session):
    return tuple(entry.key for entry in session.recent_pages.model.rows)


def click_page(session, target, qtbot):
    """通过真实行位置点击，验证列表重排后仍按稳定键跳转。"""
    view = session.recent_pages.list_view
    index = session.recent_pages.model.index_for(target)
    assert index.isValid()
    view.scrollTo(index)
    QApplication.processEvents()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())


def test_recent_navigation_covers_all_types_and_preserves_views_and_file(windows, tmp_path, qtbot):
    window = windows()
    session = create_document(window, tmp_path / "导航.mindmap")
    owner, page, detail, ideas = add_pages(session)
    assert window.save()
    canvas, engine = session.canvas, session.engine
    before, revision = session.files.path.read_bytes(), engine.revision
    recent_files = list(window.state.recent_files)
    canvas.navigate_to(page)
    # 等首次页面定位完成后模拟用户缩放，避免把尚未执行的初始视图误当成缓存。
    qtbot.waitUntil(lambda: not canvas._view_reset_pending)
    canvas.view.zoom(1.4)
    transform = canvas.view.transform()
    canvas.navigate_to(detail)
    for idea in ideas:
        canvas.show_inspiration(idea)
    assert keys(session) == ((engine.inspiration_id, ideas[1]), (engine.inspiration_id, ideas[0]),
                             (detail, None), (page, None), (None, None))
    window.manager.activate("recent_pages", focus=False)
    canvas.set_editable(False)
    click_page(session, (page, None), qtbot)
    assert canvas.current_page_id == page and canvas.view.transform() == transform
    assert keys(session)[0] == (page, None) and len(keys(session)) == 5
    assert session.recent_pages.current_key == (page, None)
    click_page(session, (engine.inspiration_id, ideas[0]), qtbot)
    assert canvas.current_inspiration_root_id == ideas[0]
    assert window.manager.states["recent_pages"].visible and not window.manager.states["inspiration"].visible
    click_page(session, (None, None), qtbot)
    assert canvas.current_page_id is None
    window.plugins.instance("mindmap").flush_recent_pages()
    assert engine.revision == revision and not engine.is_dirty
    assert session.files.path.read_bytes() == before and window.state.recent_files == recent_files


def test_file_isolation_shared_tabs_and_inactive_navigation(windows, tmp_path):
    window = windows()
    first = create_document(window, tmp_path / "甲.mindmap")
    owner, page, detail, ideas = add_pages(first)
    first.canvas.navigate_to(page)
    assert window.plugins.instance("mindmap").sessions.duplicate_session(first.id)
    second = window.plugins.instance("mindmap").active_session
    second.canvas.navigate_to(detail)
    window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    assert first.document is second.document and keys(first) == keys(second)
    before = keys(first)
    first.canvas.navigate_to(None)
    assert window.plugins.instance("mindmap").active_session is second and keys(first) == before
    first.engine.update_node(owner, text="共同的新名称")
    assert keys(first) == before and first.recent_pages.model.rows[0].title == "共同的新名称"
    window.manager.activate_document(first.id)
    assert keys(first)[0] == (None, None) and keys(first) == keys(second)
    assert second.canvas.current_page_id == detail and second.recent_pages.current_key == (detail, None)
    other = create_document(window, tmp_path / "乙.mindmap")
    assert keys(other) == ((None, None),) and window.plugins.instance("mindmap")._recent_pages_stack.currentWidget() is other.recent_pages
    assert window.manager.close_document(first.id)
    assert first.recent_pages._disposed and not second.recent_pages._disposed
    window.manager.activate_document(second.id)
    assert window.plugins.instance("mindmap")._recent_pages_stack.currentWidget() is second.recent_pages
    assert keys(second)[0] == (detail, None)


def test_restart_ignores_intermediate_activations_and_restores_each_file(windows, tmp_path, monkeypatch):
    window = windows()
    first = create_document(window, tmp_path / "甲.mindmap")
    _, page, detail, ideas = add_pages(first)
    first.canvas.navigate_to(page)
    assert window.plugins.instance("mindmap").sessions.duplicate_session(first.id)
    duplicate = window.plugins.instance("mindmap").active_session
    duplicate.canvas.navigate_to(detail)
    second = create_document(window, tmp_path / "乙.mindmap")
    _, second_page, _, _ = add_pages(second)
    second.canvas.navigate_to(second_page)
    window.manager.activate_document(first.id)
    expected = {str(item.files.path): keys(item) for item in window.plugins.instance("mindmap").sessions.values()}
    assert window.close()
    visits = []
    original = RecentPageHistory.visit

    def record(self, page_id, root_id=None):
        visits.append((str(self.path), page_id, root_id))
        return original(self, page_id, root_id)

    monkeypatch.setattr(RecentPageHistory, "visit", record)
    restored = windows(restore=True)
    assert restored.plugins.instance("mindmap").active_session.id == first.id and restored.plugins.instance("mindmap").canvas.current_page_id == page
    assert visits == [(str(first.files.path), page, None)]
    for session in restored.plugins.instance("mindmap").sessions.values():
        assert keys(session) == expected[str(session.files.path)]


def test_close_reopen_save_as_and_new_overwrite_history(windows, tmp_path, monkeypatch):
    window = windows()
    source = tmp_path / "原文件.mindmap"
    session = create_document(window, source)
    _, page, detail, _ = add_pages(session)
    session.canvas.navigate_to(page)
    session.canvas.navigate_to(detail)
    expected = keys(session)
    target = tmp_path / "另存文件.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    assert window.save(save_as=True)
    assert window.plugins.instance("mindmap").page_history.pages_for(source) == expected
    assert window.plugins.instance("mindmap").page_history.pages_for(target) == expected
    assert window.manager.close_document(session.id)
    assert window.plugins.instance("mindmap")._recent_pages_stack.count() == 1 and window.plugins.instance("mindmap")._recent_pages_stack.currentIndex() == 0
    assert window.open_path(target)
    reopened = window.plugins.instance("mindmap").active_session
    assert reopened.canvas.current_page_id is None
    assert keys(reopened) == ((None, None), (detail, None), (page, None))
    assert window.manager.close_document(reopened.id)
    replaced = create_document(window, target)
    assert keys(replaced) == ((None, None),)
    assert window.plugins.instance("mindmap").page_history.pages_for(source) == expected


def test_deleted_pages_and_attached_ideas_are_pruned_without_reinserting_on_undo(windows, tmp_path):
    window = windows()
    session = create_document(window, tmp_path / "变化.mindmap")
    owner, page, detail, ideas = add_pages(session)
    engine, canvas = session.engine, session.canvas
    canvas.navigate_to(page)
    canvas.navigate_to(None)
    engine.move_to_inspiration([owner], positions={owner: (0, 0)})
    summary = next(entry for entry in session.recent_pages.model.rows if entry.page_id == page)
    assert summary.path[0].text == "灵感区"
    canvas.show_inspiration(owner)
    canvas.navigate_to(None)
    engine.attach_inspirations([owner], engine.root_id)
    assert (engine.inspiration_id, owner) not in keys(session) and (page, None) in keys(session)
    engine.remove_submap(owner)
    assert (page, None) not in keys(session)
    engine.undo()
    assert engine.page_summary(page) is not None and (page, None) not in keys(session)
    canvas.navigate_to(page)
    assert keys(session)[0] == (page, None)


@pytest.mark.parametrize("input_source", ["canvas", "properties"])
def test_ime_blocked_navigation_preserves_history_current_page_and_draft(windows, tmp_path, qtbot, input_source):
    window = windows()
    session = create_document(window, tmp_path / "输入保护.mindmap")
    owner, page, _, _ = add_pages(session)
    canvas = session.canvas
    canvas.navigate_to(page)
    canvas.navigate_to(None)
    canvas.select_nodes([owner])
    before = keys(session)
    if input_source == "canvas":
        canvas.begin_edit(owner)
        canvas.editor.setPlainText("未完成的草稿")
        canvas.scene.sendEvent(canvas.editor, QInputMethodEvent("候选", []))
    else:
        field = session.panels.properties.fields["style.font_family"].lineEdit()
        QApplication.sendEvent(field, QInputMethodEvent("候选", []))
    try:
        click_page(session, (page, None), qtbot)
        assert canvas.current_page_id is None and keys(session) == before and session.is_composing
        assert session.recent_pages.current_key == (None, None)
        assert session.recent_pages.list_view.currentIndex() == session.recent_pages.model.index_for((None, None))
        assert "候选" in window.status.currentMessage()
        if input_source == "canvas":
            assert canvas.editor.toPlainText() == "未完成的草稿"
    finally:
        if input_source == "canvas":
            canvas.finish_edit(commit=False)
        else:
            QApplication.sendEvent(field, QInputMethodEvent())
    click_page(session, (page, None), qtbot)
    assert canvas.current_page_id == page and keys(session)[0] == (page, None)


def test_debounced_failure_is_nonblocking_and_retry_keeps_memory(windows, tmp_path, monkeypatch, qtbot):
    window = windows()
    session = create_document(window, tmp_path / "保存失败.mindmap")
    _, page, _, _ = add_pages(session)
    assert window.save()
    original = window.state.store.path.read_bytes()
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("配置目录不可写")
        patch.setattr(window.state.store, "save", fail)
        session.canvas.navigate_to(page)
        assert window.plugins.instance("mindmap")._recent_pages_timer.isActive()
        qtbot.waitUntil(lambda: "最近访问记录未能保存" in window.status.currentMessage())
        assert session.canvas.current_page_id == page and keys(session)[0] == (page, None)
        assert window.state.store.path.read_bytes() == original
        assert window.plugins.instance("mindmap").page_history.dirty and not session.engine.is_dirty
    # 重访已经置顶的当前页也可重试失败写入，不必人为改动排序。
    session._navigate_recent_page(page, None)
    qtbot.waitUntil(lambda: not window.plugins.instance("mindmap").page_history.dirty)
    loaded = PageHistoryState(StateStore(window.manager.storage_dir).plugin_state("mindmap"))
    loaded.load()
    assert loaded.pages_for(session.files.path) == keys(session) and not window.plugins.instance("mindmap").page_history.dirty


def test_empty_inspiration_and_stale_navigation_never_choose_another_page(windows, tmp_path):
    window = windows()
    session = create_document(window, tmp_path / "失效目标.mindmap")
    session.canvas.show_inspiration()
    assert keys(session) == ((None, None),)
    session.canvas.navigate_to(None)
    owner = session.engine.add_child(session.engine.root_id, "将被删除的入口")
    page = session.engine.create_submap(owner)
    session.canvas.navigate_to(page)
    session.canvas.navigate_to(None)
    session.engine.remove_submap(owner)
    before = keys(session)
    # 模拟列表项删除前已经捕获、之后才到达宿主的稳定目标。
    session.recent_pages.navigate_requested.emit(page, None)
    assert "该页面已不存在" in window.status.currentMessage()
    assert session.canvas.current_page_id is None and keys(session) == before
    assert session.recent_pages.current_key == (None, None)


def test_history_evicts_oldest_different_page_and_keeps_duplicate_single(windows, tmp_path):
    window = windows()
    session = create_document(window, tmp_path / "容量.mindmap")
    pages = [session.engine.create_submap(session.engine.add_child(session.engine.root_id, f"页面 {index}"))
             for index in range(RECENT_PAGE_LIMIT + 2)]
    for page in pages:
        session.document.recent_pages.visit(page)
    assert keys(session) == tuple((page, None) for page in reversed(pages[-RECENT_PAGE_LIMIT:]))
    session.document.recent_pages.visit(pages[-2])
    assert len(keys(session)) == RECENT_PAGE_LIMIT and keys(session)[0] == (pages[-2], None)


@pytest.mark.parametrize("visible", [False, True])
def test_sidebar_shortcut_theme_layout_and_restart(windows, tmp_path, qtbot, visible):
    window = windows()
    session = create_document(window, tmp_path / "布局.mindmap")
    assert window.manager.states["recent_pages"].visible and session.recent_pages.isVisible()
    assert window.manager.specs["recent_pages"].shortcut == "Alt+7"
    assert not window.manager.theme.icon("recent-pages").isNull()
    window.activateWindow()
    session.canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(window, Qt.Key.Key_7, Qt.KeyboardModifier.AltModifier)
    assert not window.manager.states["recent_pages"].visible
    window.plugins.instance("mindmap").recent_pages_action.trigger()
    assert window.manager.states["recent_pages"].visible
    window.manager.move("recent_pages", DockTarget(DockPosition.LEFT_BOTTOM))
    window.manager.set_theme("dark")
    window.manager.set_option("compact", True)
    if not visible:
        window.manager.hide("recent_pages")
    assert window.close()
    restored = windows(restore=True)
    assert restored.manager.states["recent_pages"].visible is visible
    assert restored.manager.states["recent_pages"].position == DockPosition.LEFT_BOTTOM
    assert restored.manager.theme.name == "dark" and restored.manager.settings.compact
    restored.manager.restore_default_layout()
    assert restored.manager.states["recent_pages"].visible
    assert restored.manager.states["recent_pages"].position == DockPosition.LEFT_TOP


def test_old_layout_adds_recent_panel_once_and_preserves_other_settings(windows, tmp_path):
    window = windows()
    create_document(window, tmp_path / "升级.mindmap")
    window.manager.activate("favorites", focus=False)
    snapshot = window.manager.capture_layout()
    snapshot["tools"] = [tool for tool in snapshot["tools"] if tool["id"] != "recent_pages"]
    snapshot["theme"] = "dark"
    snapshot["hosts"]["main"]["region_sizes"]["dock"]["left"] = 319
    assert window.close()
    layout_file = tmp_path / "配置" / "workbench-layout-v1.json"
    layout_file.write_text(json.dumps({"schema_version": 1, "session": snapshot}), encoding="utf-8")
    restored = windows(restore=True)
    assert restored.manager.states["recent_pages"].visible
    assert not restored.manager.states["favorites"].visible
    assert restored.manager.active_id == "recent_pages"
    assert restored.manager.theme.name == "dark" and restored.host.region_sizes["dock"]["left"] == 319
    restored.manager.hide("recent_pages")
    assert restored.close()
    again = windows(restore=True)
    assert not again.manager.states["recent_pages"].visible
