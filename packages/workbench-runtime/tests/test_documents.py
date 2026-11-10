"""通用文档去重、保存全部与两阶段关闭事务。"""

import json

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox
from workbench_api import OperationResult
from workbench_runtime import WorkbenchRuntimeWindow


def sample_view(window, content=""):
    """通过测试插件登记文档，核验宿主协调的生命周期。"""
    assert window.new_document(provider_id="sample.editor")
    view = window.documents.active
    view.widget.insertPlainText(content)
    return view


def test_batch_close_cancel_retains_discarded_document_and_all_tabs(window, monkeypatch):
    views = []
    for text in ("甲", "乙"):
        views.append(sample_view(window, text))
    answers = iter([QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Cancel])
    monkeypatch.setattr(window, "confirm_close", lambda view: next(answers))
    assert not window.manager.close_documents([view.id for view in views])
    assert len(window.documents.views) == 2
    assert [view.widget.toPlainText() for view in views] == ["甲", "乙"]
    assert all(view.document.is_dirty for view in views)


def test_save_all_deduplicates_and_stops_at_cancelled_document(workspace, monkeypatch):
    first = sample_view(workspace, "第一份")
    first.duplicate()
    second = sample_view(workspace, "第二份")
    third = sample_view(workspace, "第三份")
    calls = []

    def save_first(**kwargs):
        calls.append(first.document.id)
        return OperationResult()

    def save_second(**kwargs):
        calls.append(second.document.id)
        return OperationResult.cancelled()

    monkeypatch.setattr(first.document, "save", save_first)
    monkeypatch.setattr(second.document, "save", save_second)
    monkeypatch.setattr(third.document, "save", lambda **kw: pytest.fail("取消后必须停止"))
    assert not workspace.save_all()
    assert calls == [first.document.id, second.document.id]
    assert workspace.documents.active is second


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Discard,
                                    QMessageBox.StandardButton.Cancel])
def test_last_view_close_decision_and_no_untitled_body_in_session(workspace, tmp_path, monkeypatch, answer):
    view = sample_view(workspace, "未保存的正文")
    assert view.duplicate()
    duplicate = workspace.documents.active
    monkeypatch.setattr(workspace, "confirm_close", lambda view: pytest.fail("仍有视图时不能询问"))
    assert workspace.manager.close_document(view.id)
    target = tmp_path / "保存.example"
    monkeypatch.setattr(workspace, "confirm_close", lambda view: answer)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert workspace.manager.close_document(duplicate.id) is (answer != QMessageBox.StandardButton.Cancel)
    assert target.exists() is (answer == QMessageBox.StandardButton.Save)
    if answer == QMessageBox.StandardButton.Cancel:
        assert duplicate.widget.toPlainText() == "未保存的正文" and duplicate.document.is_dirty
    else:
        assert workspace.close()
        payload = (tmp_path / "配置" / "workbench-state-v1.json").read_text("utf-8")
        assert "未保存的正文" not in payload
        assert json.loads(payload)["views"] == []


def test_exit_save_failure_preserves_all_targets_and_discarded_buffers(workspace, tmp_path, monkeypatch):
    first = sample_view(workspace, "已决定放弃")
    second = sample_view(workspace, "保存失败仍保留")
    answers = iter([QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Save])
    monkeypatch.setattr(workspace, "confirm_close", lambda view: next(answers))
    monkeypatch.setattr(second.document, "save", lambda **kw: OperationResult.failed("磁盘写入失败"))
    assert not workspace.close()
    assert set(workspace.documents.views) == {first.id, second.id}
    assert first.widget.toPlainText() == "已决定放弃" and first.document.is_dirty
    assert workspace.documents.active is second
    assert not (tmp_path / "配置" / "workbench-state-v1.json").exists()


def test_corrupt_settings_preserve_bytes_and_do_not_block_runtime(qtbot, tmp_path, sample_manifest):
    path = tmp_path / "workbench-settings-v1.json"
    path.write_bytes(b"invalid settings")
    window = WorkbenchRuntimeWindow(storage_dir=tmp_path, manifests=[sample_manifest])
    qtbot.addWidget(window, before_close_func=lambda item: item.dispose())
    assert sample_view(window).title == "未命名"
    assert next(tmp_path.glob("workbench-settings-invalid-*.json")).read_bytes() == b"invalid settings"


def test_platform_rejects_second_document_for_normalized_resource(workspace, tmp_path):
    """即使提供者忘记复用内容，平台登记边界仍保证一份规范化资源只有一个文档。"""
    path = tmp_path / "共享.example"
    path.write_text("已有内容", encoding="utf-8")
    assert workspace.open_path(path)
    original = workspace.documents.active
    plugin = workspace.plugins.instance("sample")
    with pytest.raises(ValueError, match="已有共享文档"):
        plugin.open(path.parent / "." / path.name)
    assert list(workspace.documents.views.values()) == [original]
    assert original.widget.toPlainText() == "已有内容"
