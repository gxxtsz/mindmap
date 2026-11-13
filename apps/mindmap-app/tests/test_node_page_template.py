"""保留原模板下载的取消、覆盖确认和写入失败保障，入口改为节点详情页。"""

import json

import pytest
from mindmap_app.app import build_window
from PySide6.QtCore import QSaveFile, QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox


@pytest.fixture
def window(qtbot, tmp_path):
    result = build_window(storage_dir=tmp_path / "配置", restore=False)
    qtbot.addWidget(result, before_close_func=lambda item: item.dispose())
    result.show()
    assert result.new_document(tmp_path / "主体.mindmap")
    view = result.documents.active
    assert view.pages.open_page(view.engine.root_id, "software-structure", create=True)
    return result


def test_download_template_confirms_overwrite_after_suffix_completion(window, tmp_path, monkeypatch):
    """用 Qt 的覆盖提示拒绝和接受同名文件，核对提示检查的是补全后的路径。"""
    target = tmp_path / "已有模板.software-structure"
    original = b"original"
    target.write_bytes(original)
    answers = iter((QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes))
    confirmations = []

    def choose_file(dialog):
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.selectFile(str(target.with_suffix("")))
        answer = next(answers)

        def answer_overwrite():
            prompt = dialog.findChild(QMessageBox)
            if prompt is not None:
                confirmations.append(prompt.text())
                prompt.button(answer).click()

        # accept 会启动确认对话框的事件循环，此时由定时器点击覆盖选项。
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(answer_overwrite)
        timer.start(0)
        dialog.accept()
        timer.stop()
        return dialog.result()

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    download = window.documents.active.pages._page.template_action
    download.trigger()
    assert target.read_bytes() == original
    download.trigger()
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == 1
    assert len(confirmations) == 2 and all(target.name in prompt for prompt in confirmations)


@pytest.mark.parametrize(("filename", "accepted", "message"), [
    ("取消.software-structure", False, None),
    ("扩展名错误.txt", True, "请使用 .software-structure 扩展名保存模板"),
])
def test_download_template_without_valid_destination(window, tmp_path, monkeypatch, filename, accepted, message):
    """取消保存或输入其他后缀时，目标目录中没有模板副本。"""
    original_log = window.operation_log.plain_text()

    def choose_file(dialog):
        dialog.selectFile(str(tmp_path / filename))
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    window.documents.active.pages._page.template_action.trigger()
    assert not any(tmp_path.glob("*.software-structure")) and not (tmp_path / filename).exists()
    if message:
        assert message in window.status.currentMessage() and message in window.operation_log.plain_text()
    else:
        assert window.operation_log.plain_text() == original_log


def test_download_template_write_failure_keeps_existing_file(window, tmp_path, monkeypatch):
    """模拟模板写入中断，原有内容得到保留，宿主同时报告失败。"""
    target = tmp_path / "已有模板.software-structure"
    original = b"original"
    target.write_bytes(original)
    write = QSaveFile.write

    def choose_file(dialog):
        dialog.selectFile(str(target))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QFileDialog, "exec", choose_file)
    monkeypatch.setattr(QSaveFile, "write", lambda file, content: write(file, content[:1]))
    window.documents.active.pages._page.template_action.trigger()

    assert target.read_bytes() == original
    assert {item for item in tmp_path.iterdir() if item.is_file()} == {target, tmp_path / "主体.mindmap"}
    message = "保存软件结构模板失败"
    assert message in window.status.currentMessage() and str(target) in window.status.currentMessage()
    assert message in window.operation_log.plain_text()
