"""收藏侧栏的多文档隔离、真实停靠操作、自动保存和重启恢复。"""

import pytest
from mindmap_formats import load_document
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog


@pytest.fixture
def favorite_window(qtbot, tmp_path):
    window = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(window)
    window.resize(1100, 750)
    window.show()
    QApplication.processEvents()
    yield window
    for session in window.sessions.values():
        session.panels.properties.discard_pending()
        session.canvas.finish_edit(commit=False)
    window.close()


def add_favorite(window, path):
    """新建独立文件并收藏一张子导图，返回仍位于主导图的会话及页面 ID。"""
    assert window.new_document(path)
    session = window.active_session
    page = session.engine.create_submap(session.engine.add_child(session.engine.root_id, "同名收藏"))
    session.canvas.set_submap_favorite(page, True)
    return session, page


@pytest.mark.parametrize("split", [False, True])
def test_favorites_follow_active_session_and_preserve_views(favorite_window, tmp_path, qtbot, split):
    window = favorite_window
    assert not window.manager.states["favorites"].visible
    assert window._favorites_stack.currentIndex() == 0
    first, first_page = add_favorite(window, tmp_path / "甲.mindmap")
    second, second_page = add_favorite(window, tmp_path / "乙.mindmap")
    if split:
        window.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    window.manager.activate_document(first.id)
    window.manager.activate("favorites", focus=False)
    QApplication.processEvents()
    assert window._favorites_stack.currentWidget() is first.favorites and first.favorites.isVisible()
    assert first.favorites.model.rows[0][0].page_id == first_page
    first.canvas.navigate_to(first_page)
    QApplication.processEvents()
    first.canvas.view.zoom(1.3)
    transform = first.canvas.view.transform()
    first.canvas.navigate_to(None)
    first.canvas.set_editable(False)
    index = first.favorites.model.index(0, 0)
    qtbot.mouseClick(first.favorites.list_view.viewport(), Qt.MouseButton.LeftButton,
                     pos=first.favorites.list_view.visualRect(index).center())
    assert first.canvas.current_page_id == first_page and first.canvas.view.transform() == transform
    assert not first.favorites.remove_action.isEnabled()
    window.manager.activate_document(second.id)
    assert window._favorites_stack.currentWidget() is second.favorites
    assert second.favorites.model.rows[0][0].page_id == second_page
    second.favorites.remove_action.setData(second_page)
    second.favorites.remove_action.trigger()
    assert not second.engine.favorite_submaps() and first.engine.is_submap_favorite(first_page)
    assert window.manager.close_document(first.id)
    assert first.favorites._disposed and window._favorites_stack.count() == 2
    assert window.manager.close_document(second.id)
    assert window._favorites_stack.currentIndex() == 0 and window._favorites_stack.count() == 1


def test_favorites_shortcut_autosave_save_as_and_restart(favorite_window, tmp_path, qtbot, monkeypatch):
    window = favorite_window
    session, page = add_favorite(window, tmp_path / "原文件.mindmap")
    qtbot.waitUntil(lambda: not session.files.is_dirty, timeout=5000)
    assert load_document(session.files.path).metadata["submap_favorites"] == [page]
    window.activateWindow()
    session.canvas.view.setFocus()
    QApplication.processEvents()
    qtbot.keyClick(window, Qt.Key.Key_4, Qt.KeyboardModifier.AltModifier)
    QApplication.processEvents()
    assert window.manager.states["favorites"].visible and session.favorites.isVisible()
    assert window.manager.specs["favorites"].shortcut == "Alt+4"
    assert not window.manager.theme.icon("favorites").isNull()
    copied = tmp_path / "另存副本.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(copied), ""))
    assert window.save(save_as=True)
    assert load_document(copied).metadata["submap_favorites"] == [page]
    window.close()
    restored = build_window(storage_dir=tmp_path / "配置", restore=True)
    qtbot.addWidget(restored)
    restored.show()
    QApplication.processEvents()
    try:
        active = restored.active_session
        assert active.files.path == copied
        assert active.canvas.current_page_id is None and not active.files.is_dirty
        assert active.favorites.model.rows[0][0].page_id == page
        assert restored.manager.states["favorites"].visible and active.favorites.isVisible()
    finally:
        restored.close()


def test_failed_favorite_save_keeps_in_memory_state_and_allows_retry(
    favorite_window, tmp_path, monkeypatch
):
    window = favorite_window
    session, page = add_favorite(window, tmp_path / "写入失败.mindmap")
    original = session.files.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("收藏保存失败")

    with monkeypatch.context() as context:
        context.setattr("mindmap_workbench.sessions.save_document", fail)
        assert not session.autosave()
        assert session.files.is_dirty and session.engine.is_submap_favorite(page)
        assert session.files.path.read_bytes() == original
        assert session.favorites.model.rows[0][0].page_id == page
    assert window.save()
    assert not session.files.is_dirty and load_document(session.files.path).metadata["submap_favorites"] == [page]
