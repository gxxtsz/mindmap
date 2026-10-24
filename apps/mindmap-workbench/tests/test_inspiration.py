"""工作台灵感区自动保存、页面恢复与多文件隔离。"""

import pytest
from mindmap_formats import load_document
from mindmap_workbench.shell_adapter import build_window
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication


@pytest.fixture
def windows(qtbot, tmp_path):
    opened = []

    def create(restore=False):
        window = build_window(storage_dir=tmp_path / "配置", restore=restore)
        qtbot.addWidget(window)
        opened.append(window)
        window.show()
        QApplication.processEvents()
        return window

    yield create
    for window in reversed(opened):
        window.close()


def test_autosave_undo_and_file_isolation(windows, tmp_path, qtbot, caplog):
    window = windows()
    path = tmp_path / "第一份.mindmap"
    assert window.new_document(path)
    session = window.active_session
    e, canvas = session.engine, session.canvas
    branch = e.add_child(e.root_id, "待整理")
    child = e.add_child(branch, "全部随行")
    detail = e.create_detail_page(branch)
    canvas.select_nodes([branch])
    canvas.move_selected_to_inspiration()
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    loaded = load_document(path)
    assert loaded == e.document
    assert loaded.nodes[branch].detail_page_id == detail
    assert child in canvas.document_view.nodes
    e.undo()
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert load_document(path).root.children == [branch]
    e.redo()
    qtbot.waitUntil(lambda: not e.is_dirty, timeout=5000)
    assert window.new_document(tmp_path / "第二份.mindmap")
    assert not window.engine.document.inspiration.root_ids
    window.manager.activate_document(session.id)
    assert window.canvas.current_page_id == e.inspiration_id
    assert window.panels.outline.tree_model.rowCount() == 1
    assert not any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.parametrize("in_detail", [False, True])
def test_restart_restores_inspiration_and_owned_detail(windows, tmp_path, qtbot, in_detail):
    window = windows()
    assert window.new_document(tmp_path / "恢复.mindmap")
    canvas, e = window.canvas, window.engine
    root = e.add_inspiration("重新找回", position=(-200, 100))
    detail = e.create_detail_page(root)
    canvas.navigate_to(detail if in_detail else e.inspiration_id)
    QApplication.processEvents()
    canvas.view.zoom(1.3)
    canvas.view.update_navigation_rect(center=QPointF(50, 60))
    expected = canvas.capture_page_view_state()
    assert window.close()
    restored = windows(restore=True)
    qtbot.waitUntil(lambda: not restored.canvas._view_reset_pending)
    actual = restored.canvas.capture_page_view_state()
    assert actual["page_id"] == expected["page_id"]
    assert actual["zoom"] == pytest.approx(expected["zoom"])
    assert actual["selection"] == expected["selection"]
    assert restored.engine.document.inspiration.positions[root] == (-200, 100)
