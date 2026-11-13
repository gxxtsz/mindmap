"""子导图收藏树：只读查询引擎，以信号把导航和内容修改交回宿主。"""

from __future__ import annotations

from mindmap_core import MindMapEngine, Theme
from PySide6.QtCore import QAbstractItemModel, QItemSelectionModel, QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QMenu,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from mindmap_qt_editor.icons import toolbar_icon
from mindmap_qt_editor.theme import UiColors, apply_ui_palette, menu_styles, outline_colors

from ._sidebar_tree import ITEM_RIGHT_MARGIN, SidebarTreeView

_PAGE_ROLE = Qt.ItemDataRole.UserRole + 1
_PATH_ROLE = Qt.ItemDataRole.UserRole + 2
_CURRENT_ROLE = Qt.ItemDataRole.UserRole + 3


class _FavoritesModel(QAbstractItemModel):
    """以页面 ID 关联收藏父子，只持有不可变路径项及独立的显示索引。

    rows 保留文档先序的扁平摘要，供数量和路径检查使用；Qt 行号始终相对于
    当前父项，导航必须通过 index_for(page_id) 定位，不能把摘要序号当作根行号。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.rows = ()
        self.current_page_id = None
        self._entries = {}
        self._parents = {}
        self._children = {None: ()}
        self._row_numbers = {}
        self._identifiers = {}
        self._keys = {}
        self._next_identifier = 1

    def rowCount(self, parent=QModelIndex()):
        """返回指定父项的直接孩子数；无效父索引表示隐藏根。"""
        if parent.isValid() and (parent.model() is not self or parent.column() != 0):
            return 0
        return len(self._children.get(self.page_id(parent), ()))

    def columnCount(self, parent=QModelIndex()):
        """收藏树只有一列，单行显示标题，完整路径保留在悬停及无障碍描述中。"""
        return 1

    def index(self, row, column, parent=QModelIndex()):
        """按局部行号建立整数索引，不向 Qt 交出可能被 Python 回收的对象指针。"""
        if column != 0 or row < 0:
            return QModelIndex()
        if parent.isValid() and (parent.model() is not self or parent.column() != 0):
            return QModelIndex()
        children = self._children.get(self.page_id(parent), ())
        if row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, self._identifiers[children[row]])

    def parent(self, index):
        """返回最近的已收藏祖先；顶层项及无效索引返回无效父索引。"""
        return self.index_for(self._parents.get(self.page_id(index)))

    def page_id(self, index):
        """解析属于本模型的有效索引；不持有 QModelIndex 内的 Python 对象指针。"""
        if not index.isValid() or index.model() is not self:
            return None
        return self._keys.get(index.internalId())

    def index_for(self, page_id: str | None) -> QModelIndex:
        """按稳定页面 ID 定位任意层的收藏项，不展开、不选择、不滚动。

        未收藏、已删除及 None 返回无效索引；返回索引只适用于当前模型结构，
        调用方在模型重置后应再次按页面 ID 查询，不保存过期的行号或索引。
        """
        if page_id not in self._identifiers:
            return QModelIndex()
        return self.createIndex(self._row_numbers[page_id], 0, self._identifiers[page_id])

    def branch_ids(self):
        """迭代当前有孩子的收藏页面，供视图在模型重置前后保存展开状态。"""
        return (page_id for page_id, children in self._children.items() if page_id is not None and children)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        page_id = self.page_id(index)
        if page_id not in self._entries:
            return None
        entry, path = self._entries[page_id]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return entry.text.replace("\n", " ")
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            return f"{entry.text}\n{path}"
        if role == _PAGE_ROLE:
            return entry.page_id
        if role == _PATH_ROLE:
            return path
        if role == _CURRENT_ROLE:
            return entry.page_id == self.current_page_id
        return None

    def set_rows(self, rows, parents, *, reset=False):
        """同步只读摘要及最近收藏祖先映射，返回是否重置了结构。

        仅结构、顺序或整份文档替换才重置 Qt 模型；改名及路径变化逐项发出
        dataChanged，使已有选择、键盘焦点和手动展开不被文本编辑打断。
        """
        entries = {entry.page_id: (entry, path) for entry, path in rows}
        children = {None: [], **{page_id: [] for page_id in entries}}
        for entry, _ in rows:
            children[parents[entry.page_id]].append(entry.page_id)
        children = {page_id: tuple(items) for page_id, items in children.items()}
        structural = reset or children != self._children
        old_entries = self._entries
        if structural:
            self.beginResetModel()
            # internalId 使用不重复的整数；存活页面跨重置保留标识，已移除页面
            # 不会把标识立即让给别的页面，避免迟到索引意外指向同名的新页面。
            self._identifiers = {key: value for key, value in self._identifiers.items() if key in entries}
            for page_id in entries:
                if page_id not in self._identifiers:
                    self._identifiers[page_id] = self._next_identifier
                    self._next_identifier += 1
            self._keys = {value: key for key, value in self._identifiers.items()}
            self._row_numbers = {key: row for items in children.values() for row, key in enumerate(items)}
        self.rows, self._entries, self._parents, self._children = rows, entries, parents, children
        if structural:
            self.endResetModel()
        else:
            for page_id, value in entries.items():
                if value != old_entries[page_id]:
                    index = self.index_for(page_id)
                    self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole,
                                                        Qt.ItemDataRole.ToolTipRole,
                                                        Qt.ItemDataRole.AccessibleTextRole,
                                                        Qt.ItemDataRole.AccessibleDescriptionRole, _PATH_ROLE])
        return structural


class _FavoriteDelegate(QStyledItemDelegate):
    """在共用行样式上补充子导图图标、当前页状态和键盘焦点。"""

    def initStyleOption(self, option, index):
        """把宿主确认的当前页映射为 Qt 的选中样式，保留原生排版与省略处理。

        方向键可把 Qt 选择移到另一行浏览，只有宿主切页后才更新当前页角色。
        因此在绘制选项中单独设置高亮和加粗，同时保留 Qt 提供的焦点、悬停状态。
        """
        super().initStyleOption(option, index)
        colors = self.parent()._colors
        current = bool(index.data(_CURRENT_ROLE))
        selected = QStyle.StateFlag.State_Selected
        if current:
            option.state |= selected
        else:
            option.state &= ~selected

        option.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration
        option.decorationSize = QSize(16, 16)
        # Qt 给已展开的条目使用图标开启状态。普通和开启状态使用同色，让强调色
        # 始终表达宿主确认的当前页，展开状态则由树形三角表达。
        icon_color = colors.accent if current else colors.muted
        option.icon = toolbar_icon("submap", icon_color, colors.muted, icon_color)
        # 同步字体与度量，让当前页加粗后的宽度参与 Qt 的末尾省略计算。
        font = QFont(option.font)
        font.setBold(current)
        option.font = font
        option.fontMetrics = QFontMetrics(font)

    def paint(self, painter, option, index):
        """先按共用样式绘制条目，再用虚线标出 Enter 将操作的键盘焦点行。"""
        super().paint(painter, option, index)
        if not option.state & QStyle.StateFlag.State_HasFocus:
            return
        rect = option.rect.adjusted(1, 1, -ITEM_RIGHT_MARGIN - 1, -1)
        if rect.isEmpty():
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
            painter.setPen(QPen(QColor(self.parent()._colors.accent), 1, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 4, 4)
        finally:
            painter.restore()


class SubmapFavoritesPanel(QWidget):
    """一份文档的收藏面板，不依赖画布、停靠框架或既有 PanelHost 协议。

    engine 为借用的文档引擎，必须在本控件 dispose() 前保持有效；构造及全部图形
    调用发生在 Qt GUI 线程。面板只读订阅引擎，绝不直接修改文档或决定页面导航。
    navigate_requested(page_id) 请求跳转，unfavorite_requested(page_id) 请求取消收藏；
    宿主负责连接安全的导航、修改接口，并调用 set_current_page_id、set_editable 同步状态。
    """

    navigate_requested = Signal(str)
    unfavorite_requested = Signal(str)

    def __init__(self, engine: MindMapEngine, parent: QWidget | None = None):
        """为借用的 engine 建立独立收藏树，parent 决定 Qt 控件所有权。

        只显示已收藏页面，并挂到最近的已收藏祖先下，同级遵循导图原有顺序。
        新分支默认展开，浏览状态只保留在当前控件；构造不会改写收藏或产生历史。
        缺少收藏时显示中文操作提示，永久关闭会话前必须调用 dispose() 解除订阅。
        """
        super().__init__(parent)
        self.engine = engine
        self._disposed = False
        self._editable = True
        self._current_page_id = None
        self.setObjectName("mindmap_submap_favorites")
        self.setAccessibleName("子导图收藏")
        layout = QVBoxLayout(self)
        # 树自身带有与大纲一致的内边距，空态提示单独保留原来的留白。
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)
        self.empty_label = QLabel("暂无收藏\n\n在子导图路径栏点击星标，\n或右键入口节点收藏子导图。", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setMargin(18)
        self._stack.addWidget(self.empty_label)
        self.tree_view = SidebarTreeView(self)
        # 保留旧宿主使用的控件引用；两者始终指向同一棵树，不维护另一份列表。
        self.list_view = self.tree_view
        self.tree_view.setAccessibleName("已收藏的子导图树")
        self.tree_view.setExpandsOnDoubleClick(False)
        self.tree_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree_view.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.tree_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree_view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.model = _FavoritesModel(self)
        self.tree_view.setModel(self.model)
        self.tree_view.setItemDelegate(_FavoriteDelegate(self))
        self.tree_view.clicked.connect(self._navigate)
        self.tree_view.activated.connect(self._navigate)
        self._stack.addWidget(self.tree_view)
        self._menu = QMenu(self)
        self.remove_action = QAction("取消收藏", self)
        self.remove_action.triggered.connect(self._unfavorite)
        self._menu.addAction(self.remove_action)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)
        self._unsubscribe = engine.subscribe(self._engine_changed)
        self.set_ui_colors(None)
        self._refresh()

    def set_current_page_id(self, page_id: str | None) -> None:
        """同步当前显示页并高亮对应收藏，返回 None，不触发导航或修改。

        主导图使用 None，详情页或未收藏页面会清除当前收藏高亮；无效 ID 同样视为
        未命中。隐藏项也保留当前索引，但不自动展开祖先或滚动；释放后调用无操作。
        """
        if self._disposed:
            return
        scroll = self.tree_view.verticalScrollBar().value()
        self._current_page_id = page_id
        self.model.current_page_id = page_id
        index = self.model.index_for(page_id)
        # Qt 在当前项变化时也可能通过 currentChanged 自动定位。同步期间关闭
        # 自动滚动，并直接操作选择模型，避免收起的祖先因高亮同步而被展开；
        # 随后恢复设置，保留用户用上下键浏览时的原生自动滚动行为。
        auto_scroll = self.tree_view.hasAutoScroll()
        self.tree_view.setAutoScroll(False)
        try:
            self.tree_view.selectionModel().setCurrentIndex(
                index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
            if not index.isValid():
                self.tree_view.clearSelection()
        finally:
            self.tree_view.setAutoScroll(auto_scroll)
        self.tree_view.verticalScrollBar().setValue(scroll)
        self.tree_view.viewport().update()

    def set_editable(self, editable: bool) -> None:
        """同步取消收藏入口的可用状态，返回 None，树形浏览与导航仍可使用。

        宿主应传入只读及正文编辑约束合并后的布尔值；本接口只控制界面，实际修改
        仍由宿主在收到 unfavorite_requested 后再次校验，避免菜单期间权限发生变化。
        """
        self._editable = bool(editable) and not self._disposed
        self.remove_action.setEnabled(self._editable)

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """应用宿主界面色板，None 使用与大纲一致的默认浅色，不修改导图主题。

        非 UiColors、None 的参数抛出 TypeError；已释放面板忽略有效调用。色板同时
        覆盖树形视图、空态、滚动条和右键菜单，不依赖 QApplication 的全局样式。
        """
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        if self._disposed:
            return
        colors = outline_colors(colors or Theme())
        self._colors = colors
        apply_ui_palette(self, colors)
        self.setStyleSheet(
            f"QWidget#mindmap_submap_favorites {{background:{colors.surface};}}"
            f"QStackedWidget {{background:{colors.surface}; border:0;}}"
            f"QLabel {{background:transparent; color:{colors.muted};}}"
        )
        self.tree_view.set_tree_colors(colors)
        self._menu.setStyleSheet(menu_styles(colors))
        apply_ui_palette(self._menu, colors)
        self.remove_action.setIcon(toolbar_icon("favorite", colors.muted, colors.border, colors.accent))

    def close_popups(self) -> None:
        """关闭本面板菜单并清除待操作目标，返回 None，可重复调用。

        会话切换及释放前调用，防止旧菜单的迟到动作修改已离开的文档；不提交草稿、
        不导航、不改变收藏。该方法在 dispose() 后仍安全。
        """
        self._menu.close()
        self.remove_action.setData(None)

    def dispose(self) -> None:
        """幂等解除引擎订阅并关闭菜单，返回 None，不销毁借用的引擎或本控件。

        宿主应先调用本方法，再删除控件或释放引擎；释放后的树不再更新或发送
        导航、取消收藏请求。最终 QWidget 所有权仍由 parent 或宿主布局管理。
        """
        if self._disposed:
            return
        self._disposed = True
        self._unsubscribe()
        self.close_popups()
        self.set_editable(False)

    def _engine_changed(self, event):
        if not self._disposed and event.kind != "saved":
            self.close_popups()
            self._refresh(reset=event.kind == "reset")

    def _refresh(self, *, reset=False):
        """按稳定 ID 投影收藏树，已有分支保留展开状态，新分支默认展开。

        普通祖先与未收藏页面只参与路径和父级计算，不插入树。整份文档替换时
        清除旧浏览状态；保存事件由调用方跳过，不产生多余查询或布局。
        """
        favorites = self.engine.favorite_submaps(order="document")
        favorite_ids = {entry.page_id for entry in favorites}
        rows, parents = [], {}
        for entry in favorites:
            path = [item for item in self.engine.node_path(entry.node_id) if item.page_root]
            rows.append((entry, " › ".join(item.text.replace("\n", " ") for item in path)))
            parents[entry.page_id] = next(
                (item.page_id for item in reversed(path[:-1]) if item.page_id in favorite_ids), None)
        scroll = 0 if reset else self.tree_view.verticalScrollBar().value()
        collapsed = set() if reset else {
            page_id for page_id in self.model.branch_ids()
            if not self.tree_view.isExpanded(self.model.index_for(page_id))
        }
        structural = self.model.set_rows(tuple(rows), parents, reset=reset)
        self._stack.setCurrentWidget(self.tree_view if rows else self.empty_label)
        if structural:
            # 只对重置后的真实分支恢复状态。原叶节点首次拥有孩子时视为新分支，
            # 即使祖先已手动收起也保存它的展开状态，不强行展开上面的祖先。
            for page_id in self.model.branch_ids():
                self.tree_view.setExpanded(self.model.index_for(page_id), page_id not in collapsed)
        self.set_current_page_id(self._current_page_id)
        self.tree_view.doItemsLayout()
        self.tree_view.verticalScrollBar().setValue(scroll)

    def _navigate(self, index):
        if not self._disposed and index.isValid():
            self.navigate_requested.emit(index.data(_PAGE_ROLE))

    def _show_context_menu(self, position):
        index = self.tree_view.indexAt(position)
        if not self._disposed and index.isValid():
            self.remove_action.setData(index.data(_PAGE_ROLE))
            self.remove_action.setEnabled(self._editable)
            self._menu.popup(self.tree_view.viewport().mapToGlobal(position))

    def _unfavorite(self):
        page_id = self.remove_action.data()
        if not self._disposed and self._editable and isinstance(page_id, str):
            self.unfavorite_requested.emit(page_id)
