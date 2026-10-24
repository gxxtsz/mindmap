"""独立想法画布协调、兼容坐标落位和无损分支迁移的界面入口。"""

from mindmap_core import ChangeEvent, MindMapError, MindMapPageView
from mindmap_qt_ui.ui_style import apply_ui_palette, dialog_styles
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .inspiration_state import InspirationViewStateMixin
from .transfers import BranchTransferMixin


class InspirationCanvasMixin(BranchTransferMixin, InspirationViewStateMixin):
    """复用同一个引擎、画布和面板，区域切换不创建新文件会话。"""

    def inspiration_positions(self, roots, document=None):
        """计算自动移入位置；旧树不移动，新树按八十逻辑像素间距排列。

        roots 是将要独立摆放的分支根，document 可指定剪贴板试算读模型。
        返回新的坐标映射，不提交编辑；布局异常沿用宿主错误处理。
        """
        book = self._book_document
        area = book.inspiration
        existing = book.page_view(area.id)
        visuals, sizes = self.factory.measure(existing)
        result = self.layout_registry.layout(existing, sizes)
        x, y = (result.bounds.right + 80, result.bounds.y) if existing.nodes else (0, 0)
        source = document or book
        positions = {}
        for root in roots:
            tree = MindMapPageView(root, {key: source.nodes[key] for key in source.descendants(root)},
                                   source.assets, area.layout, area.theme, source.metadata)
            _, sizes = self.factory.measure(tree)
            layout = self.layout_registry.layout(tree, sizes)
            rect = layout.nodes[root]
            # 第一棵树的根在原点；其他树按实际内容边界避免新建时覆盖既有内容。
            px = x + rect.x - layout.bounds.x if existing.nodes else x
            py = y + rect.y - layout.bounds.y if positions else y
            positions[root] = (px, py)
            y = py + layout.bounds.bottom - rect.y + 80
        return positions

    def show_inspiration(self, root_id=None):
        """打开单个想法；省略标识时恢复最近有效想法，无内容则显示空态。

        root_id 必须是本文件现有游离根，非法标识抛出 MindMapError，不改变现场。
        提交有效草稿后导航，只读允许浏览，输入法候选阻止切换；不产生内容历史。
        所有图形操作发生在 GUI 线程，节点与附属页继续归同一文档持有。
        """
        if root_id is not None and root_id not in self._book_document.inspiration.root_ids:
            raise MindMapError("独立想法不存在")
        self._prepare_page_navigation()
        root = self._choose_inspiration_root(root_id or self._last_inspiration_root)
        if self.current_page_id != self.engine.inspiration_id or self._inspiration_root_id != root:
            self._switch_page(self.engine.inspiration_id, inspiration_root_id=root)
        else:
            self.inspiration_sidebar_requested.emit()

    def add_inspiration(self, position=None):
        """新建游离主题并编辑正文，返回稳定 ID；position 为可选逻辑像素坐标。

        省略时按旧文件坐标规则自动落位，画布始终把新根展示在原点，不改写其他
        想法的存储坐标。只读或输入法候选时拒绝，不丢弃草稿。新建只产生一次
        结构历史并打开新想法，文字编辑按现有机制单独提交；仅在 GUI 线程调用。
        """
        self._ensure_editable()
        self._prepare_page_navigation()
        if position is None:
            area = self._book_document.page_view(self.engine.inspiration_id)
            _, sizes = self.factory.measure(area)
            bounds = self.layout_registry.layout(area, sizes).bounds
            position = (bounds.right + 80, bounds.y) if area.nodes else (0, 0)
        self._creating_inspiration = True
        try:
            key = self.engine.add_inspiration(position=position)
        finally:
            self._creating_inspiration = False
        self.select_nodes([key])
        self.view.ensureVisible(self.node_items[key], 35, 35)
        self._update_history_view_after()
        self.begin_edit(key)
        return key

    def move_selected_to_inspiration(self):
        """整条移出当前选中分支，所有坐标和归属在一次引擎提交中完成。"""
        self._ensure_editable()
        self._prepare_page_navigation()
        selected = set(self.selected_node_ids())
        roots = []
        for key in self.selected_node_ids():
            parent = self._node_parents.get(key)
            while parent is not None and parent not in selected:
                parent = self._node_parents.get(parent)
            if parent is None:
                roots.append(key)
        if not roots:
            return []
        positions = self.inspiration_positions(roots)
        self._inspiration_target_root = roots[0]
        try:
            return self.engine.move_to_inspiration(roots, positions=positions)
        finally:
            self._inspiration_target_root = None

    def group_selected_inspirations(self):
        """为选中主题创建共同父节点并进入改名，重叠选择由引擎归并。"""
        self._ensure_editable()
        self._prepare_page_navigation()
        ids = self.selected_node_ids()
        if not ids:
            return None
        rect = self.layout_result.nodes[ids[0]]
        key = self.engine.group_inspirations(ids, position=(rect.x, rect.y))
        self.select_nodes([key])
        self.begin_edit(key)
        return key

    def choose_inspiration_target(self):
        """显示主导图/子导图目标选择器，返回父主题 ID；取消返回 None，不改内容。"""
        ids = self.selected_node_ids()
        if not ids:
            return None
        book = self._book_document
        blocked = {key for root in ids for key in book.owned_descendants(root)}
        dialog = QDialog(self)
        dialog.setWindowTitle("加入导图")
        dialog.resize(560, 580)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"将 {len(ids)} 条分支加入所选父主题，保留全部内容。", dialog))
        pages = QComboBox(dialog)
        pages.setAccessibleName("目标导图页面")
        pages.addItem("主导图", None)
        for page in book.submaps.values():
            if page.root_id not in blocked:
                path = " › ".join(entry.text for entry in self._page_path_entries(page.root_id))
                pages.addItem(path, page.id)
        search = QLineEdit(dialog)
        search.setPlaceholderText("查找当前页面的父主题")
        tree = QTreeWidget(dialog)
        tree.setHeaderHidden(True)
        tree.setAccessibleName("接收分支的父主题")
        preview = QLabel("选择父主题后，将作为它的最后一个子主题。", dialog)
        preview.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                                  parent=dialog)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("加入导图")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        items = {}

        def populate():
            tree.clear()
            items.clear()
            document = book.page_view(pages.currentData())
            for key in document.ordered_nodes():
                node = document.nodes[key]
                item = QTreeWidgetItem([node.text or "（空主题）"])
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                parent = self.engine.parent_id(key)
                if parent in items:
                    items[parent].addChild(item)
                else:
                    tree.addTopLevelItem(item)
                items[key] = item
                if node.submap_id or key in blocked:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    item.setToolTip(0, "请进入子导图选择父主题" if node.submap_id else "不能移入自身或所属页面")
            tree.expandToDepth(1)
            search.clear()
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        def filter_items(text):
            visible = set()
            for key, item in items.items():
                if text.casefold() in item.text(0).casefold():
                    current = item
                    while current is not None:
                        visible.add(current.data(0, Qt.ItemDataRole.UserRole))
                        current = current.parent()
            for key, item in items.items():
                item.setHidden(key not in visible)
                if text and key in visible:
                    item.setExpanded(True)

        def selected():
            item = tree.currentItem()
            enabled = bool(item and item.isSelected() and not item.isHidden())
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)
            if enabled:
                path = self.engine.node_path(item.data(0, Qt.ItemDataRole.UserRole))
                preview.setText(" › ".join(entry.text for entry in path
                                           if entry.page_id != self.engine.inspiration_id or entry.node_id is not None)
                                + "\n作为最后一个子主题")

        pages.currentIndexChanged.connect(populate)
        search.textChanged.connect(filter_items)
        tree.itemSelectionChanged.connect(selected)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        for control in (pages, search, tree, preview, buttons):
            layout.addWidget(control)
        colors = self._ui_colors
        if colors:
            apply_ui_palette(dialog, colors)
            dialog.setStyleSheet(dialog_styles(colors))
        populate()
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and tree.currentItem():
                return tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
            return None
        finally:
            dialog.deleteLater()

    def attach_selected_inspirations(self):
        """选择父主题后原地迁入导图，取消及迟到的失效选择不提交修改。"""
        self._ensure_editable()
        self._prepare_page_navigation()
        ids = self.selected_node_ids()
        generation = self.engine.document_generation
        parent = self.choose_inspiration_target()
        if parent is None:
            return
        self._ensure_editable()
        if self._disposed or generation != self.engine.document_generation or self.is_composing:
            raise MindMapError("导图或输入状态发生变化，请重新选择目标")
        self.engine.attach_inspirations(ids, parent)

    def _apply_inspiration_event(self, event):
        """区域及跨区提交后重建当前页成员，包含未发生字段变化的随行后代。"""
        self.document_changing.emit(event)
        self.view.node_drag.document_changed()
        self._cancel_branch_menu()
        self._cache_page_view()
        before = self.selected_node_ids()
        old_roots = list(self._book_document.inspiration.root_ids)
        old_root = self._inspiration_root_id
        old_parents = dict(self._node_parents)
        if self.editor is not None:
            self.finish_edit(commit=False)
        event.changes.apply(self._book_document, forward=event.forward)
        page = self.current_page_id
        root = old_root
        moves = event.changes.moves
        transfers = [move for move in moves if move.before.page_id != move.after.page_id]
        if transfers and not self._inspiration_stay:
            placement = transfers[0].after if event.forward else transfers[0].before
            page = placement.page_id
        elif event.kind in ("undo", "redo") and not self._inspiration_stay:
            page = event.changes.page_id
        fresh = [key for key, pair in event.changes.collections.get("nodes", {}).items()
                 if pair[0] is None and pair[1] is not None]
        if self._creating_inspiration:
            page = self.engine.inspiration_id
        pending_root = getattr(self, "_inspiration_target_root", None)
        if pending_root:
            page = self.engine.inspiration_id
        if page == self.engine.inspiration_id:
            moved = [move.node_id for move in transfers]
            fresh_roots = [key for key in fresh if key in self._book_document.inspiration.root_ids]
            preferred = (pending_root or (fresh_roots or moved or [old_root])[0]
                         if not self._inspiration_stay else old_root)
            root = self._choose_inspiration_root(preferred, old_roots)
        if not self._book_document.has_page(page):
            page = self.engine.inspiration_id
        self._switch_page(page, notify=False, cache=False, inspiration_root_id=root)
        after = [key for key in before if key in self._document.nodes]
        if transfers:
            after = [move.node_id for move in transfers if move.node_id in self._document.nodes]
        if event.kind in ("undo", "redo"):
            source = self._selection_undo if event.kind == "undo" else self._selection_redo
            target = self._selection_redo if event.kind == "undo" else self._selection_undo
            if source:
                pair = source.pop()
                target.append(pair)
                if not transfers:
                    after = [key for key in pair[0 if event.kind == "undo" else 1] if key in self._document.nodes]
        if event.kind == "change":
            fresh = [key for key, pair in event.changes.collections.get("nodes", {}).items()
                     if pair[0] is None and pair[1] is not None and key in self._document.nodes]
            fresh_set = set(fresh)
            after = [key for key in fresh if self._node_parents.get(key) not in fresh_set] or after
            self._selection_undo.append((before, after))
            del self._selection_undo[:-self.engine.history_limit]
            self._selection_redo.clear()
        if not after:
            after = [old_parents[key] for key in before if old_parents.get(key) in self._document.nodes]
            after = after or list(self._document.root_ids[:1])
        self.select_nodes(after)
        if transfers and after:
            self.view.ensureVisible(self.node_items[after[0]], 35, 35)
        self._update_actions()
        self.history_changed.emit(self.engine.can_undo, self.engine.can_redo, self.engine.is_dirty)
        # 成员可能整棵进入或离开，面板应重载页内视图，不能仅合并节点字段差量。
        local = ChangeEvent(event.operation, (), event.revision, event.kind, None, event.forward)
        self.document_changed.emit(local)

    def expand_all_trees(self):
        """在同一事务中展开当前区域的全部树，空区不提交修改。"""
        with self.engine.transaction("展开全部"):
            for root in self._document.root_ids:
                self.engine.set_expanded(root, True, recursive=True)
