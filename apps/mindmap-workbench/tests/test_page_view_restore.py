"""正常退出后的逐文件页面视图恢复及真实启动器验收。"""

import json
import os
import subprocess
import sys
import textwrap
from copy import deepcopy

import pytest
from mindmap_core import MindMapEngine
from mindmap_formats import load_document, save_document
from mindmap_workbench.document_state import DocumentState
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QFileDialog


@pytest.fixture
def windows(qtbot, tmp_path):
    """每例使用独立配置；所有窗口均通过正式退出流程清理。"""
    opened = []

    def create(*, restore=False, storage=None, theme=None, visible=True):
        window = build_window(storage_dir=storage or tmp_path / "配置", theme=theme, restore=restore)
        qtbot.addWidget(window)
        opened.append(window)
        if visible:
            window.show()
            QApplication.processEvents()
        return window

    yield create
    for window in reversed(opened):
        window.close()


def open_pages(window, path):
    """使用临时文件建立四类页面，同名入口只能按标识区分。"""
    engine = MindMapEngine()
    first_owner = engine.add_child(engine.root_id, "同名")
    engine.add_child(engine.root_id, "主图兄弟")
    first = engine.create_submap(first_owner)
    first_root = engine.document.submaps[first].root_id
    nested_owner = engine.add_child(first_root, "同名")
    engine.add_child(first_root, "子图兄弟")
    nested = engine.create_submap(nested_owner)
    nested_root = engine.document.submaps[nested].root_id
    owner = engine.add_child(nested_root, "详情入口")
    engine.add_child(nested_root, "嵌套兄弟")
    detail = engine.create_detail_page(owner)
    detail_root = engine.document.detail_pages[detail].root_id
    engine.add_child(detail_root, "详情内容一")
    engine.add_child(detail_root, "详情内容二")
    save_document(engine.document, path)
    assert window.open_path(path)
    return window.active_session, [None, first, nested, detail]


def position(session, page_id, index=0):
    """调整当前页视图，使用公开快照作为重启后的验收目标。"""
    canvas = session.canvas
    canvas.navigate_to(page_id)
    QApplication.processEvents()
    canvas.view.zoom((0.7 + 0.35 * index) / canvas.view.transform().m11())
    canvas.view.update_navigation_rect(center=QPointF(80 + 20 * index, -70 + 10 * index))
    canvas.select_nodes([] if index == 1 else canvas.document_view.root.children)
    return canvas.capture_page_view_state()


def assert_snapshot(canvas, expected):
    actual = canvas.capture_page_view_state()
    assert actual["page_id"] == expected["page_id"]
    assert actual["zoom"] == pytest.approx(expected["zoom"])
    assert actual["selection"] == expected["selection"]
    assert actual["focus_id"] == expected["focus_id"]
    assert (QPointF(*actual["center"]) - QPointF(*expected["center"])).manhattanLength() < 3 / expected["zoom"]


@pytest.mark.parametrize("theme,compact", [("light", False), ("light", True), ("dark", False), ("dark", True)])
@pytest.mark.parametrize("split", [False, True])
def test_restart_restores_each_page_in_visible_and_hidden_tabs(windows, tmp_path, theme, compact, split, qtbot):
    window = windows(theme=theme)
    window.manager.set_option("compact", compact)
    sessions, expected = [], {}
    for index in range(4):
        session, pages = open_pages(window, tmp_path / f"导图{index}.mindmap")
        position(session, pages[index], index)
        sessions.append(session)
    if split:
        window.host.editor.split_token(sessions[1].id, Qt.Orientation.Horizontal)
    window.manager.activate_document(sessions[2].id)
    QApplication.processEvents()
    for session in sessions:
        expected[session.id] = session.canvas.capture_page_view_state()
    paths = {session.id: session.files.path for session in sessions}
    saved = {identifier: path.read_bytes() for identifier, path in paths.items()}
    order = [session.id for session in window.sessions.ordered()]
    locations = {identifier: (window.host.editor.find_token(identifier)[0].group_id,
                              window.host.editor.find_token(identifier)[1]) for identifier in paths}
    recent = list(window.document_state.recent_files)
    storage = window.manager.storage_dir
    assert window.close()
    payload = json.loads(window.document_state.store.path.read_text("utf-8"))
    assert payload["schema_version"] == 1
    assert {item["id"]: item["view_state"] for item in payload["open_documents"]} == expected
    assert all(path.read_bytes() == saved[identifier] for identifier, path in paths.items())
    restored = windows(storage=storage, restore=True, visible=False)
    assert restored.active_session.id == sessions[2].id
    for identifier in order:
        assert_snapshot(restored.sessions.get(identifier).canvas, expected[identifier])
    restored.resize(1160, 780)
    restored.show()
    QApplication.processEvents()
    assert [session.id for session in restored.sessions.ordered()] == order
    assert restored.document_state.recent_files == recent
    assert restored.manager.theme.name == theme and restored.manager.theme.compact == compact
    for identifier in order:
        session = restored.sessions.get(identifier)
        if session.canvas.view.isVisible():
            qtbot.waitUntil(lambda: not session.canvas._view_reset_pending)
        group, index = restored.host.editor.find_token(identifier)
        assert (group.group_id, index) == locations[identifier]
        assert_snapshot(session.canvas, expected[identifier])
        restored.manager.activate_document(identifier)
        QApplication.processEvents()
        qtbot.waitUntil(lambda: not session.canvas._view_reset_pending)
        assert_snapshot(session.canvas, expected[identifier])
        assert restored.panels.outline.tree_model.document.root_id == session.canvas.document_view.root_id
        assert restored._favorites_stack.currentWidget() is session.favorites
        assert session.favorites.model.current_page_id == expected[identifier]["page_id"]
        assert not session.engine.is_dirty and not session.engine.can_undo and not session.engine.can_redo
        assert paths[identifier].read_bytes() == saved[identifier]


