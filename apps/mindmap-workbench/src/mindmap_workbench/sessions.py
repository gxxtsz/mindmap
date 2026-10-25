"""多导图会话：文件级共享保存、标签级独立视图、激活切换和确定性资源释放。"""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from pathlib import Path
from uuid import uuid4

from mindmap_core import MindMapEngine, MindMapError
from mindmap_docking import DocumentSpec
from mindmap_formats import can_process_snapshot, save_document, supports_process_save
from mindmap_qt_canvas import MindMapFileController, MindMapFileState, MindMapWidget
from mindmap_qt_panels import InspirationPanel, MindMapPanels, SubmapFavoritesPanel
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from .background_save import save_snapshot
from .document_state import path_key
from .operation_log import describe_change

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


class MindMapDocumentSession(QObject):
    """一份文件的共享文档及保存协调器，最后一个视图释放后才结束生命周期。

    引擎、路径、保存错误和后台任务均只有一份；画布和面板由各标签拥有。所有
    方法在 GUI 线程执行，文件操作不借用全局活动画布，关闭原标签也不会中止
    其他视图的防抖保存。视图通过 attach/detach 显式维护引用与信号连接。
    """

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.engine = MindMapEngine()
        self.file_state = MindMapFileState()
        self.views = []
        self.last_error = ""
        self._file_size_bytes = None
        self.disposed = False
        self._preparing = False
        self._unsubscribe = lambda: None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(500)
        self.timer.timeout.connect(lambda: self.autosave(background=True))
        self._background_save = None
        self._save_poll = QTimer(self)
        self._save_poll.setInterval(15)
        self._save_poll.timeout.connect(self._finish_background_save)

    @property
    def representative(self):
        """取该文件当前活动视图，否则使用首个关联视图；不激活或重排标签。"""
        active = self.owner.active
        return active if active in self.views else self.views[0]

    @property
    def files(self):
        """借用存活视图的文件控制器；各控制器使用相同引擎、路径与写入边界。"""
        return self.representative.files

    @property
    def canvas(self):
        """只为保存能力查询借用代表视图，导出仍由用户选中的标签自行执行。"""
        return self.representative.canvas

    def attach(self, view):
        """连接已初始化视图的文件通知并更新共享模式，不读盘或创建历史记录。"""
        self.views.append(view)
        view.files.error_occurred.connect(self._failed)
        view.files.state_changed.connect(self._saved)
        view.files.wait_for_pending_write = lambda: self._finish_background_save(wait=True)
        view.files.prepare_save = self.prepare_inputs
        self._unsubscribe()
        # 文档级状态在所有画布消费差量后刷新，防止侧栏读取仍未同步的旧选择。
        self._unsubscribe = self.engine.subscribe(self._changed)
        for item in self.views:
            item.canvas.shared_views = len(self.views) > 1

    def detach(self, view):
        """只断开指定视图；保留其他视图的保存任务，最后一个视图负责收束后台写入。"""
        if len(self.views) == 1:
            self.dispose()
        view.files.error_occurred.disconnect(self._failed)
        view.files.state_changed.disconnect(self._saved)
        view.files.prepare_save = None
        view.files.wait_for_pending_write = None
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
                self._failed("请先完成输入法候选，再保存或关闭导图")
                if self.owner.get(view.id) is view:
                    self.owner.window.manager.activate_document(view.id)
                return False
        self._preparing = True
        try:
            for view in views:
                view.commit_input()
        finally:
            self._preparing = False
        return True

    def flush(self, view) -> bool:
        """提交本文件全部视图并保存一次；失败保留文档、所有标签及原路径。"""
        if not self.prepare_inputs():
            return False
        self.timer.stop()
        saved = view.files.save_committed()
        if saved:
            self._saved()
        return saved

    def dispose(self):
        """幂等释放文件级计时器和订阅；调用时最后一个视图的控制器仍须存活。"""
        if self.disposed:
            return
        self.timer.stop()
        self._finish_background_save(wait=True)
        self.disposed = True
        self.timer.stop()
        self._save_poll.stop()
        self._unsubscribe()
        self.deleteLater()

    def _write(self, document, path, **kwargs):
        """所有写入先检查其他会话的文件归属，再复用引擎原子写入，禁止跨标签覆盖。"""
        self._finish_background_save(wait=True)
        other = self.owner.find_path(path)
        if other is not None and other.document is not self:
            raise MindMapError("该文件已在另一个标签打开，请选择其他保存位置")
        save_document(document, path, **kwargs)

    def _changed(self, event):
        """保存事件不重新启动计时器；修改、撤销和重做合并为一次延迟写入。"""
        if not self.disposed and event.kind in {"change", "undo", "redo"}:
            self.timer.start()
            if self.owner.get(self.representative.id) is self.representative:
                self.owner.window.record_operation(describe_change(event), session=self.representative)
        self.sync_views()

    def _refresh_file_size(self) -> None:
        """在文件操作或会话激活边界刷新实际字节数，不估算内存中尚未保存的内容。

        只读取当前绑定路径的磁盘元信息，整份导图的全部页面和图片均计入大小。
        未绑定路径、文件缺失或元信息读取失败时缓存 None，由状态栏显示“未知”；
        读取失败不修改保存错误、文档脏状态或自动保存计时器，也不弹出额外提示。
        """
        try:
            self._file_size_bytes = self.files.path.stat().st_size if self.files.path is not None else None
        except OSError:
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
        self.sync_views()

    def _failed(self, message):
        """失败保留原始状态且停止重试；显示文件名，下一次编辑或显式保存可再次尝试。"""
        if self.disposed:
            return
        self._refresh_file_size()
        self.last_error = message
        self.timer.stop()
        name = self.files.path.name if self.files.path else "新建导图"
        self.owner.window.show_status_message(f"{name}：{message}")
        self.owner.window.record_operation(f"保存导图失败：{message}", session=self.representative, level="error")
        self.sync_views()

    def autosave(self, *, background=False):
        """保存已提交版本，不结束编辑、移动焦点或提交草稿。

        计时器对大图的内置原生编码器启用后台写入，只在 GUI 线程捕获稳定快照。
        直接调用默认同步，保留显式保存、测试及宿主调用的成功/失败返回语义。
        外部注入写入器或编码器继续同步，不能假定它们支持独立进程。
        """
        if self.disposed:
            return False
        if (background and self.engine.node_count > BACKGROUND_SAVE_NODE_LIMIT
                and self.engine.is_dirty and self.files.writer == self._write):
            try:
                self.files.require_native_save_format()
                registry = self.canvas.format_registry
                if supports_process_save(registry):
                    if self._background_save is not None:
                        return True
                    if self.files.path is None:
                        raise MindMapError("导图尚未选择保存位置")
                    target = self.files.validate_save_path(self.files.path)
                    other = self.owner.find_path(target)
                    if other is not None and other.document is not self:
                        raise MindMapError("该文件已在另一个标签打开，请选择其他保存位置")
                    snapshot = self.engine.capture_save_snapshot()
                    if not can_process_snapshot(snapshot.document):
                        return self.files.save_committed()
                    executor = _save_pool()
                    try:
                        future = executor.submit(save_snapshot, snapshot.document, target)
                    except BrokenProcessPool:
                        _discard_save_pool(executor)
                        raise MindMapError("后台保存进程不可用，请重新保存") from None
                    self._background_save = future, snapshot, target, executor
                    self._save_poll.start()
                    return True
            except (MindMapError, OSError) as exc:
                self.files.report_error(str(exc))
                return False
        self._finish_background_save(wait=True)
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
            future.result()
        except Exception as exc:
            if isinstance(exc, BrokenProcessPool):
                _discard_save_pool(executor)
            if not self.disposed:
                self.files.report_error(f"后台保存失败：{exc}")
            return False
        if not self.disposed:
            self.engine.mark_saved_snapshot(snapshot)
            if self.files.path == target:
                self.files.state_changed.emit()
        return True


