"""多导图会话：文件归属、独立自动保存、激活切换和确定性资源释放。"""

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
from mindmap_qt_canvas import MindMapFileController, MindMapWidget
from mindmap_qt_panels import MindMapPanels, SubmapFavoritesPanel
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


class MindMapSession(QObject):
    """一份导图拥有全部编辑和文件状态，自动保存只读取已提交的引擎快照。"""

    def __init__(self, owner, *, session_id=None):
        """普通文件创建新标识，启动恢复使用已校验的旧标识接回原来的分屏标签。"""
        super().__init__(owner)
        self.owner = owner
        self.id = session_id or "mindmap." + uuid4().hex
        self.engine = MindMapEngine()
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
        self.files = MindMapFileController(self.canvas, self.panels, owner.window, writer=self._write)
        self.last_error = ""
        self.disposed = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(500)
        self.timer.timeout.connect(lambda: self.autosave(background=True))
        self._background_save = None
        self._save_poll = QTimer(self)
        self._save_poll.setInterval(15)
        self._save_poll.timeout.connect(self._finish_background_save)
        self.files.wait_for_pending_write = lambda: self._finish_background_save(wait=True)
        self._unsubscribe = self.engine.subscribe(self._changed)
        self.files.error_occurred.connect(self._failed)
        self.canvas.error_occurred.connect(self._canvas_failed)
        self.files.message.connect(self._file_message)
        self.files.state_changed.connect(self._saved)
        self.canvas.selection_changed.connect(lambda *_: owner.window._sync_document())
        self.canvas.editable_changed.connect(lambda *_: owner.window._sync_document())
        owner.window._configure_session(self)

    def _write(self, document, path, **kwargs):
        """所有写入先检查其他会话的文件归属，再复用引擎原子写入，禁止跨标签覆盖。"""
        self._finish_background_save(wait=True)
        other = self.owner.find_path(path)
        if other is not None and other is not self:
            raise MindMapError("该文件已在另一个标签打开，请选择其他保存位置")
        save_document(document, path, **kwargs)

    def _changed(self, event):
        """保存事件不重新启动计时器；修改、撤销和重做合并为一次延迟写入。"""
        if not self.disposed and event.kind in {"change", "undo", "redo"}:
            self.timer.start()
            if self.owner.get(self.id) is self:
                self.owner.window.record_operation(describe_change(event), session=self)
        self.owner.sync(self)

    def _saved(self):
        self.last_error = ""
        if self.engine.is_dirty and not self.disposed:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
        self.owner._saved_path(self)
        self.owner.sync(self)

    def _failed(self, message):
        """失败保留原始状态且停止重试；显示文件名，下一次编辑或显式保存可再次尝试。"""
        if self.disposed:
            return
        self.last_error = message
        self.timer.stop()
        name = self.files.path.name if self.files.path else "新建导图"
        self.owner.window.show_status_message(f"{name}：{message}")
        self.owner.window.record_operation(f"保存导图失败：{message}", session=self, level="error")
        self.owner.sync(self)

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
                    if other is not None and other is not self:
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

    def flush(self):
        """显式保存和关闭先完成已确认输入；输入法仍在合成时保留页面等待用户完成。"""
        if self.is_composing:
            self._failed("请先完成输入法候选，再保存或关闭导图")
            return False
        self.commit_input()
        self.timer.stop()
        saved = self.files.save_committed()
        if saved:
            self._saved()
        return saved

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
        focus = QApplication.focusWidget()
        self.panels.flush_pending_edits()
        self.canvas.finish_edit(commit=True)
        if focus is not None and focus.isVisible() and QApplication.focusWidget() is not focus:
            focus.setFocus(Qt.FocusReason.OtherFocusReason)

    def can_close(self):
        """保存成功才允许关闭；失败时定位该文件，批量关闭的其他标签继续保留。"""
        allowed = self.flush()
        if not allowed and self.id in self.owner.window.manager.documents:
            self.owner.window.manager.activate_document(self.id)
        return allowed

    def close_popups(self):
        """离开会话时关闭旧菜单、候选预览和字段下拉框，拒绝迟到的选择回调。"""
        self.canvas.cancel_transient_ui()
        self.panels.close_popups()
        self.favorites.close_popups()

    def _sync_favorites(self, *_):
        """状态回调始终使用所属会话，后台标签或分屏不会借用另一个活动画布。"""
        self.favorites.set_current_page_id(self.canvas.current_page_id)
        self.favorites.set_editable(self.canvas.editable and not self.canvas.is_editing)

    def _navigate_favorite(self, page_id):
        """复用画布输入检查与页面现场恢复；失败后还原列表高亮，不伪装已经切页。"""
        self.canvas.call(lambda: self.canvas.navigate_to(page_id))
        self._sync_favorites()

    def dispose(self):
        """停止计时器和订阅并延迟销毁组件；关闭标签和退出窗口均不缓存旧画布。

        停靠层在关闭通知返回后仍需完成页面移除，因此使用 deleteLater；重复安排
        延迟删除是安全的，候选会话未进入停靠层时也能通过同一流程彻底清理。
        """
        if self.disposed:
            return
        self.disposed = True
        self.timer.stop()
        self._finish_background_save(wait=True)
        self._unsubscribe()
        self.files.error_occurred.disconnect(self._failed)
        self.canvas.error_occurred.disconnect(self._canvas_failed)
        self.files.message.disconnect(self._file_message)
        self.files.state_changed.disconnect(self._saved)
        self.close_popups()
        self.panels.dispose()
        self.favorites.dispose()
        for stack, page in ((self.owner.window._outline_stack, self.panels.outline),
                            (self.owner.window._favorites_stack, self.favorites),
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
        key = path_key(path)
        return next((item for item in self.values() if item.files.path is not None
                     and path_key(item.files.path) == key), None)

    def _install(self, session, *, remember=True):
        """初次加载或保存完整成功后才注册界面，避免出现空路径和半初始化的标签。"""
        self._sessions[session.id] = session
        self._known_paths[session.id] = session.files.path
        self.window._outline_stack.addWidget(session.panels.outline)
        self.window._favorites_stack.addWidget(session.favorites)
        self.window._properties_stack.addWidget(session.panels.properties)
        self.window._settings_stack.addWidget(session.panels.settings)
        # 标签只显示不含末尾扩展名的文件名，完整路径仍由文件控制器保留。
        # 图标沿用应用配置的语义名称，由停靠层在主题切换时统一刷新。
        self.window.manager.register_document(DocumentSpec(
            session.id, session.files.path.stem, lambda: session.canvas, icon=self.window.spec.icon,
            can_close=session.can_close, on_closed=lambda: self._closed(session), dispose_on_close=True,
        ))
        self.window.manager.set_document_actions(session.id, [session.canvas.action("undo"), session.canvas.action("redo")])
        self.sync(session)
        self.window.manager.activate_document(session.id)
        if remember:
            self.window._remember_path(session.files.path)

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
        if existing is not None:
            self.window.manager.activate_document(existing.id)
            if remember:
                self.window._remember_path(existing.files.path)
            self.window.record_operation("激活已打开的导图", session=existing)
            return True
        candidate = MindMapSession(self, session_id=session_id)
        try:
            if not candidate.files.open_path(source.resolve()):
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
        for item in documents:
            try:
                if not self._open_path(item["path"], session_id=item["id"], remember=False,
                                       view_state=item.get("view_state")):
                    failures.append(f"{Path(item['path']).name}：未能打开导图")
            except (MindMapError, OSError, ValueError) as error:
                failures.append(f"{Path(item['path']).name}：{error}")
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
        """按可见标签顺序提交和保存；失败时停止并定位错误，成功不切换当前标签。"""
        for session in self.ordered():
            try:
                if not session.can_close():
                    return False
            except Exception as error:
                self.window._record_exception(error, operation="保存导图", session=session)
                raise
        self.window.show_status_message("全部导图已保存")
        if len(self):
            self.window.record_operation(f"保存全部完成，共 {len(self)} 份导图")
        return True

    def _saved_path(self, session):
        """仅在保存成功通知后记录改绑路径；外部错误改绑或保存失败不能污染历史。

        初次加载及首次保存尚未安装会话，由安装流程记入历史。已安装会话的另存为
        可能直接通过文件控制器调用，因此在成功事件边界检测，而不是依赖窗口动作。
        """
        if session.id not in self._sessions or session.disposed:
            return
        if self._known_paths.get(session.id) != session.files.path:
            self._known_paths[session.id] = session.files.path
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
        self.window.record_operation("关闭导图", session=session)
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
