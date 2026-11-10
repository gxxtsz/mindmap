"""导图编辑器视图：每个标签独立持有画布、面板、导航和输入状态。"""

from __future__ import annotations

from uuid import uuid4

from mindmap_core import MindMapError
from mindmap_qt_canvas import MindMapFileController, MindMapWidget
from mindmap_qt_panels import InspirationPanel, MindMapPanels, RecentPagesPanel, SubmapFavoritesPanel
from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication
from workbench_api import OperationResult

from .documents import MindMapDocumentHandle


class MindMapEditorView(QObject):
    """一个文件标签拥有独立画布和面板，内容及保存状态委托共享文档。"""

    def __init__(self, owner, *, session_id=None, document=None):
        """建立标签级视图；document 省略时创建独立文件对象，传入时共享已有文档。

        普通打开及复制使用新标识，启动恢复传入已校验的旧 session_id 接回原分屏。
        本对象只拥有画布、面板和文件控制器；最后一个标签释放后才销毁共享文档。
        """
        super().__init__(owner)
        self.owner = owner
        self.id = session_id or "mindmap." + uuid4().hex
        self.document = document if document is not None else MindMapDocumentHandle(owner)
        self.engine = self.document.engine
        self.disposed = False
        self._navigating_recent_page = False
        self._last_recent_page_key = None
        colors = owner.window.ui_colors
        # 历史动作由标签栏承载，视图操作由主窗口状态栏承载，画布不再创建浮条。
        self.canvas = MindMapWidget(self.engine, toolbar=False, view_controls=False, embedded=True,
                                    ui_colors=colors,
                                    layout_registry=owner.window.layout_registry,
                                    format_registry=owner.window.format_registry)
        self.canvas.setFocusProxy(self.canvas.view)
        # 嵌入工作台时由宿主统一路由标准快捷键；独立画布的默认绑定保持原样。
        self.canvas.set_shortcuts({key: [] for key in ("undo", "redo", "cut", "copy", "paste", "select_all")})
        self.panels = MindMapPanels(self.canvas, self, ui_colors=colors)
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
        self.recent_pages = RecentPagesPanel(self.canvas)
        self.recent_pages.hide()
        self.recent_pages.navigate_requested.connect(self._navigate_recent_page)
        self.canvas.page_changed.connect(self._sync_recent_page)
        self.canvas.document_changed.connect(self._sync_recent_page)
        self.files = MindMapFileController(self.canvas, self.panels, owner.window.context.ui.parent,
                                           writer=self.document._write, state=self.document.file_state,
                                           background_open=True)
        self.canvas.error_occurred.connect(self._canvas_failed)
        self.files.message.connect(self._file_message)
        self.document.attach(self)
        self.canvas.selection_changed.connect(lambda *_: owner.window._sync_document())
        self.canvas.editable_changed.connect(lambda *_: owner.window._sync_document())
        owner.window._configure_session(self)

    icon = "mindmap"

    @property
    def widget(self):
        return self.canvas

    @property
    def title(self):
        return (self.files.path.stem if self.files.path else "新建导图") + ("（保存失败）" if self.document.last_error else "")

    @property
    def status_text(self):
        size = self.document._file_size_bytes
        value = float(size or 0)
        unit = "B"
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                break
            value /= 1024
        display = "未知" if size is None else f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        status = "保存失败" if self.document.last_error else "待保存" if self.files.is_dirty else "已保存"
        return (f"{len(self.canvas.document_view.nodes)} 个节点 · {self.canvas.selection_count} 个选中 · "
                f"{status} · 文件大小：{display}")

    @property
    def edit_actions(self):
        return {key: self.canvas.action(key) for key in ("undo", "redo", "cut", "copy", "paste", "select_all")}

    @property
    def toolbar_actions(self):
        return [self.canvas.action("undo"), self.canvas.action("redo")]

    @property
    def menu_actions(self):
        groups = {"编辑": ("undo", "redo", "add_inspiration", "move_to_inspiration", "attach_inspirations",
                "group_inspirations", "add_child", "add_sibling", "insert_parent", "edit", "properties",
                "delete", "convert_descendants_to_submap", "convert_submap_to_descendants", "set_page_reference",
                "open_page_reference", "clear_page_reference", "copy", "cut", "paste", "select_all", "image", "image_url"),
                "视图": ("zoom_in", "zoom_out", "expand_all", "collapse_all")}
        return {title: [self.canvas.action(key) for key in keys] for title, keys in groups.items()}

    def prepare_input(self):
        if self.is_composing:
            self.owner.window.show_status_message("请先完成输入法候选，再保存或关闭导图")
            return False
        self.commit_input()
        return True

    def capture_state(self):
        # 退出快照提交前收束页面历史防抖，保证隐藏标签最后的导航也进入插件状态。
        self.owner.window.flush_recent_pages()
        return {"version": 1, **self.canvas.capture_page_view_state()}

    def restore_state(self, state):
        """插件负责自己的视图版本，旧记录没有版本字段时按第一版兼容读取。"""
        if isinstance(state, dict) and state.get("version", 1) == 1:
            self.canvas.restore_page_view_state(state)

    def duplicate(self):
        return OperationResult() if self.owner.duplicate_session(self.id) else OperationResult.cancelled()


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

    def _record_page_visit(self) -> None:
        """仅记录已安装的活动标签；后台画布同步和启动恢复的中间页面均不计入。"""
        if (self.disposed or self.owner.active is not self or self.owner.get(self.id) is not self
                or self.owner.window._restoring_recent_pages):
            return
        key = self.canvas.current_page_id, self.canvas.current_inspiration_root_id
        self._last_recent_page_key = key
        self.document.recent_pages.visit(*key)

    def _sync_recent_page(self, *_):
        """用完整页面键检测实际切换；文字编辑和自动保存的通知不改变访问顺序。"""
        if self.disposed:
            return
        key = self.canvas.current_page_id, self.canvas.current_inspiration_root_id
        changed = key != self._last_recent_page_key
        self._last_recent_page_key = key
        self.recent_pages.set_current_page(*key)
        if changed:
            self._record_page_visit()

    def _navigate_recent_page(self, page_id, root_id):
        """在所属活动标签安全跳转；失败恢复高亮，成功才置顶并保留最近访问侧栏。

        同步信号期间临时抑制灵感侧栏自动展开，避免成功进入想法后替换本列表。
        输入法、待提交属性和视口缓存均交给现有画布接口；不会创建新标签。
        """
        if self.disposed or self.owner.active is not self:
            return

        def navigate():
            if self.engine.page_summary(page_id, root_id) is None:
                self.document.recent_pages.refresh()
                raise MindMapError("该页面已不存在，已从最近访问中移除")
            if root_id is not None:
                self.canvas.show_inspiration(root_id)
            else:
                self.canvas.navigate_to(page_id)
            return True

        self._navigating_recent_page = True
        try:
            if self.canvas.call(navigate):
                self._record_page_visit()
        finally:
            self._navigating_recent_page = False
            self.recent_pages.set_current_page(self.canvas.current_page_id,
                                               self.canvas.current_inspiration_root_id, reset_selection=True)

    def _navigate_favorite(self, page_id):
        """复用画布输入检查与页面现场恢复；失败后还原收藏树高亮，不伪装已经切页。"""
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
        if not self._navigating_recent_page and self.owner.get(self.id) is self and self.owner.active is self:
            self.owner.window.context.ui.activate_panel("inspiration", focus=False)

    def dispose(self):
        """停止计时器和订阅并延迟销毁组件；关闭标签和退出窗口均不缓存旧画布。

        停靠层在关闭通知返回后仍需完成页面移除，因此使用 deleteLater；重复安排
        延迟删除是安全的，候选会话未进入停靠层时也能通过同一流程彻底清理。
        """
        if self.disposed:
            return
        if self.owner.get(self.id) is self:
            self.owner.window.flush_recent_pages()
            self.owner.window.record_operation("关闭标签" if len(self.document.views) > 1 else "关闭导图", session=self)
            self.owner._sessions.pop(self.id, None)
            self.owner._known_paths.pop(self.id, None)
            if self.owner.active_id == self.id:
                self.owner.active_id = ""
                self.owner.window._bind_session(None)
        self.disposed = True
        self.document.detach(self)
        self.canvas.error_occurred.disconnect(self._canvas_failed)
        self.files.message.disconnect(self._file_message)
        self.close_popups()
        self.panels.dispose()
        self.favorites.dispose()
        self.inspiration.dispose()
        self.recent_pages.dispose()
        for stack, page in ((self.owner.window._outline_stack, self.panels.outline),
                            (self.owner.window._favorites_stack, self.favorites),
                            (self.owner.window._inspiration_stack, self.inspiration),
                            (self.owner.window._recent_pages_stack, self.recent_pages),
                            (self.owner.window._properties_stack, self.panels.properties),
                            (self.owner.window._settings_stack, self.panels.settings)):
            # 未安装的候选会话独立持有面板，后台读取结束时主窗口的停靠栈可能已释放。
            if page.parentWidget() is stack:
                stack.removeWidget(page)
            page.hide()
            page.deleteLater()
        self.canvas.dispose()
        self.canvas.deleteLater()
        self.files.deleteLater()
        self.deleteLater()