class MindMapSession(QObject):
    """一个文件标签拥有独立画布和面板，内容及保存状态委托共享文档。"""

    def __init__(self, owner, *, session_id=None, document=None):
        """建立标签级视图；document 省略时创建独立文件对象，传入时共享已有文档。

        普通打开及复制使用新标识，启动恢复传入已校验的旧 session_id 接回原分屏。
        本对象只拥有画布、面板和文件控制器；最后一个标签释放后才销毁共享文档。
        """
        super().__init__(owner)
        self.owner = owner
        self.id = session_id or "mindmap." + uuid4().hex
        self.document = document if document is not None else MindMapDocumentSession(owner)
        self.engine = self.document.engine
        self.disposed = False
        # 历史动作由标签栏承载，视图操作由主窗口状态栏承载，画布不再创建浮条。
        self.canvas = MindMapWidget(self.engine, toolbar=False, view_controls=False, embedded=True,
                                    layout_registry=owner.window.layout_registry,
                                    format_registry=owner.window.format_registry)
        self.canvas.setFocusProxy(self.canvas.view)
        self.panels = MindMapPanels(self.canvas, self)
        # 收藏面板单独接入会话，不扩大独立宿主的 PanelHost 必需接口；候选会话在
        # 完成文件读取前先隐藏面板，安装后由共享停靠容器接管 QWidget 所有权。
        self.favorites = SubmapFavoritesPanel(self.engine, self.canvas)
        self.favorites.hide()
        self.favorites.navigate_requested.connect(self._navigate_favorite)
        self.favorites.unfavorite_requested.connect(
            lambda page: self.canvas.call(lambda: self.canvas.set_submap_favorite(page, False)))
        self.canvas.page_changed.connect(self._sync_favorites)
        self.canvas.editable_changed.connect(self._sync_favorites)
        self.canvas.editing_changed.connect(self._sync_favorites)
        self.canvas.document_changed.connect(self._sync_favorites)
        self._sync_favorites()
        self.inspiration = InspirationPanel(self.engine, self.canvas)
        self.inspiration.hide()
        self.inspiration.navigate_requested.connect(self._run_inspiration_action)
        self.inspiration.attach_requested.connect(lambda root: self._run_inspiration_action(root, "attach"))
        self.inspiration.delete_requested.connect(lambda root: self._run_inspiration_action(root, "delete"))
        # 弹出菜单只刷新即时权限，不重设当前项，以保留键盘浏览尚未打开的想法。
        self.inspiration.menu_about_to_show.connect(
            lambda: self.inspiration.set_editable(self.canvas.editable and not self.is_composing))
        self.inspiration.new_requested.connect(lambda: self.canvas.call(self.canvas.add_inspiration))
        self.inspiration.drag_finished.connect(self.canvas.cancel_branch_transfer_preview)
        self.inspiration.set_drag_handlers(
            lambda root: self.canvas.call(lambda: self.canvas.create_branch_drag([root], origin="sidebar")),
            self.canvas.can_detach_branch_drag,
            lambda mime: bool(self.canvas.call(lambda: self.canvas.detach_branch_drag(mime))),
        )
        self.canvas.page_changed.connect(self._sync_inspiration)
        self.canvas.document_changed.connect(self._sync_inspiration)
        self.canvas.editable_changed.connect(self._sync_inspiration)
        self.canvas.editing_changed.connect(self._sync_inspiration)
        self.canvas.inspirations_detached.connect(self.inspiration.flash_roots)
        self.canvas.inspiration_sidebar_requested.connect(self._show_inspiration)
        self._sync_inspiration()
        self.files = MindMapFileController(self.canvas, self.panels, owner.window,
                                           writer=self.document._write, state=self.document.file_state)
        self.canvas.error_occurred.connect(self._canvas_failed)
        self.files.message.connect(self._file_message)
        self.document.attach(self)
        self.canvas.selection_changed.connect(lambda *_: owner.window._sync_document())
        self.canvas.editable_changed.connect(lambda *_: owner.window._sync_document())
        owner.window._configure_session(self)

    @property
    def timer(self):
        """兼容标签入口，返回所属文件唯一的自动保存计时器。"""
        return self.document.timer

    @property
    def last_error(self):
        """返回全部同文件标签共享的最近保存错误，空字符串表示无错误。"""
        return self.document.last_error

    @property
    def _file_size_bytes(self):
        return self.document._file_size_bytes

    @property
    def _background_save(self):
        return self.document._background_save

    @property
    def _write(self):
        return self.document._write

    def _refresh_file_size(self):
        """在原有激活边界刷新共享磁盘大小，其他标签直接读取同一缓存。"""
        self.document._refresh_file_size()

    def _saved(self):
        self.document._saved()

    def _failed(self, message):
        self.document._failed(message)

    def autosave(self, *, background=False):
        """兼容标签入口，仅委托文件级保存；已释放视图不再发起写入。"""
        return not self.disposed and self.document.autosave(background=background)

    def _finish_background_save(self, *, wait=False):
        return self.document._finish_background_save(wait=wait)

    def _canvas_failed(self, message):
        """记录本会话的编辑失败，保留原状态提示，不把编辑错误写成保存失败状态。"""
        if not self.disposed:
            self.owner.window.show_status_message(message)
            self.owner.window.record_operation(f"操作失败：{message}", session=self, level="error")

    def _file_message(self, message):
        """文件控制器仅在实际导出成功后通知；来源始终使用发出信号的所属会话。"""
        if not self.disposed:
            self.owner.window.show_status_message(message)
            self.owner.window.record_operation(message, session=self)

    def flush(self):
        """保存本文件所有标签的有效输入，共享写入失败时全部视图继续保留。"""
        return not self.disposed and self.document.flush(self)

    @property
    def is_composing(self) -> bool:
        """通过组件公开接口汇总输入法状态；查询不访问具体编辑器或表单字段。

        保存和关闭共用该边界，任一组件仍有候选时等待用户确认，不强制提交草稿。
        """
        return self.canvas.is_composing or self.panels.is_composing

    def commit_input(self):
        """提交本会话的有效输入并保留调用前焦点，批量保存不得激活后台分屏。

        原地编辑器的完成处理会把焦点交回自己的画布。切换会话或保存其他标签时，
        必须在同一事件循环内还原原焦点，避免停靠层将旧画布再次判定为活动导图。
        """
        if self.is_composing:
            return
        focus = QApplication.focusWidget()
        with self.engine.event_source(self.canvas.interaction_id):
            self.panels.flush_pending_edits()
            self.canvas.finish_edit(commit=True)
        if focus is not None and focus.isVisible() and QApplication.focusWidget() is not focus:
            focus.setFocus(Qt.FocusReason.OtherFocusReason)

    def can_close(self):
        """保存成功才允许关闭；失败时定位该文件，批量关闭的其他标签继续保留。"""
        allowed = self.flush()
        if (not allowed and not any(view.is_composing for view in self.document.views)
                and self.id in self.owner.window.manager.documents):
            self.owner.window.manager.activate_document(self.id)
        return allowed

    def close_popups(self):
        """离开会话时关闭旧菜单、候选预览和字段下拉框，拒绝迟到的选择回调。"""
        self.canvas.cancel_transient_ui()
        self.panels.close_popups()
        self.favorites.close_popups()
        self.inspiration.close_popups()

    def _sync_favorites(self, *_):
        """状态回调始终使用所属会话，后台标签或分屏不会借用另一个活动画布。"""
        self.favorites.set_current_page_id(self.canvas.current_page_id)
        self.favorites.set_editable(self.canvas.editable and not self.canvas.is_editing)

    def _navigate_favorite(self, page_id):
        """复用画布输入检查与页面现场恢复；失败后还原列表高亮，不伪装已经切页。"""
        self.canvas.call(lambda: self.canvas.navigate_to(page_id))
        self._sync_favorites()

    def _sync_inspiration(self, *_):
        """更新本文件侧栏高亮与权限；附属页面仍高亮所属灵感根，不借用活动会话。"""
        root = self.canvas.current_inspiration_root_id
        if self.canvas.current_page_kind in ("submap", "detail"):
            path = self.engine.node_path(self.canvas.document_view.root_id)
            if path and path[0].page_id == self.engine.inspiration_id:
                root = path[1].node_id
        self.inspiration.set_current_root_id(root)
        self.inspiration.set_editable(self.canvas.editable and not self.is_composing)

    def _run_inspiration_action(self, root_id: str, operation: str = "open") -> None:
        """对本会话指定想法执行列表导航、迁入导图或删除，返回 None。

        root_id 来自列表导航或右键命中的稳定根标识；operation 仅由本会话的信号连接提供。
        菜单弹出不切页，确定动作后才导航；修改项先复查只读与输入法状态，再选择
        根节点并复用画布既有操作，保留撤销、自动保存及附属页面删除确认。
        关闭或离开本会话后的迟到请求直接忽略；导航失败后重新同步侧栏高亮。
        所有调用均在 GUI 线程，错误交给画布的公开异常边界报告。
        """
        if self.disposed or self.owner.active is not self:
            return

        def perform():
            if operation != "open":
                if not self.canvas.editable:
                    raise MindMapError("当前画布为只读模式")
                if self.is_composing:
                    raise MindMapError("请先完成输入法候选，再操作想法")
            self.canvas.show_inspiration(root_id)
            if operation != "open":
                self.canvas.select_nodes([root_id])
                if operation == "attach":
                    self.canvas.attach_selected_inspirations()
                elif operation == "delete":
                    self.canvas.delete_selected()

        self.canvas.call(perform)
        self._sync_inspiration()

    def _show_inspiration(self):
        """导航成功后打开共享左侧灵感工具；后台恢复候选不抢占其他文件焦点。"""
        if self.owner.get(self.id) is self and self.owner.active is self:
            self.owner.window.manager.activate("inspiration", focus=False)

    def dispose(self):
        """停止计时器和订阅并延迟销毁组件；关闭标签和退出窗口均不缓存旧画布。

        停靠层在关闭通知返回后仍需完成页面移除，因此使用 deleteLater；重复安排
        延迟删除是安全的，候选会话未进入停靠层时也能通过同一流程彻底清理。
        """
        if self.disposed:
            return
        self.disposed = True
        self.document.detach(self)
        self.canvas.error_occurred.disconnect(self._canvas_failed)
        self.files.message.disconnect(self._file_message)
        self.close_popups()
        self.panels.dispose()
        self.favorites.dispose()
        self.inspiration.dispose()
        for stack, page in ((self.owner.window._outline_stack, self.panels.outline),
                            (self.owner.window._favorites_stack, self.favorites),
                            (self.owner.window._inspiration_stack, self.inspiration),
                            (self.owner.window._properties_stack, self.panels.properties),
                            (self.owner.window._settings_stack, self.panels.settings)):
            stack.removeWidget(page)
            page.hide()
            page.deleteLater()
        self.canvas.dispose()
        self.canvas.deleteLater()
        self.files.deleteLater()
        self.deleteLater()


