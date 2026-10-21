"""多导图会话：文件归属、独立自动保存、激活切换和确定性资源释放。"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from mindmap_docking import DocumentSpec
from mindmap_engine import MindMapEngine, MindMapError, save_document
from mindmap_qt import MindMapFileController, MindMapPanels, MindMapWidget
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog


def path_key(path):
    """比较真实绝对路径；Windows 忽略大小写，解析软链接后避免同一文件重复编辑。"""
    return os.path.normcase(str(Path(path).resolve()))


class MindMapSession(QObject):
    """一份导图拥有全部编辑和文件状态，自动保存只读取已提交的引擎快照。"""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.id = "mindmap." + uuid4().hex
        self.engine = MindMapEngine()
        self.canvas = MindMapWidget(self.engine, toolbar=False, view_controls=True, embedded=True,
                                    layout_registry=owner.window.layout_registry,
                                    format_registry=owner.window.format_registry)
        self.canvas.setFocusProxy(self.canvas.view)
        self.panels = MindMapPanels(self.canvas, self)
        self.files = MindMapFileController(self.canvas, self.panels, owner.window, writer=self._write)
        self.last_error = ""
        self.disposed = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.autosave)
        self._unsubscribe = self.engine.subscribe(self._changed)
        self.files.error_occurred.connect(self._failed)
        self.canvas.error_occurred.connect(owner.window.show_status_message)
        self.files.message.connect(owner.window.show_status_message)
        self.files.state_changed.connect(self._saved)
        self.canvas.selection_changed.connect(lambda *_: owner.window._sync_document())
        self.canvas.editable_changed.connect(lambda *_: owner.window._sync_document())
        owner.window._configure_session(self)
        self.settings_menu = self.panels.settings.create_menu(owner.window)

    def _write(self, document, path, **kwargs):
        """所有写入先检查其他会话的文件归属，再复用引擎原子写入，禁止跨标签覆盖。"""
        other = self.owner.find_path(path)
        if other is not None and other is not self:
            raise MindMapError("该文件已在另一个标签打开，请选择其他保存位置")
        save_document(document, path, **kwargs)

    def _changed(self, event):
        """保存事件不重新启动计时器；修改、撤销和重做合并为一次延迟写入。"""
        if not self.disposed and event.kind in {"change", "undo", "redo"}:
            self.timer.start()
        self.owner.sync(self)

    def _saved(self):
        self.last_error = ""
        self.timer.stop()
        self.owner.sync(self)

    def _failed(self, message):
        """失败保留原始状态且停止重试；显示文件名，下一次编辑或显式保存可再次尝试。"""
        self.last_error = message
        self.timer.stop()
        name = self.files.path.name if self.files.path else "新建导图"
        self.owner.window.show_status_message(f"{name}：{message}")
        self.owner.sync(self)

    def autosave(self):
        """计时保存不得结束原地编辑、移动焦点或提交仍在输入的侧栏字段。"""
        if self.disposed:
            return False
        saved = self.files.save_committed()
        if saved and self.last_error:
            # 撤销回已保存快照时可以无需写盘，但旧错误也应清除，避免干净标签
            # 一直显示失败。实际写盘成功则已经由 state_changed 清除了错误。
            self._saved()
        return saved

    def flush(self):
        """显式保存和关闭先完成已确认输入；输入法仍在合成时保留页面等待用户完成。"""
        if self.panels.properties.composing or (self.canvas.editor is not None and self.canvas.editor.composing):
            self._failed("请先完成输入法候选，再保存或关闭导图")
            return False
        self.commit_input()
        self.timer.stop()
        saved = self.files.save_committed()
        if saved:
            self._saved()
        return saved

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
        self.canvas._cancel_branch_menu()
        self.panels.settings._close_popups()
        self.panels.outline.depth_menu.close()
        fields = self.panels.properties.fields
        fields["branch.kind"].hidePopup()
        fields["style.font_family"].hidePopup()
        fields["style.font_family"].completer().popup().hide()

    def dispose(self):
        """停止计时器和订阅并延迟销毁组件；关闭标签和退出窗口均不缓存旧画布。

        停靠层在关闭通知返回后仍需完成页面移除，因此使用 deleteLater；重复安排
        延迟删除是安全的，候选会话未进入停靠层时也能通过同一流程彻底清理。
        """
        if self.disposed:
            return
        self.disposed = True
        self.timer.stop()
        self._unsubscribe()
        self.close_popups()
        self.settings_menu.close()
        self.settings_menu.deleteLater()
        self.panels.dispose()
        for stack, page in ((self.owner.window._outline_stack, self.panels.outline),
                            (self.owner.window._properties_stack, self.panels.properties)):
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

    def _install(self, session):
        """初次加载或保存完整成功后才注册界面，避免出现空路径和半初始化的标签。"""
        self._sessions[session.id] = session
        self.window._outline_stack.addWidget(session.panels.outline)
        self.window._properties_stack.addWidget(session.panels.properties)
        # 标签只显示不含末尾扩展名的文件名，完整路径仍由文件控制器保留。
        # 图标沿用应用配置的语义名称，由停靠层在主题切换时统一刷新。
        self.window.manager.register_document(DocumentSpec(
            session.id, session.files.path.stem, lambda: session.canvas, icon=self.window.spec.icon,
            can_close=session.can_close, on_closed=lambda: self._closed(session), dispose_on_close=True,
        ))
        self.window.manager.set_document_actions(session.id, [session.canvas.action("undo"), session.canvas.action("redo")])
        self.sync(session)
        self.window.manager.activate_document(session.id)

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
            return True
        except Exception:
            candidate.dispose()
            candidate.canvas.deleteLater()
            raise

    def open_path(self, path):
        """仅打开原生导图；先校验再查重，额外格式不能借已有会话绕过打开限制。"""
        source = MindMapFileController.validate_open_path(path)
        existing = self.find_path(source)
        if existing is not None:
            self.window.manager.activate_document(existing.id)
            return True
        candidate = MindMapSession(self)
        try:
            if not candidate.files.open_path(source.resolve()):
                candidate.dispose()
                candidate.canvas.deleteLater()
                return False
            self._install(candidate)
            return True
        except Exception:
            candidate.dispose()
            candidate.canvas.deleteLater()
            raise

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
            if not session.can_close():
                return False
        self.window.show_status_message("全部导图已保存")
        return True

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
        self._sessions.pop(session.id, None)
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
        self.active_id = ""
