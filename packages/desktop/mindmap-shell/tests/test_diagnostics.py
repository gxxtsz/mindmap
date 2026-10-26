"""验证无控制台启动时仍能保存底层错误，诊断退出不会污染宿主钩子。"""

import logging
import sys

from mindmap_shell import AppSpec, WorkbenchWindow
from mindmap_shell.diagnostics import runtime_diagnostics
from PySide6.QtCore import qInstallMessageHandler, qWarning


def test_runtime_log_records_qt_and_python_failures_and_restores_hooks(qtbot, tmp_path, monkeypatch):
    """检查真正的 Qt 消息、异常堆栈和窗口心跳，并核验两个旧处理器均恢复。"""
    window = WorkbenchWindow(AppSpec("diagnostic-test", "诊断验收", storage_dir=tmp_path))
    qtbot.addWidget(window)
    qt_messages, exceptions = [], []

    def old_qt(kind, context, message):
        qt_messages.append(message)

    def old_exception(kind, error, traceback):
        exceptions.append(str(error))

    previous_qt = qInstallMessageHandler(old_qt)
    monkeypatch.setattr(sys, "excepthook", old_exception)
    logger = logging.getLogger("mindmap_shell")
    original_level, original_handlers = logger.level, list(logger.handlers)
    try:
        with runtime_diagnostics(window):
            qWarning("绘图失败验收：BitBlt failed")
            try:
                raise RuntimeError("回调异常验收")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        assert sys.excepthook is old_exception
        assert exceptions == ["回调异常验收"]
        qWarning("退出诊断后仍使用原处理器")
        content = (tmp_path / "logs" / "runtime.log").read_text("utf-8")
        assert "绘图失败验收：BitBlt failed" in content
        assert "Python 未处理异常" in content and "RuntimeError: 回调异常验收" in content
        assert "窗口状态" in content and "进程=" in content
        assert "退出诊断后仍使用原处理器" not in content
        assert qt_messages[-2:] == ["绘图失败验收：BitBlt failed", "退出诊断后仍使用原处理器"]
        assert logger.level == original_level and logger.handlers == original_handlers
    finally:
        qInstallMessageHandler(previous_qt)
        window.close()


def test_unwritable_log_path_does_not_prevent_running(qtbot, tmp_path):
    """日志路径被文件占用时，事件循环主体仍能执行，异常钩子保持原值。"""
    window = WorkbenchWindow(AppSpec("diagnostic-path-test", "路径验收", storage_dir=tmp_path))
    qtbot.addWidget(window)
    (tmp_path / "logs").write_text("占用路径", encoding="utf-8")
    original = sys.excepthook
    with runtime_diagnostics(window):
        assert sys.excepthook is original
    window.close()


def test_pythonw_without_standard_error_still_records_failures(qtbot, tmp_path, monkeypatch):
    """模拟正式GUI启动器没有标准错误流的环境，错误仍落盘且不调用失效的默认输出。"""
    window = WorkbenchWindow(AppSpec("diagnostic-pythonw-test", "无控制台验收", storage_dir=tmp_path))
    qtbot.addWidget(window)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", None)
        patch.setattr(sys, "excepthook", sys.__excepthook__)
        with runtime_diagnostics(window):
            qWarning("无控制台绘图警告")
            sys.excepthook(RuntimeError, RuntimeError("无控制台回调异常"), None)
        assert sys.excepthook is sys.__excepthook__
    content = (tmp_path / "logs" / "runtime.log").read_text("utf-8")
    assert "无控制台绘图警告" in content and "RuntimeError: 无控制台回调异常" in content
    window.close()
