"""导图共享文档：持有引擎、文件状态、撤销历史和后台保存任务。"""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from pathlib import Path
from uuid import uuid4

from mindmap_core import MindMapEngine, MindMapError, ValidatedDocument
from mindmap_core.formats import (
    DirectoryVersion,
    DirectoryVersionConflict,
    can_process_snapshot,
    directory_size,
    directory_version,
    save_directory,
)
from mindmap_desktop_api import OperationResult, SavePolicy
from mindmap_qt_editor.canvas import MindMapFileState
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QMessageBox

from .background_save import SavedSnapshot, save_snapshot
from .operation_log import describe_change
from .recent_pages import RecentPageHistory

# 延迟启动一个独立进程，避免编码、深拷贝及垃圾回收持有 GUI 的解释器锁。
_SAVE_POOL = None
BACKGROUND_SAVE_NODE_LIMIT = 1000


def _save_pool():
    """首次大图保存时才建立共享进程，Windows 使用无控制台解释器运行后台任务。"""
    global _SAVE_POOL
    if _SAVE_POOL is None:
        context = get_context("spawn")
        # 与 multiprocessing 绕过 venv 重定向器的规则一致，使用真实运行时的 pythonw。
        # 父进程的模块搜索路径会由 spawn 传入，后台进程仍能加载当前工作区包。
        interpreter = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
        if sys.platform == "win32" and not getattr(sys, "frozen", False) and interpreter.exists():
            context.set_executable(str(interpreter))
        _SAVE_POOL = ProcessPoolExecutor(max_workers=1, mp_context=context)
    return _SAVE_POOL


def _discard_save_pool(executor):
    """工作进程异常退出后丢弃损坏的池；下一次显式请求或编辑可以重新建立进程。"""
    global _SAVE_POOL
    if _SAVE_POOL is executor:
        _SAVE_POOL = None
        executor.shutdown(wait=False, cancel_futures=True)


