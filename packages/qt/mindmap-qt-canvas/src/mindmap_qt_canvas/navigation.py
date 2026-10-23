"""页面路径控件与会话内视图恢复；所有导航均独立于内容事务和文件保存。"""

from dataclasses import dataclass

from mindmap_core import ChangeEvent, MindMapError
from mindmap_qt_ui.icons import toolbar_icon
from mindmap_qt_ui.ui_style import apply_ui_palette, dialog_styles, ui_colors
from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMenu, QMessageBox, QToolButton, QWidget


class PathNavigation(QWidget):
    """固定高度的页面路径栏；空间不足时收起中间页面，每一级仍可直接跳转。"""

    navigate_requested = Signal(object, str)
    back_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mindmap_path_navigation")
        self.setAccessibleName("导图路径导航")
        self.setFixedHeight(36)
        self.setMinimumWidth(0)
        self.entries, self.selection_count = [], 0
        self._signature = None
        self._colors = None
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(8, 3, 8, 3)
        self.row.setSpacing(3)
        self.back_button = QToolButton(self)
        self.back_button.setToolTip("返回上级导图")
        self.back_button.setAccessibleName("返回上级导图")
        self.back_button.setFixedWidth(26)
        self.back_button.clicked.connect(self.back_requested)
        self.row.addWidget(self.back_button)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._rebuild)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    @property
    def full_path(self):
        """完整页面路径不受省略布局影响，供提示、复制及无障碍描述使用。"""
        return " › ".join(entry.text for entry in self.entries)

    def set_path(self, entries, *, page_id=None, selection_count=0):
        """接收导航层筛选的页面根路径，仅在页面内容或选择数量变化时重建控件。"""
        signature = (tuple(entries), page_id, selection_count)
        if signature == self._signature:
            return
        self._signature = signature
        self.entries, self.selection_count = list(entries), selection_count
        self.back_button.setEnabled(page_id is not None)
        self.setToolTip(self.full_path)
        self.setAccessibleDescription(self.full_path)
        self._rebuild()

    def set_ui_colors(self, colors):
        """独立跟随外壳色板，保证深浅主题和菜单均可辨认，不修改导图主题。"""
        if colors == self._colors:
            return
        self._colors = colors
        apply_ui_palette(self, colors)
        self.back_button.setIcon(toolbar_icon("page_up", colors.muted, colors.border, colors.accent))
        self.back_button.setIconSize(QSize(16, 16))
        self.setStyleSheet(
            f"QWidget#mindmap_path_navigation {{background:{colors.field}; border-bottom:1px solid {colors.border};}}"
            f"QToolButton {{color:{colors.muted}; border:0; border-radius:4px; padding:3px 5px;}}"
            f"QToolButton:hover {{background:{colors.hover}; color:{colors.text};}}"
            f"QToolButton:disabled {{color:{colors.border};}}"
            f"QToolButton[last=true] {{color:{colors.accent}; font-weight:600;}}"
            f"QLabel {{color:{colors.muted}; background:transparent;}}" + dialog_styles(colors)
        )
        self._rebuild()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_timer.start(0)

    def _menu(self):
        menu = QMenu(self)
        if self._colors:
            apply_ui_palette(menu, self._colors)
        return menu

    def _context_menu(self, position):
        menu = self._menu()
        menu.addAction("复制完整路径", lambda: QApplication.clipboard().setText(self.full_path))
        menu.exec(self.mapToGlobal(position))
        menu.deleteLater()

    def _rebuild(self):
        # 重排只改变本控件布局，不改变路径内容；清理旧控件可避免频繁选择造成堆积。
        while self.row.count() > 1:
            item = self.row.takeAt(1)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if not self.entries:
            self.row.addStretch()
            return
        metrics = self.fontMetrics()
        # 页面项统一使用纯文字，仅保留文字按钮内边距，不再为页面图标预留宽度。
        widths = [min(180, metrics.horizontalAdvance(entry.text.replace("\n", " ")) + 14)
                  for entry in self.entries]
        available = max(70, self.width() - 65 - (80 if self.selection_count > 1 else 0))
        indices = list(range(len(self.entries)))
        hidden = []
        if len(indices) > 2 and sum(widths) + 14 * (len(indices) - 1) > available:
            start, used = len(indices) - 1, widths[0] + widths[-1] + 55
            while start > 1 and used + widths[start - 1] + 14 <= available:
                start -= 1
                used += widths[start] + 14
            hidden = list(range(1, start))
            indices = [0, None, *range(start, len(self.entries))]
        for offset, index in enumerate(indices):
            if offset:
                separator = QLabel("›", self)
                self.row.addWidget(separator)
                separator.show()
            button = QToolButton(self)
            button.setAutoRaise(True)
            if index is None:
                button.setText("…")
                button.setToolTip("查看中间的全部页面")
                menu = self._menu()
                for hidden_index in hidden:
                    entry = self.entries[hidden_index]
                    action = menu.addAction(entry.text)
                    action.triggered.connect(lambda checked=False, item=entry:
                                             self.navigate_requested.emit(item.page_id, item.node_id))
                button.setMenu(menu)
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                button.setFixedWidth(30)
            else:
                entry = self.entries[index]
                width = min(widths[index], max(40, available // 2))
                text = entry.text.replace("\n", " ")
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                button.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, width - 12))
                button.setFixedWidth(width)
                button.setToolTip(entry.text)
                button.setAccessibleName(entry.text)
                button.setProperty("last", index == len(self.entries) - 1)
                button.clicked.connect(lambda checked=False, item=entry:
                                       self.navigate_requested.emit(item.page_id, item.node_id))
            self.row.addWidget(button)
            button.show()
        self.row.addStretch()
        if self.selection_count > 1:
            badge = QLabel(f"已选 {self.selection_count} 项", self)
            self.row.addWidget(badge)
            badge.show()
        self.row.activate()


