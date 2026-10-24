"""灵感侧栏与画布之间的标识拖放、独立候选校验和一次性事务。"""

from mindmap_core import MindMapEngine, MindMapError
from mindmap_qt_ui.branch_drag import BranchDrag, decode_branch_drag, encode_branch_drag
from PySide6.QtCore import Qt
from PySide6.QtGui import QDrag
from shiboken6 import isValid

from .dragging import _apply_move, _DropProposal


class BranchTransferMixin:
    """供画布组合的跨控件适配；面板只拿到公开回调，不借用场景或编辑器。"""

    def cancel_branch_transfer_preview(self) -> None:
        """清除跨控件落点提示，返回 None，不回滚已提交内容或生成历史。

        侧栏原生拖放结束时由宿主调用，覆盖 Windows 取消后未发送 dragLeave 的
        情况；已经成功提交的重排动画继续播放。重复调用安全，画布释放后无操作；
        仅在 GUI 线程调用。
        """
        if not self._disposed:
            self.view.node_drag.cancel_external()

    def create_branch_drag(self, node_ids=None, *, origin="canvas"):
        """创建本会话的分支移动载荷，返回新 QMimeData，无效来源返回 None。

        node_ids 省略时取画布选择；侧栏来源只允许一个现有游离根。画布多选会
        按树序去掉祖先重复选择。完成有效输入后冻结文档版本；只读、输入法候选、
        详情页内部及受保护页根拒绝。通过 call 调用可将中文异常交给宿主提示。
        仅限 GUI 线程，返回数据交给 QDrag 拥有，不包含内容副本，也不产生历史。
        """
        if self._disposed or not self.editable or origin not in ("canvas", "sidebar"):
            return None
        self._prepare_page_navigation()
        ids = list(node_ids if node_ids is not None else self.selected_node_ids())
        if origin == "sidebar":
            if len(ids) != 1 or ids[0] not in self._book_document.inspiration.root_ids:
                return None
        else:
            if self.current_page_kind == "detail" or any(key not in self._document.nodes for key in ids):
                return None
            selected = set(ids)
            ids = []
            for key in self._document.ordered_nodes():
                if key not in selected:
                    continue
                parent = self._node_parents.get(key)
                while parent is not None and parent not in selected:
                    parent = self._node_parents.get(parent)
                if parent is None:
                    ids.append(key)
            if self.current_page_kind != "inspiration" and self._document.root_id in ids:
                return None
        if not ids:
            return None
        return encode_branch_drag(BranchDrag(self._branch_drag_token, self.engine.document_generation,
                                             self._branch_content_revision, tuple(ids), origin))

    def _branch_drag_data(self, mime):
        data = decode_branch_drag(mime)
        if (self._disposed or not self.editable or self.is_composing or data is None
                or data.token != self._branch_drag_token
                or data.generation != self.engine.document_generation
                or data.revision != self._branch_content_revision
                or not all(guard() for guard in tuple(self._navigation_guards))
                or any(key not in self._book_document.nodes for key in data.node_ids)):
            return None
        return data

    def can_detach_branch_drag(self, mime) -> bool:
        """只读判断载荷能否成为独立想法，返回布尔值，不修改内容或手势。

        mime 是 create_branch_drag 的载荷；其他文件、过期版本、详情内部、
        受保护页根及全部已独立的根返回 False。可在拖动每帧重复调用，
        结果不替代松手时的二次检查；仅限 GUI 线程，释放后返回 False。
        """
        data = self._branch_drag_data(mime)
        if data is None or data.origin != "canvas":
            return False
        pages = {self.engine.node_page_id(key) for key in data.node_ids}
        if len(pages) != 1:
            return False
        page = next(iter(pages))
        if self._book_document.page_kind(page) == "detail":
            return False
        if page != self.engine.inspiration_id:
            root = self._book_document.page_record(page).root_id
            return root not in data.node_ids
        return any(key not in self._book_document.inspiration.root_ids for key in data.node_ids)

    def detach_branch_drag(self, mime) -> bool:
        """将画布分支拆入侧栏，一次提交并留在原画布；失败抛出异常且不提交。

        先复查会话、版本和输入，再独立试算源页及兼容坐标布局。现有独立根跳过；
        新根按文档既有坐标边界自动落位，全部内容和 ID 原地随行。成功通知侧栏提示，
        空操作返回 False，不制造历史或保存请求。只在 GUI 线程调用。
        """
        if not self.can_detach_branch_drag(mime):
            return False
        self._prepare_page_navigation()
        if not self.can_detach_branch_drag(mime):
            return False
        data = self._branch_drag_data(mime)
        roots = [key for key in data.node_ids if key not in self._book_document.inspiration.root_ids]
        positions = self.inspiration_positions(roots)
        source = self.engine.node_page_id(roots[0])
        scratch = MindMapEngine(self.engine.document)
        scratch.move_to_inspiration(roots, positions=positions)
        for page in dict.fromkeys((source, self.engine.inspiration_id)):
            self.validate_document(scratch.page_document(page))
        if not self.can_detach_branch_drag(mime):
            return False
        self._inspiration_stay = True
        self._committing_branch_drag = True
        try:
            with self.engine.transaction("拆出独立想法"):
                self.engine.move_to_inspiration(roots, positions=positions)
        finally:
            self._inspiration_stay = False
            self._committing_branch_drag = False
        self.inspirations_detached.emit(roots)
        return True

    def can_receive_branch_drag(self, mime, parent_id=None) -> bool:
        """只读检查当前画布能否接收本文件的分支载荷，返回布尔值。

        parent_id 省略时只检查拖入资格，不代表空白可提交；指定时须属于当前
        画布且能接收普通子主题。自行、后代及所属附属页目标一律拒绝。
        检查不复制或改写内容，可重复调用；GUI 线程调用，释放后返回 False。
        """
        data = self._branch_drag_data(mime)
        if data is None or self.current_page_kind == "detail":
            return False
        pages = {self.engine.node_page_id(key) for key in data.node_ids}
        if len(pages) != 1:
            return False
        source = next(iter(pages))
        if source != self.engine.inspiration_id and source != self.current_page_id:
            return False
        if data.origin == "sidebar" and any(
                key not in self._book_document.inspiration.root_ids for key in data.node_ids):
            return False
        if source != self.engine.inspiration_id and self._book_document.page_record(source).root_id in data.node_ids:
            return False
        if parent_id is None:
            return True
        if parent_id not in self._document.nodes or self._document.nodes[parent_id].submap_id:
            return False
        return not any(parent_id in self._book_document.owned_descendants(key) for key in data.node_ids)

    def receive_branch_drag(self, mime, parent_id: str, *, index: int | None = None,
                            direction: str | None = None) -> bool:
        """把有效分支迁入当前父主题的指定位置，留在目标画布并选中移动分支。

        mime 来自本会话，parent_id 必须在当前页；index 为移除本次移动根后的
        子节点插入下标，省略时追加。direction 可指定双向布局根主题的左右分支，
        其他位置只能省略。两个关键字均省略时保留旧的追加语义。
        非法下标或方向抛出 MindMapError，无效载荷或无变化返回 False。先独立
        试算并复查内容版本和目标页，再把迁入、排序、方向和展开作为一条可撤销
        事务提交。节点样式及附属内容保持原样；仅限 Qt GUI 线程调用。
        """
        if not self.can_receive_branch_drag(mime, parent_id):
            return False
        self._prepare_page_navigation()
        if not self.can_receive_branch_drag(mime, parent_id):
            return False
        data = self._branch_drag_data(mime)
        roots = list(data.node_ids)
        source = self.engine.node_page_id(roots[0])
        document = self._document
        context = self.current_page_id, self.current_inspiration_root_id
        siblings = [key for key in document.nodes[parent_id].children if key not in roots]
        if index is not None and (type(index) is not int or not 0 <= index <= len(siblings)):
            raise MindMapError("拖放插入位置已失效")
        if direction not in (None, "left", "right") or (direction is not None and (
                document.layout.direction != "both" or parent_id != document.root_id)):
            raise MindMapError("拖放分支方向不适用于当前目标")
        proposal = _DropProposal(tuple(roots), parent_id, len(siblings) if index is None else index, direction)

        def apply(engine):
            if source == self.engine.inspiration_id and self.current_page_id != source:
                engine.attach_inspirations(roots, parent_id)
            if index is not None or direction is not None:
                _apply_move(engine, proposal, document)
            elif source != self.engine.inspiration_id or self.current_page_id == source:
                engine.move_nodes(roots, parent_id, "inside")

        scratch = MindMapEngine(self.engine.document)
        apply(scratch)
        self.validate_document(scratch.page_document(
            self.current_page_id, inspiration_root_id=self.current_inspiration_root_id))
        if (context != (self.current_page_id, self.current_inspiration_root_id)
                or not self.can_receive_branch_drag(mime, parent_id)):
            return False
        revision = self.engine.revision
        self._inspiration_stay = True
        self._committing_branch_drag = True
        try:
            with self.engine.transaction("拖入分支"):
                apply(self.engine)
        finally:
            self._inspiration_stay = False
            self._committing_branch_drag = False
        changed = self.engine.revision != revision
        if changed:
            self.select_nodes(roots)
            self.view.ensureVisible(self.node_items[roots[0]], 35, 35)
            self._update_history_view_after()
        return changed

    def start_external_branch_drag(self, node_ids):
        """将画布内部鼠标手势安全交给 Qt 原生拖放，取消时不提交节点修改。

        node_ids 为内部拖动已归并的根标识；释放场景捕获和预览后才进入 QDrag。
        原生循环期间版本变化、关闭或 Esc 可取消，宿主不应直接调用此内部适配。
        返回实际 Qt 放下动作，所有资源在 finally 清理；仅在 GUI 线程调用。
        """
        mime = self.create_branch_drag(node_ids)
        self.view.cancel_interaction()
        if mime is None:
            return Qt.DropAction.IgnoreAction
        drag = QDrag(self.view)
        drag.setMimeData(mime)
        self._native_branch_drag = drag
        try:
            return drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._native_branch_drag = None
            if not self._disposed and isValid(self.view):
                self.view.node_drag.cancel_external()
            if isValid(drag):
                drag.deleteLater()

    def _update_history_view_after(self):
        if self._view_undo:
            before, _ = self._view_undo[-1]
            if before is not None:
                self._view_undo[-1] = (before, self._history_view_snapshot())