def test_manual_reopen_still_starts_from_main_after_startup_restore(windows, tmp_path):
    window = windows()
    session, pages = open_pages(window, tmp_path / "手动重开.mindmap")
    expected = position(session, pages[2], 2)
    storage, path = window.manager.storage_dir, session.files.path
    assert window.close()
    restarted = windows(storage=storage, restore=True)
    assert_snapshot(restarted.canvas, expected)
    assert restarted.manager.close_document(session.id)
    assert restarted.open_path(path)
    QApplication.processEvents()
    assert restarted.canvas.current_page_id is None
    assert restarted.canvas.view.transform().isIdentity()
    assert restarted.canvas.selected_node_ids() == [restarted.engine.root_id]


def test_legacy_and_corrupt_view_fields_do_not_prevent_other_files_restoring(windows, tmp_path):
    window = windows()
    sessions, page_ids = [], []
    for index in range(4):
        session, pages = open_pages(window, tmp_path / f"兼容{index}.mindmap")
        position(session, pages[2], 2)
        sessions.append(session)
        page_ids.append(pages[2])
    storage, state_path = window.manager.storage_dir, window.document_state.store.path
    assert window.close()
    payload = json.loads(state_path.read_text("utf-8"))
    records = payload["open_documents"]
    records[0].pop("view_state")
    records[1]["view_state"]["center"] = ["损坏", 0]
    records[2]["view_state"] = []
    records[3]["view_state"]["page_id"] = "外部已删除的页面"
    state_path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    restarted = windows(storage=storage, restore=True)
    assert len(restarted.sessions) == 4
    for index, old in enumerate(sessions):
        canvas = restarted.sessions.get(old.id).canvas
        assert canvas.current_page_id == (page_ids[1] if index == 1 else None)
        restarted.manager.activate_document(old.id)
        QApplication.processEvents()
        assert canvas.view.transform().isIdentity()
        assert canvas.selected_node_ids() == [canvas.document_view.root_id]
    assert "未能恢复" not in restarted.status.currentMessage()


@pytest.mark.parametrize("remove_all", [False, True])
def test_external_changes_filter_selection_and_renamed_pages_keep_identifiers(windows, tmp_path, remove_all):
    window = windows()
    session, pages = open_pages(window, tmp_path / "外部修改.mindmap")
    expected = position(session, pages[1], 2)
    storage, path = window.manager.storage_dir, session.files.path
    assert window.close()
    engine = MindMapEngine(load_document(path))
    root = engine.document.submaps[pages[1]].root_id
    engine.update_node(root, text="页面已经改名")
    # 删除普通兄弟时不影响嵌套页面；全部删除允许级联清理下级页。
    removed = expected["selection"] if remove_all else expected["selection"][-1:]
    engine.remove_nodes(removed)
    save_document(engine.document, path)
    saved = path.read_bytes()
    restarted = windows(storage=storage, restore=True)
    assert restarted.canvas.current_page_id == pages[1]
    assert restarted.canvas.document_view.root.text == "页面已经改名"
    assert restarted.canvas.selected_node_ids() == ([root] if remove_all else expected["selection"][:1])
    assert path.read_bytes() == saved


def test_external_page_removal_restores_main_without_failing_file(windows, tmp_path):
    window = windows()
    session, pages = open_pages(window, tmp_path / "删除页面.mindmap")
    position(session, pages[2], 2)
    storage, path = window.manager.storage_dir, session.files.path
    assert window.close()
    engine = MindMapEngine(load_document(path))
    engine.remove_submap(engine.document.submaps[pages[1]].owner_id)
    save_document(engine.document, path)
    restarted = windows(storage=storage, restore=True)
    assert restarted.canvas.current_page_id is None
    assert restarted.canvas.view.transform().isIdentity()
    assert restarted.canvas.selected_node_ids() == [restarted.engine.root_id]
    assert "未能恢复" not in restarted.status.currentMessage()


