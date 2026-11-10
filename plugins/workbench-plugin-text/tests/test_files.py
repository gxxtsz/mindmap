"""文本插件的编码、共享内容、原子保存及输入法保护。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtWidgets import QApplication, QFileDialog
from workbench_plugin_text import manifest as text_manifest
from workbench_runtime import WorkbenchRuntimeWindow


def text_view(window, content=""):
    """通过实际文本插件登记文档，核验平台协调的真实生命周期。"""
    assert window.new_document(provider_id="text.editor")
    view = window.documents.active
    view.widget.insertPlainText(content)
    return view


def test_manual_text_save_and_untitled_cancel(window, tmp_path, monkeypatch):
    assert window.new_document(provider_id="text.editor")
    view = window.documents.active
    view.widget.insertPlainText("中文文本\n第二行")
    assert view.document.path is None and view.document.is_dirty
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    assert not window.save()
    assert view.document.path is None and view.document.is_dirty
    path = tmp_path / "文本.txt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    assert window.save()
    assert path.read_text("utf-8") == "中文文本\n第二行"
    assert not view.document.is_dirty
    view.widget.insertPlainText("改动")
    assert "改动" not in path.read_text("utf-8")


def test_text_duplicate_shares_history_and_survives_first_view_close(window, tmp_path):
    path = tmp_path / "文本.txt"
    path.write_text("原文", encoding="utf-8")
    assert window.open_path(path)
    first = window.documents.active
    assert first.duplicate()
    second = window.documents.active
    assert first.document is second.document
    second.widget.insertPlainText("新增")
    assert first.widget.toPlainText() == second.widget.toPlainText()
    first.widget.undo()
    assert second.widget.toPlainText() == "原文"
    second.widget.redo()
    assert window.manager.close_document(first.id)
    assert second.widget.toPlainText() == "新增原文"
    assert window.save()


@pytest.mark.parametrize("bom,newline", [(False, "\n"), (True, "\r\n"), (False, "\r")])
def test_text_encoding_roundtrip(window, tmp_path, bom, newline):
    path = tmp_path / "文本.md"
    raw = ("甲" + newline + "乙").encode("utf-8-sig" if bom else "utf-8")
    path.write_bytes(raw)
    assert window.open_path(path)
    assert window.save()
    assert path.read_bytes() == raw


def test_invalid_utf8_does_not_install_view(window, tmp_path):
    path = tmp_path / "损坏.txt"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError):
        window.open_path(path)
    assert not window.documents.views
    assert path.read_bytes() == b"\xff\xfe\x00"


def test_text_atomic_save_failure_preserves_disk_and_dirty_buffer(window, tmp_path, monkeypatch):
    import workbench_plugin_text.editor as module
    path = tmp_path / "文本.txt"
    path.write_text("原文", encoding="utf-8")
    assert window.open_path(path)
    view = window.documents.active
    view.widget.insertPlainText("新增")
    monkeypatch.setattr(module.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("写入失败")))
    assert not window.save()
    assert path.read_text("utf-8") == "原文"
    assert view.document.is_dirty and view.widget.toPlainText() == "新增原文"
    assert not list(tmp_path.glob(".workbench-text-*"))


def test_save_as_cannot_overwrite_another_open_document(window, tmp_path, monkeypatch):
    paths = [tmp_path / "甲.txt", tmp_path / "乙.txt"]
    for path in paths:
        path.write_text(path.stem, encoding="utf-8")
        assert window.open_path(path)
    view = window.documents.active
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(paths[0]), ""))
    assert not window.save(save_as=True)
    assert view.document.path == paths[1]
    assert paths[0].read_text("utf-8") == "甲"


def test_text_input_method_candidate_blocks_save_and_close(workspace, tmp_path, monkeypatch):
    view = text_view(workspace)
    QApplication.sendEvent(view.widget, QInputMethodEvent("zhong", []))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: pytest.fail("组合输入尚未完成"))
    assert not workspace.save()
    assert not workspace.manager.close_document(view.id)
    assert view.id in workspace.documents.views
    committed = QInputMethodEvent()
    committed.setCommitString("中文")
    QApplication.sendEvent(view.widget, committed)
    assert view.widget.toPlainText() == "中文" and view.document.is_dirty
    target = tmp_path / "中文.txt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert workspace.save() and target.read_bytes() == "中文".encode()


def test_text_save_as_rebinds_all_views_and_keeps_independent_selection(workspace, tmp_path, monkeypatch):
    source, target = tmp_path / "原文.txt", tmp_path / "副本.md"
    source.write_text("甲乙丙丁", encoding="utf-8")
    assert workspace.open_path(source)
    first = workspace.documents.active
    cursor = first.widget.textCursor()
    cursor.setPosition(1)
    cursor.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
    first.widget.setTextCursor(cursor)
    assert first.duplicate()
    second = workspace.documents.active
    cursor = second.widget.textCursor()
    cursor.clearSelection()
    cursor.setPosition(4)
    second.widget.setTextCursor(cursor)
    assert first.widget.textCursor().selectedText() == "乙丙"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert workspace.save(save_as=True)
    assert first.document.path == second.document.path == target
    assert first.document.resource_uri == target.as_uri()
    assert first.widget.textCursor().selectedText() == "乙丙"
    assert second.widget.textCursor().position() == 4
    assert workspace.open_path(target) and len(workspace.documents.views) == 2
    assert source.read_text("utf-8") == "甲乙丙丁"


def test_text_view_cursor_selection_and_split_restore_without_body(workspace, qtbot, tmp_path):
    path = tmp_path / "视图.md"
    path.write_text("0123456789" * 20, encoding="utf-8")
    assert workspace.open_path(path)
    first = workspace.documents.active
    assert first.duplicate()
    second = workspace.documents.active
    workspace.host.editor.split_token(second.id, Qt.Orientation.Horizontal)
    for view, anchor, position in ((first, 10, 20), (second, 50, 60)):
        cursor = view.widget.textCursor()
        cursor.setPosition(anchor)
        cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
        view.widget.setTextCursor(cursor)
    ids = (first.id, second.id)
    assert workspace.close()
    raw = (tmp_path / "配置" / "workbench-state-v1.json").read_text("utf-8")
    assert "0123456789" not in raw
    restored = WorkbenchRuntimeWindow(manifests=[text_manifest], storage_dir=tmp_path / "配置")
    qtbot.addWidget(restored, before_close_func=lambda item: item.dispose())
    restored.show()
    QApplication.processEvents()
    first, second = [restored.documents.views[key] for key in ids]
    assert first.document is second.document
    assert (first.widget.textCursor().anchor(), first.widget.textCursor().position()) == (10, 20)
    assert (second.widget.textCursor().anchor(), second.widget.textCursor().position()) == (50, 60)
    assert len(restored.host.editor.groups) == 2
