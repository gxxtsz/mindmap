"""原生导图目录的后台解析及可取消等待；工作线程不持有画布或引擎。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mindmap_core import ValidatedDocument
from mindmap_core.formats import DirectoryVersion, load_validated_directory
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QProgressDialog, QWidget

# 小文件保持同步，避免线程调度与加载提示增加普通文件的打开成本。
BACKGROUND_OPEN_BYTES = 1024 * 1024
_LOAD_POOL = None


def _load_pool():
    """按需创建单工作线程并复用；只接收文件路径，不跨线程传递任何 Qt 对象。"""
    global _LOAD_POOL
    if _LOAD_POOL is None:
        _LOAD_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mindmap-load")
    return _LOAD_POOL


def load_native_in_background(
    path: Path, parent: QWidget | None,
) -> tuple[ValidatedDocument, DirectoryVersion] | None:
    """工作线程读取导图目录，GUI 线程以可取消的模态等待接收校验凭据和版本。

    工作线程只恢复未完成的写入、解析和校验。布局、图片解码、引擎接管及信号
    由返回后的 GUI 调用方执行。

    保持 open_path 的同步结果语义，但等待期间继续处理 Qt 事件，模态提示阻止
    对所属窗口发起重复操作。取消返回 None，后台任务若已开始则自行结束并丢弃
    结果，不提交文档或修改原文件；读取异常原样返回调用方处理。控件及轮询计时器
    只活到本次等待结束，不给后台任务连接 Qt 回调，取消后也不会访问已销毁的界面。
    """
    future = _load_pool().submit(load_validated_directory, path)
    dialog = QProgressDialog("正在打开导图…", "取消", 0, 0, parent)
    dialog.setWindowTitle("打开导图")
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumDuration(0)
    # 性能基准使用隐藏窗口，加载提示也保持隐藏，仍实际运行事件循环及布局。
    if parent is not None and parent.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: loop.quit() if future.done() else None)
    dialog.canceled.connect(loop.quit)
    destroyed = []
    if parent is not None:
        def parent_destroyed():
            destroyed.append(True)
            loop.quit()

        parent.destroyed.connect(parent_destroyed)
    try:
        dialog.show()
        timer.start()
        if not future.done():
            loop.exec()
        if destroyed or dialog.wasCanceled() or not future.done():
            future.cancel()
            return None
        return future.result()
    finally:
        timer.stop()
        if not destroyed:
            if parent is not None:
                parent.destroyed.disconnect(parent_destroyed)
            dialog.close()
            dialog.deleteLater()