class MindMapDocumentHandle(QObject):
    """一份文件的共享文档及保存协调器，最后一个视图释放后才结束生命周期。

    引擎、路径、保存错误和后台任务均只有一份；画布和面板由各标签拥有。所有
    方法在 GUI 线程执行，文件操作不借用全局活动画布，关闭原标签也不会中止
    其他视图的防抖保存。视图通过 attach/detach 显式维护引用与信号连接。
    """

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.id = "document." + uuid4().hex
        self.engine = MindMapEngine()
        self.file_state = MindMapFileState()
        self.views = []
        self.recent_pages = RecentPageHistory(self)
        self.last_error = ""
        self._file_size_bytes = None
        self.disposed = False
        self._preparing = False
        self._git_editable = None
        self._unsubscribe = lambda: None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(500)
        self.timer.timeout.connect(lambda: self.autosave(background=True))
        self._background_save = None
        self._save_poll = QTimer(self)
        self._save_poll.setInterval(15)
        self._save_poll.timeout.connect(self._finish_background_save)

    save_policy = SavePolicy.AUTOMATIC

    @property
    def path(self):
        """平台只读访问共享路径；实际改绑仍由文件控制器在保存成功后执行。"""
        return self.file_state.path

    @property
    def is_dirty(self):
        return self.engine.is_dirty

    @property
    def is_writing(self) -> bool:
        """后台写入尚未由 GUI 线程结算时返回 True。多个标签共享同一状态。"""
        return self._background_save is not None

    @property
    def git_busy(self) -> bool:
        """在 Git 本地写入和重载期间为 True；仅查询和联网时不锁定文档。"""
        return self._git_editable is not None

    @property
    def resource_uri(self):
        return self.path.as_uri() if self.path else "untitled:" + self.id

    def save(self, *, save_as=False, reason="explicit"):
        """将既有导图保存结果转换为通用协议，保留输入法和后台快照保护。"""
        if self.git_busy:
            return OperationResult.cancelled("正在同步导图，请等操作完成")
        view = self.representative
        result = (view.files.save(save_as=True) if save_as
                  else self.flush(view))
        if result:
            if reason == "explicit":
                self.owner.window.record_operation(f"{'另存为' if save_as else '保存导图'}：{self.path}", session=view)
            elif reason == "close":
                self.owner.window.flush_recent_pages()
            return OperationResult()
        return OperationResult.failed(self.last_error) if self.last_error else OperationResult.cancelled()

    @property
    def representative(self):
        """取该文件当前活动视图，否则使用首个关联视图；不激活或重排标签。"""
        active = self.owner.active
        return active if active in self.views else self.views[0]

    @property
    def files(self):
        """借用存活视图的文件控制器；各控制器使用相同引擎、路径与写入边界。"""
        return self.representative.files

    def attach(self, view):
        """连接已初始化视图的文件通知并更新共享模式，不读盘或创建历史记录。"""
        self.views.append(view)
        view.files.error_occurred.connect(self._failed)
        view.files.state_changed.connect(self._saved)
        view.files.wait_for_pending_write = lambda: self._finish_background_save(wait=True)
        view.files.prepare_save = self.prepare_inputs
        view.files.check_save_target = self._check_target_owner
        self._unsubscribe()
        # 文档级状态在所有画布消费差量后刷新，防止侧栏读取仍未同步的旧选择。
        self._unsubscribe = self.engine.subscribe(self._changed)
        for item in self.views:
            item.canvas.shared_views = len(self.views) > 1

    def begin_git_write(self) -> bool:
        """暂缓自动保存和编辑，直到 Git 写入和可能的重载结束。

        调用方须确认各视图的输入已完成。手动同步可先提交输入，定时同步会跳过
        正在编辑的文档。锁定时保存每个标签的编辑权限并暂缓自动保存，完成时逐一恢复。
        """
        if self.disposed or self.git_busy or self.is_dirty or self.is_writing or self.file_state.conflict:
            return False
        self.timer.stop()
        self._git_editable = [(view, view.canvas.editable) for view in self.views]
        for view, _ in self._git_editable:
            view.close_popups()
            view.canvas.set_editable(False)
        self.sync_views()
        return True

    def finish_git_write(self) -> None:
        """恢复同步前的编辑权限。发生磁盘冲突时继续停用自动保存。"""
        if self._git_editable is None:
            return
        editable, self._git_editable = self._git_editable, None
        if not self.disposed:
            for view, allowed in editable:
                if not view.disposed:
                    view.canvas.set_editable(allowed)
            if self.is_dirty and not self.file_state.conflict:
                self.timer.start()
            self.sync_views()

    def detach(self, view):
        """只断开指定视图；保留其他视图的保存任务，最后一个视图负责收束后台写入。"""
        if len(self.views) == 1:
            self.dispose()
        view.files.error_occurred.disconnect(self._failed)
        view.files.state_changed.disconnect(self._saved)
        view.files.prepare_save = None
        view.files.wait_for_pending_write = None
        view.files.check_save_target = None
        self.views.remove(view)
        for item in self.views:
            item.canvas.shared_views = len(self.views) > 1

    def sync_views(self):
        """把同一份路径、脏状态和错误同步到全部关联标签，不修改各自的视图状态。"""
        for view in tuple(self.views):
            self.owner.sync(view)

    def prepare_inputs(self) -> bool:
        """先检查全部关联标签的输入法候选，再按标签顺序提交有效草稿。

        候选未完成返回 False 并定位相应标签，不提交其他标签。重入时复用正在
        进行的准备，避免属性提交触发文件状态回调后再次遍历。不会自行写盘。
        """
        if self.disposed:
            return False
        if self._preparing:
            return True
        views = [view for view in self.owner.ordered() if view.document is self]
        views.extend(view for view in self.views if view not in views)
        for view in views:
            if view.is_composing:
                self.owner.window.show_status_message("请先完成输入法候选，再保存或关闭导图")
                if self.owner.get(view.id) is view:
                    self.owner.window.context.documents.activate_view(view.id)
                return False
        self._preparing = True
        try:
            for view in views:
                view.commit_input()
        finally:
            self._preparing = False
        return True

    def flush(self, view) -> bool:
        """提交导图所有视图的输入并保存一次；失败保留文档、所有标签及原路径。"""
        if not self.prepare_inputs():
            return False
        self.timer.stop()
        saved = view.files.save_committed()
        if saved:
            self._saved()
        return saved

    def reload_after_conflict(self) -> bool:
        """用户确认后重载共享文件。取消或加载失败均保留内存中的导图与冲突标志。"""
        if self.git_busy or not self.file_state.conflict or not self.prepare_inputs():
            return False
        if self.engine.is_dirty and QMessageBox.question(
            self.owner.window.context.ui.parent, "重新加载导图",
            "要放弃这份导图在导图桌面中的修改，并读取磁盘上的版本吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return False
        return self.reload_preserving_views()

    def reload_preserving_views(
        self, *, prepared: tuple[ValidatedDocument, DirectoryVersion] | None = None,
    ) -> bool:
        """完整校验磁盘后重载共享引擎，再按稳定页面 ID 恢复各标签的视口。

        调用方先确认各标签的输入已完成。传入 prepared 时，文件控制器使用后台
        读取到的校验凭据和目录版本，在 GUI 线程复核文件并接管文档。失效页面
        由画布恢复流程回退到主图。校验失败或取消时保留原文档和各标签视图。
        """
        snapshots = [(view, view.capture_state()) for view in self.views]
        if not self.files.reload(prepared=prepared):
            return False
        for view, state in snapshots:
            if not view.disposed:
                view.restore_state(state)
        return True

    def restore_missing(self) -> bool:
        """显式恢复丢失文件，先提交关联视图输入，再由文件控制器保护目标路径。"""
        return (not self.git_busy and self.file_state.conflict and self.prepare_inputs()
                and self.files.restore_missing())

    def dispose(self):
        """幂等释放文件级计时器和订阅；调用时最后一个视图的控制器仍须存活。"""
        if self.disposed:
            return
        self.owner.window.flush_recent_pages()
        self.timer.stop()
        self._finish_background_save(wait=True)
        self.disposed = True
        self.owner.window.git_sync.track_document(self)
        self.timer.stop()
        self._save_poll.stop()
        self._unsubscribe()
        self.deleteLater()

    def _write(self, document, path, **kwargs):
        """检查目标归属后，原子提交原生完整快照或目录更新，防止跨会话覆盖。"""
        self._check_target_owner(path)
        return save_directory(document, path, **kwargs)

    def _check_target_owner(self, path):
        other = self.owner.find_path(path)
        if other is not None and other.document is not self:
            raise MindMapError("该导图目录已在另一个标签打开，请选择其他保存位置")

    def _changed(self, event):
        """保存事件不重新启动计时器；修改、撤销和重做合并为一次延迟写入。"""
        if not self.disposed and event.kind in {"change", "undo", "redo"}:
            if not self.file_state.conflict and not self.git_busy:
                self.timer.start()
            if self.owner.get(self.representative.id) is self.representative:
                self.owner.window.record_operation(describe_change(event), session=self.representative)
        if not self.disposed and event.kind != "saved":
            self.recent_pages.refresh()
        self.sync_views()

    def _refresh_file_size(self) -> None:
        """在文件操作或会话激活边界刷新实际字节数，不估算内存中尚未保存的内容。

        只读取当前绑定路径的磁盘元信息，整份导图的全部页面和图片均计入大小。
        未绑定路径、文件缺失或元信息读取失败时缓存 None，由状态栏显示“未知”；
        读取失败不修改保存错误、文档脏状态或自动保存计时器，也不弹出额外提示。
        """
        try:
            self._file_size_bytes = directory_size(self.path) if self.path is not None else None
        except (MindMapError, OSError):
            self._file_size_bytes = None

    def _saved(self):
        """打开或保存成功后更新磁盘大小及原有状态；后台保存也复用此通知边界。"""
        self._refresh_file_size()
        self.last_error = ""
        if self.engine.is_dirty and not self.disposed:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
        for view in self.views:
            self.owner._saved_path(view)
        self.owner.window.git_sync.document_saved(self)
        self.sync_views()

    def _failed(self, message):
        """失败保留原始状态且停止重试；显示文件名，下一次编辑或显式保存可再次尝试。"""
        if self.disposed:
            return
        self._refresh_file_size()
        self.timer.stop()
        name = self.files.path.name if self.files.path else "新建导图"
        hint = "，请在「文件」菜单处理保存冲突" if self.file_state.conflict else ""
        self.last_error = message + hint
        self.owner.window.show_status_message(f"{name}：{self.last_error}")
        self.owner.window.record_operation(f"保存导图失败：{message}", session=self.representative, level="error")
        self.sync_views()

    def autosave(self, *, background=False):
        """保存已提交版本，不结束编辑、移动焦点或提交草稿。

        计时器对大图启用后台写入。界面线程捕获稳定快照并准备受影响的目录成员，
        后台进程负责编码及写入。
        直接调用默认同步，保留显式保存、测试及宿主调用的成功/失败返回语义。
        自定义写入器及含有扩展实体的快照继续同步保存。
        """
        if self.disposed or self.file_state.conflict or self.git_busy:
            return False
        if (background and self.engine.node_count > BACKGROUND_SAVE_NODE_LIMIT
                and self.engine.is_dirty and self.files.writer == self._write):
            try:
                if self._background_save is not None:
                    return True
                if self.files.path is None:
                    raise MindMapError("导图尚未选择保存位置")
                target = self.files.validate_save_path(self.files.path)
                self._check_target_owner(target)
                expected = self.files.expected_write_version(target)
                snapshot, payload = self.files.capture_directory_save(target, expected)
                if not can_process_snapshot(snapshot.document):
                    return self.files.save_committed()
                executor = _save_pool()
                try:
                    future = executor.submit(save_snapshot, payload, target, expected_version=expected)
                except BrokenProcessPool:
                    _discard_save_pool(executor)
                    raise MindMapError("后台保存进程不可用，请重新保存") from None
                self._background_save = future, snapshot, target, executor
                self._save_poll.start()
                self.sync_views()
                return True
            except (MindMapError, OSError) as exc:
                self.files.report_error(exc)
                return False
        saved = self.files.save_committed()
        if saved and self.last_error:
            # 撤销回已保存快照时可以无需写盘，但旧错误也应清除，避免干净标签
            # 一直显示失败。实际写盘成功则已经由 state_changed 清除了错误。
            self._saved()
        return saved

    def _finish_background_save(self, *, wait=False):
        """GUI 线程接收完成结果，标记实际落盘的历史版本；过期文档结果不改保存位置。

        显式保存、另存、替换或关闭会等待旧任务结束，确保旧文件写入不会晚于新
        版本覆盖。后台期间的新编辑保持未保存状态，并继续安排下一次防抖写入。
        """
        pending = self._background_save
        if pending is None:
            return True
        future, snapshot, target, executor = pending
        if not wait and not future.done():
            return False
        self._background_save = None
        self._save_poll.stop()
        try:
            result = future.result()
            version = result.version if isinstance(result, SavedSnapshot) else result
            if not isinstance(version, DirectoryVersion):
                version = directory_version(target)
            if version is None:
                raise DirectoryVersionConflict("原导图目录已移走或删除")
        except Exception as exc:
            if isinstance(exc, BrokenProcessPool):
                _discard_save_pool(executor)
            if not self.disposed:
                error = exc if isinstance(exc, DirectoryVersionConflict) else f"后台保存失败：{exc}"
                self.files.report_error(error)
            return False
        if not self.disposed and self.files.path == target:
            self.files.accept_directory_version(target, version, snapshot=snapshot, written=True)
        return True