class MindMapSessionManager(QObject):
    """维护运行中的文件集合；文档内容和路径不进入桌面布局持久化。"""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._sessions = {}
        self._known_paths = {}
        self.active_id = ""
        self._switching = False
        window.manager.document_activated.connect(self._activate)

    def __len__(self):
        return len(self._sessions)

    def __iter__(self):
        return iter(self._sessions)

    def values(self):
        """返回稳定快照，关闭或保存中的回调不会改变调用方正在遍历的集合。"""
        return tuple(self._sessions.values())

    def get(self, session_id):
        return self._sessions.get(session_id)

    @property
    def active(self):
        return self.get(self.active_id)

    def find_path(self, path):
        """查找同一路径的代表标签：优先当前标签，否则使用视觉顺序的首项。"""
        key = path_key(path)
        active = self.active
        if active is not None and active.files.path is not None and path_key(active.files.path) == key:
            return active
        return next((item for item in self.ordered() if item.files.path is not None
                     and path_key(item.files.path) == key), None)

    def _install(self, session, *, remember=True, after=None):
        """初次加载或保存完整成功后才注册界面，避免出现空路径和半初始化的标签。"""
        self._sessions[session.id] = session
        self._known_paths[session.id] = session.files.path
        self.window._outline_stack.addWidget(session.panels.outline)
        self.window._favorites_stack.addWidget(session.favorites)
        self.window._inspiration_stack.addWidget(session.inspiration)
        self.window._properties_stack.addWidget(session.panels.properties)
        self.window._settings_stack.addWidget(session.panels.settings)
        # 标签只显示不含末尾扩展名的文件名，完整路径仍由文件控制器保留。
        # 图标沿用应用配置的语义名称，由停靠层在主题切换时统一刷新。
        self.window.manager.register_document(DocumentSpec(
            session.id, session.files.path.stem, lambda: session.canvas, icon=self.window.spec.icon,
            can_close=session.can_close, on_closed=lambda: self._closed(session), dispose_on_close=True,
            on_duplicate=lambda: self.duplicate_session(session.id),
        ))
        if after is not None:
            group, index = self.window.host.editor.find_token(after)
            self.window.manager.move_document(session.id, "main", group.group_id, index + 1)
        self.window.manager.set_document_actions(session.id, [session.canvas.action("undo"), session.canvas.action("redo")])
        self.sync(session)
        self.window.manager.activate_document(session.id)
        if remember:
            self.window._remember_path(session.files.path)

    def duplicate_session(self, session_id: str) -> bool:
        """为指定标签创建共享内容的独立视图，紧邻来源插入并激活，返回是否成功。

        来源不必是活动标签。先检查当前活动标签与来源的输入法候选，再提交有效
        输入、捕获来源视图；不重新读盘、不改最近文件、不创建文件副本。取消或
        失败释放候选视图，已有关联文档及原分屏保留。仅限 GUI 线程调用。
        """
        source = self.get(session_id)
        if source is None or source.disposed:
            return False
        inputs = list(dict.fromkeys(view for view in (self.active, source) if view is not None))
        if any(view.is_composing for view in inputs):
            self.window.show_status_message("请先完成输入法候选，再复制标签")
            return False
        for view in inputs:
            view.commit_input()
        state = source.canvas.capture_page_view_state()
        candidate = MindMapSession(self, document=source.document)
        try:
            candidate.canvas.restore_page_view_state(state)
            candidate.canvas.set_editable(source.canvas.editable)
            self._install(candidate, remember=False, after=source.id)
            self.window.record_operation("复制标签", session=source)
            return True
        except Exception as error:
            self.window._record_exception(error, operation="复制标签", session=source)
            self._sessions.pop(candidate.id, None)
            self._known_paths.pop(candidate.id, None)
            # 回滚安装途中已注册的容器，不再执行可能写盘的业务关闭检查。
            registered = self.window.manager.documents.get(candidate.id)
            if registered is not None:
                registered["can_close"] = lambda: True
                registered["on_closed"] = None
                self.window.manager.close_document(candidate.id, dispose=True)
            candidate.dispose()
            self.window.manager.activate_document(source.id)
            self.window.show_status_message(f"复制标签失败：{error}")
            return False

    def new_document(self, path=None):
        """新建先选择路径并完成第一次写入；取消或失败仅清理候选会话。"""
        candidate = MindMapSession(self)
        try:
            if path is not None:
                # 扩展名补齐和原生格式限制统一交给文件控制器；首次写入完成后
                # 才注册会话，错误后缀或路径冲突不会创建半初始化的标签。
                candidate.files.path = Path(path).resolve()
            if not candidate.files.save(save_as=path is None):
                candidate.dispose()
                candidate.canvas.deleteLater()
                return False
            self._install(candidate)
            self.window.record_operation(f"新建导图：{candidate.files.path}", session=candidate)
            return True
        except Exception as error:
            self.window._record_exception(error, operation="新建导图", session=candidate)
            candidate.dispose()
            candidate.canvas.deleteLater()
            raise

    def open_path(self, path):
        """仅打开原生导图；先校验再查重，额外格式不能借已有会话绕过打开限制。"""
        try:
            return self._open_path(path)
        except Exception as error:
            self.window._record_exception(error, operation="打开导图", session=path)
            raise

    def _open_path(self, path, *, session_id=None, remember=True, view_state=None):
        """共用校验与安装流程；启动恢复在加载后、安装前还原旧标识和页面视图。

        view_state 仅由退出恢复传入，普通打开继续使用默认主导图。画布自行校验
        可选字段并保存延迟定位请求，不借用配置对象；初始化失败仍释放候选会话。
        """
        source = MindMapFileController.validate_open_path(path)
        existing = self.find_path(source)
        if existing is not None and session_id is None:
            self.window.manager.activate_document(existing.id)
            if remember:
                self.window._remember_path(existing.files.path)
            self.window.record_operation("激活已打开的导图", session=existing)
            return True
        candidate = MindMapSession(self, session_id=session_id,
                                   document=existing.document if existing is not None else None)
        try:
            if existing is None and not candidate.files.open_path(source.resolve()):
                candidate.dispose()
                candidate.canvas.deleteLater()
                return False
            if view_state is not None:
                candidate.canvas.restore_page_view_state(view_state)
            self._install(candidate, remember=remember)
            self.window.record_operation(f"打开导图：{candidate.files.path}", session=candidate)
            return True
        except Exception:
            candidate.dispose()
            candidate.canvas.deleteLater()
            raise

    def restore_documents(self, documents, active_id):
        """逐份重建退出时的导图，单份失败不影响其他文件，返回供窗口汇总的错误。

        记录顺序已经是视觉标签顺序；安装时复用会话 ID，随后由外壳恢复分屏树。
        提前选中原活动项，避免布局恢复时把最后加载的文件误当成活动标签。
        可选页面快照只用于此次启动，不用于之后手动重开；隐藏标签在实际显示
        后完成定位，不激活后台会话或改变最近文件顺序。仅在 GUI 线程初始化时
        调用，返回中文错误列表，文件失败不影响其他会话，候选对象由本管理器释放。
        """
        failures = []
        failed_paths = set()
        for item in documents:
            key = path_key(item["path"])
            if key in failed_paths:
                continue
            try:
                if not self._open_path(item["path"], session_id=item["id"], remember=False,
                                       view_state=item.get("view_state")):
                    failures.append(f"{Path(item['path']).name}：未能打开导图")
                    failed_paths.add(key)
            except (MindMapError, OSError, ValueError) as error:
                failures.append(f"{Path(item['path']).name}：{error}")
                failed_paths.add(key)
                self.window._record_exception(error, operation="恢复导图", session=item["path"])
        selected = self.get(active_id) or next(iter(self.values()), None)
        if selected is not None:
            self.window.manager.activate_document(selected.id)
        return failures

    def open_dialog(self):
        """空白页也可直接选择原生导图；固定筛选，不再构造扩展格式或导入入口。"""
        path, _ = QFileDialog.getOpenFileName(self.window, "打开导图", "", MindMapFileController.OPEN_FILTER)
        if not path:
            return False
        return self.open_path(path)

    def _activate(self, session_id):
        """切换页面前提交原会话有效输入；侧栏获得焦点时不改变逻辑活动会话。"""
        if self._switching or session_id == self.active_id:
            return
        self._switching = True
        try:
            focus = QApplication.focusWidget()
            old = self.active
            if old and not old.disposed:
                old.commit_input()
                old.close_popups()
            self.active_id = session_id
            if self.active is not None:
                # 标签及分屏切换时重新读取，允许外部文件变化在下次激活时更新。
                self.active._refresh_file_size()
            self.window._bind_session(self.active)
            if focus is not None and focus.isVisible() and QApplication.focusWidget() is not focus:
                focus.setFocus(Qt.FocusReason.OtherFocusReason)
        finally:
            self._switching = False

    def ordered(self):
        """按真实分屏树及标签顺序保存，避免拖动重排后仍按文件创建顺序处理。"""
        result = []

        def walk(node):
            if node["kind"] == "tabs":
                result.extend(self.get(key) for key in node["tokens"] if self.get(key) is not None)
            else:
                for child in node["children"]:
                    walk(child)

        walk(self.window.host.editor.snapshot())
        return result

    def save_all(self):
        """按文件首次出现的标签顺序提交及保存，同文件只处理一次，失败停止并定位。"""
        saved_documents = set()
        for session in self.ordered():
            if session.document in saved_documents:
                continue
            try:
                if not session.can_close():
                    return False
                saved_documents.add(session.document)
            except Exception as error:
                self.window._record_exception(error, operation="保存导图", session=session)
                raise
        self.window.show_status_message("全部导图已保存")
        if len(self):
            self.window.record_operation(f"保存全部完成，共 {len(saved_documents)} 份导图")
        return True

    def _saved_path(self, session):
        """仅在保存成功通知后记录改绑路径；外部错误改绑或保存失败不能污染历史。

        初次加载及首次保存尚未安装会话，由安装流程记入历史。已安装会话的另存为
        可能直接通过文件控制器调用，因此在成功事件边界检测，而不是依赖窗口动作。
        """
        if session.id not in self._sessions or session.disposed:
            return
        if self._known_paths.get(session.id) != session.files.path:
            for view in session.document.views:
                if self.get(view.id) is view:
                    self._known_paths[view.id] = session.files.path
            self.window._remember_path(session.files.path)

    def sync(self, session):
        """同步标签显示和文件状态；保存或另存为后使用当前路径，悬停提示保留完整路径。"""
        if session.id not in self._sessions or session.disposed:
            return
        manager = self.window.manager
        if session.id in manager.documents:
            # 仅隐藏最后一个扩展名，保留文件名中的其他点号；每次刷新都使用同一规则。
            # 失败文字与修改星号分别展示，即使错误发生在后台标签也能直接定位。
            title = session.files.path.stem + ("（保存失败）" if session.last_error else "")
            manager.set_document_title(session.id, title)
            manager.set_document_modified(session.id, session.files.is_dirty)
            location = self.window.host.editor.find_token(session.id)
            if location:
                tooltip = str(session.files.path) + ("\n保存失败：" + session.last_error if session.last_error else "")
                location[0].setTabToolTip(location[1], tooltip)
        self.window._sync_document()

    def _closed(self, session):
        self.window.record_operation("关闭标签" if len(session.document.views) > 1 else "关闭导图", session=session)
        self._sessions.pop(session.id, None)
        self._known_paths.pop(session.id, None)
        if self.active_id == session.id:
            self.active_id = ""
            self.window._bind_session(None)
        session.dispose()

    def dispose(self):
        """主窗口已经通过全部保存检查后释放资源，不再执行二次关闭或写入。"""
        self.window.manager.document_activated.disconnect(self._activate)
        self.active_id = ""
        self.window._bind_session(None)
        for session in self.values():
            session.dispose()
        self._sessions.clear()
        self._known_paths.clear()
        self.active_id = ""