@dataclass
class PageViewState:
    """当前会话的视图快照；focus_id 仅保存导航临时显示范围，不提供手动聚焦入口。

    快照只记录显示状态，不复制节点内容或改写折叠字段，也不进入文档撤销历史。
    """

    transform: QTransform
    center: QPointF
    selection: tuple[str, ...]
    focus_id: str | None


class PageNavigationMixin:
    """画布内部页面协调器；文件引擎始终保持同一个实例和统一历史。"""

    def _init_page_navigation(self, layout):
        self._current_page_id = None
        self._page_states = {}
        self._navigation_guards = []
        self.path_navigation = PathNavigation(self)
        self.path_navigation.back_requested.connect(lambda: self.call(self.return_to_parent_page))
        self.path_navigation.navigate_requested.connect(
            lambda page, node: self.call(lambda: self.navigate_to(page, node)))
        layout.addWidget(self.path_navigation)

    @property
    def current_page_id(self):
        """当前显示页面，None 表示主导图；查询不改变引擎修订号或修改状态。"""
        return self._current_page_id

    def register_navigation_guard(self, callback):
        """面板注册切页前的输入检查；返回 False 时保留原页，返回取消注册函数。"""
        self._navigation_guards.append(callback)

        def remove():
            if callback in self._navigation_guards:
                self._navigation_guards.remove(callback)

        return remove

    def _prepare_page_navigation(self):
        if self.is_composing or not all(guard() for guard in tuple(self._navigation_guards)):
            raise MindMapError("请先完成输入法候选，再切换导图页面")
        self.edits_flush_requested.emit()
        self.finish_edit(commit=True)

    def _cache_page_view(self):
        # 首次居中尚未执行时先完成请求，防止快速进入再返回恢复到初始化占位视口。
        if self._view_reset_pending and self.view.isVisible():
            self._apply_view_reset()
        center = self.view._hidden_center
        if center is None:
            center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self._page_states[self._current_page_id] = PageViewState(
            QTransform(self.view.transform()), QPointF(center), tuple(self.selected_node_ids()), self.focus_id)

    def _switch_page(self, page_id, *, notify=True, cache=True):
        """只切换页内读模型和视图；撤销导致页面消失时由调用方先选择有效祖先。"""
        if page_id is not None and page_id not in self._book_document.submaps:
            raise MindMapError("子导图不存在")
        event = ChangeEvent("切换导图页面", (), self.engine.revision, "navigation")
        if notify:
            self.document_changing.emit(event)
        if cache:
            self._cache_page_view()
        self.view.cancel_interaction()
        self._cancel_branch_menu()
        self._view_reset_timer.stop()
        self._view_reset_pending = False
        # 恢复缩放时，Qt 的 setTransform 会同步触发 scrollContentsBy，进而刷新视口图元。
        # 此刻即将换成新页节点，旧页布局和测量缓存还未替换，不能继续按旧布局取节点。
        # 先停用旧页的视口同步；refresh 会用新页的布局重新确定是否启用虚拟化并重建图元。
        self._virtualized = False
        self._current_page_id = page_id
        self._document = self._book_document.page_view(page_id)
        self._page_chain = [entry.page_id for entry in self.engine.node_path(self._document.root_id)
                            if entry.page_root]
        self._node_parents = self._document.parents()
        self._node_order = None
        self._incremental_layout = None
        self._visual_document = None
        self._measure_dirty = None
        state = self._page_states.get(page_id)
        self.focus_id = state.focus_id if state and state.focus_id in self._document.nodes else None
        self.view.setTransform(state.transform if state else QTransform())
        self.refresh()
        ids = [key for key in state.selection if key in self._document.nodes] if state else []
        self.select_nodes(ids if state and not state.selection else ids or [self._document.root_id])
        if state:
            self.view.update_navigation_rect(center=state.center)
        else:
            self.reset_view()
        if not self.view.isVisible():
            self.view._hidden_center = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        self._sync_navigation()
        self._update_actions()
        if notify:
            self.document_changed.emit(event)
        self.page_changed.emit(page_id)

    def navigate_to(self, page_id, node_id=None):
        """先恢复目标页现场，再定位节点；被折叠隐藏的目标只临时显示，不改写文档。

        focus_id 只为导航保留：先退出旧的临时范围，仍不可见时才以目标为显示中心。
        导航不会自动适应窗口，也不新增手动进入或退出分支显示范围的菜单与快捷键。
        """
        self._prepare_page_navigation()
        if node_id is not None and self.engine.node_page_id(node_id) != page_id:
            raise MindMapError("路径节点与目标页面不一致")
        if page_id != self._current_page_id:
            self._switch_page(page_id)
        if node_id is not None:
            if node_id not in self.layout_result.nodes:
                self.focus_id = None
                self.refresh()
            if node_id not in self.layout_result.nodes:
                self.focus_id = node_id
                self.refresh()
            self.select_nodes([node_id])
            self.view.ensureVisible(self.node_items[node_id].sceneBoundingRect(), 35, 35)

    def enter_submap(self, node_id=None):
        """进入入口拥有的子导图，曾访问的页面恢复视图，首次访问使用原始倍率。"""
        key = node_id or self.current_node_id()
        node = self.engine.get_node(key)
        if not node.submap_id:
            raise MindMapError("此节点没有子导图")
        self._prepare_page_navigation()
        self.select_nodes([key])
        self._switch_page(node.submap_id)

    def create_submap(self):
        """创建后立即进入新页面；创建属于内容历史，随后进入属于临时导航。"""
        self._ensure_editable()
        self._prepare_page_navigation()
        page_id = self.engine.create_submap(self.current_node_id())
        self._switch_page(page_id)
        return page_id

    def return_to_parent_page(self):
        """按页面所有权返回一层，不等同于编辑撤销或浏览历史后退。"""
        if self._current_page_id is None:
            return
        self._prepare_page_navigation()
        owner = self._book_document.submaps[self._current_page_id].owner_id
        self._switch_page(self.engine.node_page_id(owner))

    def delete_submap(self):
        """确认影响范围后删除所有下级页面，取消不改变内容、选择和历史。"""
        self._ensure_editable()
        key = self.current_node_id()
        node = self.engine.get_node(key)
        if not node.submap_id:
            raise MindMapError("此节点没有子导图")
        root = self._book_document.submaps[node.submap_id].root_id
        nodes = self._book_document.owned_descendants(root)
        count = 1 + sum(bool(self._book_document.nodes[item].submap_id) for item in nodes)
        generation, revision = self.engine.document_generation, self.engine.revision
        choice = QMessageBox.question(self, "删除子导图",
                                      f"将删除 {count} 张子导图、{len(nodes)} 个节点，保留入口节点。\n删除后可以撤销。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if choice != QMessageBox.StandardButton.Yes:
            return
        if (generation, revision) != (self.engine.document_generation, self.engine.revision):
            raise MindMapError("确认期间导图发生变化，请重新删除")
        self._prepare_page_navigation()
        self.engine.remove_submap(key)

    def _sync_navigation(self):
        """路径始终终止于当前页根节点，普通节点选择只影响数量提示。"""
        if (not hasattr(self, "_document") or self._disposed
                or getattr(self, "_applying_engine_event", False)):
            return
        # 引擎仍提供完整节点祖先路径，界面仅投影其中的页面根，避免普通分支撑长路径栏。
        # 必须保留原有页面、节点 ID：同名页面及由根节点连续下钻的页面不能按标题合并。
        pages = [entry for entry in self.engine.node_path(self._document.root_id) if entry.page_root]
        self.path_navigation.set_path(pages, page_id=self._current_page_id,
                                      selection_count=len(self.selected_node_ids()))
        self.path_navigation.set_ui_colors(ui_colors(self._ui_colors_override or self._document.theme))