@pytest.mark.parametrize("failure", ["write", "ime"])
def test_rejected_exit_does_not_replace_last_successful_view_snapshot(windows, tmp_path, failure):
    window = windows()
    session, pages = open_pages(window, tmp_path / "退出失败.mindmap")
    position(session, pages[2], 2)
    storage, state_path = window.manager.storage_dir, window.document_state.store.path
    assert window.close()
    original = state_path.read_bytes()
    restarted = windows(storage=storage, restore=True)
    current = restarted.active_session
    current.canvas.navigate_to(pages[1])
    QApplication.processEvents()
    original_writer = current.files.writer
    if failure == "write":
        current.engine.update_node(current.canvas.document_view.root_id, text="保存失败的修改")
        current.timer.stop()

        def fail(*args, **kwargs):
            raise OSError("模拟保存失败")

        current.files.writer = fail
    else:
        current.canvas.begin_edit(current.canvas.document_view.root_id)
        current.canvas.editor.inputMethodEvent(QInputMethodEvent("候选", []))
    try:
        assert not restarted.close()
        assert state_path.read_bytes() == original
        assert not current.disposed and not restarted._disposed
    finally:
        current.files.writer = original_writer
        if current.canvas.editor is not None:
            current.canvas.editor.inputMethodEvent(QInputMethodEvent("", []))
            current.canvas.finish_edit(commit=False)
    expected = current.canvas.capture_page_view_state()
    assert restarted.close()
    payload = json.loads(state_path.read_text("utf-8"))
    assert payload["open_documents"][0]["view_state"] == expected
    successful = state_path.read_bytes()
    restarted.close()
    restarted.dispose()
    assert state_path.read_bytes() == successful


def test_save_as_records_current_view_for_new_path(windows, tmp_path, monkeypatch):
    window = windows()
    session, pages = open_pages(window, tmp_path / "原文件.mindmap")
    expected = position(session, pages[3], 3)
    original = session.files.path.read_bytes()
    target = tmp_path / "另存为.mindmap"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    assert session.files.save(save_as=True)
    storage = window.manager.storage_dir
    assert window.close()
    restarted = windows(storage=storage, restore=True)
    assert restarted.files.path == target
    assert_snapshot(restarted.canvas, expected)
    assert (tmp_path / "原文件.mindmap").read_bytes() == original


def test_document_state_owns_nested_snapshot_and_rolls_back_failed_commit(tmp_path, monkeypatch):
    state = DocumentState(tmp_path / "documents-v1.json")
    record = {"id": "mindmap." + "a" * 32, "path": str(tmp_path / "文件.mindmap"),
              "view_state": {"page_id": "页面", "zoom": 1.5, "center": [10, 20],
                             "selection": ["节点"], "focus_id": None}}
    expected = deepcopy(record)
    state.save_session([record], record["id"])
    saved = state.store.path.read_bytes()
    record["view_state"]["center"].clear()
    record["view_state"]["selection"].append("之后的修改")
    assert state.open_documents == [expected]
    loaded = DocumentState(state.store.path)
    loaded.load()
    assert loaded.open_documents == [expected]

    def fail(payload):
        raise OSError("模拟配置写入失败")

    monkeypatch.setattr(state.store, "save", fail)
    with pytest.raises(OSError, match="配置写入失败"):
        state.save_session([], "")
    assert state.open_documents == [expected] and state.active_id == expected["id"]
    assert state.store.path.read_bytes() == saved


@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
@pytest.mark.parametrize("no_restore", [False, True])
def test_real_launcher_explicit_file_preserves_restored_view_unless_disabled(
        windows, tmp_path, scale, no_restore):
    window = windows()
    session, pages = open_pages(window, tmp_path / "命令行页面.mindmap")
    expected = position(session, pages[2], 2)
    storage, path = window.manager.storage_dir, session.files.path
    assert window.close()
    original = path.read_bytes()
    output = tmp_path / "启动结果.json"
    source = textwrap.dedent("""\
        import json
        import sys
        from pathlib import Path
        from mindmap_workbench import shell_adapter
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        original = shell_adapter.build_window
        def capture(**options):
            window = original(**options)
            def finish():
                payload = {"view": window.canvas.capture_page_view_state(),
                           "count": len(window.sessions), "path": str(window.files.path),
                           "dirty": window.engine.is_dirty, "undo": window.engine.can_undo}
                Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
                QApplication.instance().quit()
            QTimer.singleShot(50, finish)
            return window
        shell_adapter.build_window = capture
        raise SystemExit(shell_adapter.run(sys.argv[2:]))
        """)
    arguments = ["--state-dir", str(storage), str(path)]
    if no_restore:
        arguments.insert(0, "--no-restore")
    result = subprocess.run(
        [sys.executable, "-c", source, str(output), *arguments],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale, PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text("utf-8"))
    assert payload["count"] == 1 and payload["path"] == str(path)
    assert not payload["dirty"] and not payload["undo"]
    if no_restore:
        assert payload["view"]["page_id"] is None and payload["view"]["zoom"] == 1
    else:
        actual = payload["view"]
        assert actual["page_id"] == expected["page_id"]
        assert actual["selection"] == expected["selection"]
        assert actual["zoom"] == pytest.approx(expected["zoom"])
        assert (QPointF(*actual["center"]) - QPointF(*expected["center"])).manhattanLength() < 3 / expected["zoom"]
    assert path.read_bytes() == original
