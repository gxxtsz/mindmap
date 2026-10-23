"""页面路径控件与会话内视图恢复；所有导航均独立于内容事务和文件保存。"""

from dataclasses import dataclass, replace
from typing import Mapping

from mindmap_core import ChangeEvent, MindMapError, Theme
from mindmap_qt_ui.icons import toolbar_icon
from mindmap_qt_ui.ui_style import apply_ui_palette, dialog_styles, ui_colors
from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMenu, QMessageBox, QToolButton, QWidget


class PathNavigation(QWidget):
    """固定高度的页面路径栏；空间不足时收起中间页面，每一级仍可直接跳转。"""

    navigate_requested = Signal(object, str)
    back_requested = Signal()
    favorite_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mindmap_path_navigation")
        self.setAccessibleName("导图路径导航")
        self.setFixedHeight(36)
        self.setMinimumWidth(0)
        self.entries, self.selection_count = [], 0
        self.page_kinds = {}
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
        # 星标独立于页面路径项，路径省略、主题切换和窗口重排均复用同一按钮。
        self.favorite_button = QToolButton(self)
        self.favorite_button.setObjectName("mindmap_page_favorite")
        self.favorite_button.setCheckable(True)
        self.favorite_button.setFixedSize(28, 28)
        self.favorite_button.setIconSize(QSize(16, 16))
        self.favorite_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.favorite_button.clicked.connect(self.favorite_requested)
        self.favorite_button.hide()
        self._favorite_visible = False
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._rebuild)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    @property
    def full_path(self):
        """完整页面路径不受省略布局影响，供提示、复制及无障碍描述使用。"""
        return " › ".join(entry.text for entry in self.entries)

    def set_path(self, entries, *, page_id=None, selection_count=0,
                 page_kinds: Mapping[str | None, str] | None = None):
        """同步页面根路径、类型图标和选择数量，不修改引擎路径对象。

        page_kinds 将页面 ID 映射到 main、submap、detail，由导航层从文档查询。
        省略时兼容旧调用：None 使用主导图图标，其余使用子导图图标。映射仅
        用于显示，不参与路径文字、复制内容或跳转依据；类型变化同样触发重建。
        """
        entries = list(entries)
        kinds = {entry.page_id: (page_kinds or {}).get(
            entry.page_id, "main" if entry.page_id is None else "submap") for entry in entries}
        signature = (tuple(entries), tuple(kinds.items()), page_id, selection_count)
        if signature == self._signature:
            return
        self._signature = signature
        self.entries, self.selection_count, self.page_kinds = entries, selection_count, kinds
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

    def set_favorite_state(self, *, visible: bool, checked: bool, enabled: bool) -> None:
        """同步当前页星标，仅修改界面，不提交收藏或发送操作信号。

        visible 指示当前页是否为子导图；checked 表示已收藏；enabled 由宿主根据
        只读及编辑状态决定。调用返回 None，按钮在路径重排中保持同一实例；
        收藏请求通过 favorite_requested 交回宿主校验，不把标识写入显示文字。
        """
        changed = self._favorite_visible != visible
        self._favorite_visible = visible
        self.favorite_button.setChecked(checked)
        self.favorite_button.setEnabled(enabled)
        text = "取消收藏当前子导图" if checked else "收藏当前子导图"
        self.favorite_button.setToolTip(text)
        self.favorite_button.setAccessibleName(text)
        self._sync_favorite_icon()
        if changed:
            self._rebuild()

    def _sync_favorite_icon(self):
        colors = self._colors or ui_colors(Theme())
        checked = self.favorite_button.isChecked()
        self.favorite_button.setIcon(toolbar_icon(
            "favorite_on" if checked else "favorite", colors.accent if checked else colors.muted,
            colors.border, colors.accent))

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
        """按图标加文字的实际宽度重排；长路径只收起中间页面，不丢失类型和跳转。"""
        # 重排只改变本控件布局，不改变路径内容；清理旧控件可避免频繁选择造成堆积。
        while self.row.count() > 1:
            item = self.row.takeAt(1)
            if item.widget() and item.widget() is not self.favorite_button:
                item.widget().hide()
                item.widget().deleteLater()
        self.favorite_button.setVisible(self._favorite_visible)
        self._sync_favorite_icon()
        if not self.entries:
            self.row.addStretch()
            return
        colors = self._colors or ui_colors(Theme())
        metrics = self.fontMetrics()
        # 预留 16 像素图标、4 像素间隔及原文字内边距；省略文字时同样扣除。
        icon_space = 20
        widths = [min(180, metrics.horizontalAdvance(entry.text.replace("\n", " ")) + 14 + icon_space)
                  for entry in self.entries]
        available = max(70, self.width() - 65 - (80 if self.selection_count > 1 else 0)
                        - (32 if self._favorite_visible else 0))
        indices = list(range(len(self.entries)))
        hidden = []
        if len(indices) > 2 and sum(widths) + 14 * (len(indices) - 1) > available:
            start, used = len(indices) - 1, widths[0] + widths[-1] + 55
            while start > 1 and used + widths[start - 1] + 14 <= available:
                start -= 1
                used += widths[start] + 14
            hidden = list(range(1, start))
            indices = [0, None, *range(start, len(self.entries))]
        # 两端加省略按钮也放不下时，将所有祖先放入菜单，仍保留当前页。
        if len(indices) > 1 and available < 166:
            hidden = list(range(len(self.entries) - 1))
            indices = [None, len(self.entries) - 1]
        page_count = len(indices) - (1 if hidden else 0)
        page_budget = available - 14 * (len(indices) - 1) - (30 if hidden else 0)
        page_width = max(32, page_budget // max(1, page_count))
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
                    kind = self.page_kinds[entry.page_id]
                    action.setIcon(toolbar_icon(kind, colors.muted, colors.border, colors.accent))
                    action.triggered.connect(lambda checked=False, item=entry:
                                             self.navigate_requested.emit(item.page_id, item.node_id))
                button.setMenu(menu)
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                button.setFixedWidth(30)
            else:
                entry = self.entries[index]
                width = min(widths[index], page_width)
                text = entry.text.replace("\n", " ")
                kind = self.page_kinds[entry.page_id]
                button.setProperty("page_kind", kind)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                button.setIconSize(QSize(16, 16))
                color = colors.accent if index == len(self.entries) - 1 else colors.muted
                button.setIcon(toolbar_icon(kind, color, colors.border, colors.accent))
                button.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, width - 12 - icon_space))
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
        if self._favorite_visible:
            self.row.addWidget(self.favorite_button)
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
        self.path_navigation.favorite_requested.connect(self._toggle_current_page_favorite)
        layout.addWidget(self.path_navigation)

    def _toggle_current_page_favorite(self):
        # 可选中按钮会先切换外观，输入法检查可能拒绝操作；无论成功或失败都以
        # 正式文档重新同步星标，避免候选期间显示尚未真正保存的收藏状态。
        self.call(lambda: self.set_submap_favorite(
            self.current_page_id, not self.engine.is_submap_favorite(self.current_page_id)))
        self._sync_navigation()

    @property
    def current_page_id(self):
        """当前显示页面，None 表示主导图；查询不改变引擎修订号或修改状态。"""
        return self._current_page_id

    @property
    def current_page_kind(self):
        """查询活动页面类别，菜单可据此隐藏详情页内禁止使用的两类创建入口。"""
        return self._book_document.page_kind(self._current_page_id)

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
        if not self._book_document.has_page(page_id):
            raise MindMapError("导图页面不存在")
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
        """为当前普通叶子创建并进入子导图，返回新页面 ID。

        无参数；纯图片、只读或输入法候选会抛出 MindMapError，纯图片检查先于
        草稿提交。创建属于内容历史，进入属于临时导航；只在 GUI 线程、画布
        存活时调用，页面及图元由所属文档和画布管理。
        """
        self._ensure_editable()
        if self.engine.get_node(self.current_node_id()).is_image_node:
            raise MindMapError("纯图片节点不能拥有详情页或子导图")
        self._prepare_page_navigation()
        page_id = self.engine.create_submap(self.current_node_id())
        self._switch_page(page_id)
        return page_id

    def set_submap_favorite(self, page_id: str, favorite: bool) -> None:
        """通过画布修改指定子导图收藏，返回 None，不切换当前页面或强制定位。

        page_id 必须指向当前文件的子导图，favorite 必须为布尔值。只读、无效目标
        或尚未完成的输入法候选抛出 MindMapError，文档及草稿保持原状。实际修改前
        请求面板提交有效草稿并完成正文编辑；重复状态直接返回，避免无意义提交。
        界面应通过 call() 调用本接口，把错误交给统一错误信号；历史和保存由引擎负责。
        """
        if not self.editable:
            raise MindMapError("只读模式不能修改子导图收藏")
        if not isinstance(page_id, str) or page_id not in self._book_document.submaps:
            raise MindMapError("只能收藏当前文件中有效的子导图")
        if not isinstance(favorite, bool):
            raise MindMapError("子导图收藏状态必须为布尔值")
        if self.engine.is_submap_favorite(page_id) == favorite:
            return
        if self.is_composing or not all(guard() for guard in tuple(self._navigation_guards)):
            raise MindMapError("请先完成输入法候选，再修改子导图收藏")
        self.edits_flush_requested.emit()
        self.finish_edit(commit=True)
        self.engine.set_submap_favorite(page_id, favorite)

    def toggle_submap_favorite(self, node_id: str | None = None) -> None:
        """切换入口节点拥有的子导图收藏，省略 node_id 时使用当前节点。

        该接口供节点右键动作使用，目标与当前显示页面的星标相互独立。节点没有
        子导图或标识无效时抛出 MindMapError；只读及输入检查复用 set_submap_favorite。
        调用返回 None，不创建页面、不导航，成功后由文档事件同步全部收藏入口。
        """
        node = self.engine.get_node(node_id or self.current_node_id())
        if not node.submap_id:
            raise MindMapError("此节点没有子导图")
        self.set_submap_favorite(node.submap_id, not self.engine.is_submap_favorite(node.submap_id))

    def enter_detail_page(self, node_id=None):
        """查看节点详情；只读也可进入，重新进入恢复该页自己的视图现场。"""
        key = node_id or self.current_node_id()
        node = self.engine.get_node(key)
        if not node.detail_page_id:
            raise MindMapError("此节点没有详情页")
        self._prepare_page_navigation()
        self.select_nodes([key])
        self._switch_page(node.detail_page_id)

    def create_detail_page(self):
        """为当前普通节点添加详情并进入，返回新页面 ID。

        无参数；纯图片、只读、已有详情或详情页内目标及输入法候选抛出
        MindMapError，纯图片检查先于草稿提交。仅在 GUI 线程、画布存活时调用；
        页面由文档管理，切页会释放旧图元，外部只保存稳定页面 ID。
        """
        self._ensure_editable()
        if self.engine.get_node(self.current_node_id()).is_image_node:
            raise MindMapError("纯图片节点不能拥有详情页或子导图")
        self._prepare_page_navigation()
        page_id = self.engine.create_detail_page(self.current_node_id())
        self._switch_page(page_id)
        return page_id

    def delete_detail_page(self):
        """确认详情删除范围，保留入口、原分支及子导图；取消不触碰文档或历史。"""
        self._ensure_editable()
        key = self.current_node_id()
        node = self.engine.get_node(key)
        if not node.detail_page_id:
            raise MindMapError("此节点没有详情页")
        page = self._book_document.detail_pages[node.detail_page_id]
        count = len(self._book_document.descendants(page.root_id))
        generation, revision = self.engine.document_generation, self.engine.revision
        choice = QMessageBox.question(
            self, "删除详情页", f"将删除此详情页及其 {count} 个节点，保留入口节点、原分支和子导图。\n删除后可以撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if choice != QMessageBox.StandardButton.Yes:
            return
        self._ensure_editable()
        if (generation, revision) != (self.engine.document_generation, self.engine.revision):
            raise MindMapError("确认期间导图发生变化，请重新删除")
        self._prepare_page_navigation()
        self.engine.remove_detail_page(key)

    def return_to_parent_page(self):
        """按页面所有权返回一层，不等同于编辑撤销或浏览历史后退。"""
        if self._current_page_id is None:
            return
        self._prepare_page_navigation()
        owner = self._book_document.page_record(self._current_page_id).owner_id
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
        details = sum(bool(self._book_document.nodes[item].detail_page_id) for item in nodes)
        generation, revision = self.engine.document_generation, self.engine.revision
        choice = QMessageBox.question(self, "删除子导图",
                                      f"将删除 {count} 张子导图、{details} 张详情页、{len(nodes)} 个节点，"
                                      "保留入口节点及其详情页。\n删除后可以撤销。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if choice != QMessageBox.StandardButton.Yes:
            return
        self._ensure_editable()
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
        pages = [replace(entry, text=f"{entry.text} · 详情")
                 if self._book_document.page_kind(entry.page_id) == "detail" else entry for entry in pages]
        self.path_navigation.back_button.setToolTip(
            "返回所属导图" if self.current_page_kind == "detail" else "返回上级导图")
        self.path_navigation.back_button.setAccessibleName(self.path_navigation.back_button.toolTip())
        self.path_navigation.set_path(pages, page_id=self._current_page_id,
                                      selection_count=len(self.selected_node_ids()),
                                      page_kinds={entry.page_id: self._book_document.page_kind(entry.page_id)
                                                  for entry in pages})
        self.path_navigation.set_ui_colors(ui_colors(self._ui_colors_override or self._document.theme))
        self.path_navigation.set_favorite_state(
            visible=self.current_page_kind == "submap",
            checked=self.engine.is_submap_favorite(self._current_page_id),
            enabled=self.editable and not self.is_editing)
