"""导图会话集合：创建、打开和复制视图，并向桌面文档服务登记。"""

from __future__ import annotations

from pathlib import Path

from mindmap_desktop_api import path_key
from mindmap_qt_editor.canvas import MindMapFileController
from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication

from .views import MindMapEditorView


class MindMapSessions(QObject):
    """维护运行中的文件集合；文档内容和路径不进入桌面布局持久化。"""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._sessions = {}
        self._known_paths = {}
        self.active_id = ""
        self._switching = False
        self._active_subscription = window.context.ui.subscribe_active(self._activate)

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

    def _install(self, session, *, remember=True, after=None, reset_recent_pages=False):
        """初次加载或保存完整成功后才注册界面，避免出现空路径和半初始化的标签。"""
        self._sessions[session.id] = session
        self._known_paths[session.id] = session.files.path
        session.document.recent_pages.bind(session.files.path, reset=reset_recent_pages)
        self.window._outline_stack.addWidget(session.panels.outline)
        self.window._favorites_stack.addWidget(session.favorites)
        self.window._inspiration_stack.addWidget(session.inspiration)
        self.window._recent_pages_stack.addWidget(session.recent_pages)
        self.window._properties_stack.addWidget(session.panels.properties)
        self.window._settings_stack.addWidget(session.panels.settings)
        self.window.context.documents.register_view(session, after=after, remember=False)
        self.sync(session)
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
        state = source.capture_state()
        candidate = MindMapEditorView(self, document=source.document)
        try:
            candidate.restore_state(state)
            candidate.canvas.set_editable(source.canvas.editable)
            self._install(candidate, remember=False, after=source.id)
            self.window.record_operation("复制标签", session=source)
            return True
        except Exception as error:
            self.window._record_exception(error, operation="复制标签", session=source)
            self._sessions.pop(candidate.id, None)
            self._known_paths.pop(candidate.id, None)
            # 回滚安装途中已注册的容器，不再执行可能写盘的业务关闭检查。
            self.window.context.documents.remove_view(candidate.id)
            candidate.dispose()
            self.window.context.documents.activate_view(source.id)
            self.window.show_status_message(f"复制标签失败：{error}")
            return False

    def new_document(self, path=None):
        """新建先选择路径并完成第一次写入；取消或失败仅清理候选会话。"""
        candidate = MindMapEditorView(self)
        try:
            if path is not None:
                # 扩展名补齐和原生格式限制统一交给文件控制器；首次写入完成后
                # 才注册会话，错误后缀或路径冲突不会创建半初始化的标签。
                candidate.files.path = Path(path).resolve()
            if not candidate.files.save(save_as=path is None):
                candidate.dispose()
                candidate.canvas.deleteLater()
                return False
            self._install(candidate, reset_recent_pages=True)
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
            self.window.context.documents.activate_view(existing.id)
            if remember:
                self.window._remember_path(existing.files.path)
            self.window.record_operation("激活已打开的导图", session=existing)
            return True
        candidate = MindMapEditorView(self, session_id=session_id,
                                   document=existing.document if existing is not None else None)
        try:
            opened = existing is not None or candidate.files.open_path(source.resolve())
            # 大文件模态等待会处理关闭事件。窗口已释放时丢弃候选，避免加载结果
            # 重新注册标签，或访问已经销毁的停靠容器。
            if not opened or self.window._disposed:
                candidate.dispose()
                candidate.canvas.deleteLater()
                return False
            candidate.restore_state(view_state)
            self._install(candidate, remember=remember)
            self.window.record_operation(f"打开导图：{candidate.files.path}", session=candidate)
            return True
        except Exception:
            candidate.dispose()
            candidate.canvas.deleteLater()
            raise


    def _activate(self, view):
        """切换页面前提交原会话有效输入；侧栏获得焦点时不改变逻辑活动会话。"""
        session_id = view.id if view is not None else ""
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
                self.active.document._refresh_file_size()
            self.window._bind_session(self.active)
            if self.active is not None:
                self.active._record_page_visit()
            if focus is not None and focus.isVisible() and QApplication.focusWidget() is not focus:
                focus.setFocus(Qt.FocusReason.OtherFocusReason)
        finally:
            self._switching = False

    def ordered(self):
        """按真实分屏树及标签顺序保存，避免拖动重排后仍按文件创建顺序处理。"""
        return [view for view in self.window.context.documents.ordered() if self.get(view.id) is view]


    def _saved_path(self, session):
        """仅在保存成功通知后记录改绑路径；外部错误改绑或保存失败不能污染历史。

        初次加载及首次保存尚未安装会话，由安装流程记入历史。已安装会话的另存为
        可能直接通过文件控制器调用，因此在成功事件边界检测，而不是依赖窗口动作。
        """
        if session.id not in self._sessions or session.disposed:
            return
        if self._known_paths.get(session.id) != session.files.path:
            session.document.recent_pages.bind(session.files.path)
            for view in session.document.views:
                if self.get(view.id) is view:
                    self._known_paths[view.id] = session.files.path
            self.window._remember_path(session.files.path)

    def sync(self, session):
        """同步标签显示和文件状态；保存或另存为后使用当前路径，悬停提示保留完整路径。"""
        if session.id not in self._sessions or session.disposed:
            return
        self.window.context.documents.sync(session)
        self.window._sync_document()

    def _closed(self, session):
        self.window.flush_recent_pages()
        self.window.record_operation("关闭标签" if len(session.document.views) > 1 else "关闭导图", session=session)
        self._sessions.pop(session.id, None)
        self._known_paths.pop(session.id, None)
        if self.active_id == session.id:
            self.active_id = ""
            self.window._bind_session(None)
        session.dispose()

    def dispose(self):
        """主窗口已经通过全部保存检查后释放资源，不再执行二次关闭或写入。"""
        self._active_subscription.dispose()
        self.active_id = ""
        self.window._bind_session(None)
        for session in self.values():
            session.dispose()
        self._sessions.clear()
        self._known_paths.clear()
        self.active_id = ""
