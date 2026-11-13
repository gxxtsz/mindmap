"""正式启动器的本机诊断日志，记录 Qt 绘图错误及 Python 未处理异常。"""

import logging
import os
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QTimer, QtMsgType, qInstallMessageHandler, qVersion
from PySide6.QtWidgets import QApplication


@contextmanager
def runtime_diagnostics(window):
    """在事件循环期间保存诊断，退出后恢复宿主原有的异常和 Qt 消息处理器。

    日志位于窗口自身配置目录的 logs/runtime.log，单文件最多 1 MiB，保留两份
    轮转备份。只记录运行环境、窗口状态和错误，不遍历或序列化用户的导图内容。
    配置目录不可写时继续启动；诊断失败不能阻止用户打开或保存导图。
    """
    try:
        path = window.manager.storage_dir / "logs" / "runtime.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    except OSError:
        yield
        return
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s：%(message)s"))
    logger = logging.getLogger("mindmap_shell")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    previous_exception = sys.excepthook

    def report_exception(kind, error, traceback):
        """捕获 Qt 事件回调中没有交给业务错误边界的异常，并保留宿主处理行为。"""
        logger.critical("Python 未处理异常", exc_info=(kind, error, traceback))
        # pythonw 没有标准错误流；默认处理器无法输出，但自定义处理器仍需调用。
        if previous_exception is not sys.__excepthook__ or sys.stderr is not None:
            previous_exception(kind, error, traceback)

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def report_qt(kind, context, message):
        """使用 logging 的线程锁写入 Qt 消息；回调内不操作 QWidget 或再次调用 Qt 日志。"""
        logger.log(levels.get(kind, logging.WARNING), "Qt[%s]：%s", context.category or "default", message)
        if previous_qt is not None:
            previous_qt(kind, context, message)
        elif sys.stderr is not None:
            print(message, file=sys.stderr)

    def record_window_state():
        """低频保留 GUI 心跳，帮助区分整窗未重绘与主线程停止处理事件。"""
        handle = window.windowHandle()
        logger.info(
            "窗口状态：可见=%s，最小化=%s，曝光=%s，允许更新=%s，尺寸=%sx%s，像素比=%.2f",
            window.isVisible(), window.isMinimized(), handle.isExposed() if handle else False,
            window.updatesEnabled(), window.width(), window.height(), window.devicePixelRatioF(),
        )

    previous_qt = qInstallMessageHandler(report_qt)
    sys.excepthook = report_exception
    timer = QTimer(window)
    timer.setInterval(30_000)
    timer.timeout.connect(record_window_state)
    try:
        logger.info("启动诊断：Python=%s，Qt=%s，平台=%s，进程=%s",
                    sys.version.split()[0], qVersion(), QApplication.platformName(), os.getpid())
        record_window_state()
        timer.start()
        yield
    finally:
        timer.stop()
        timer.deleteLater()
        logger.info("结束本次运行诊断")
        qInstallMessageHandler(previous_qt)
        sys.excepthook = previous_exception
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        handler.close()
