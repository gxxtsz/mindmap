"""完整导图大纲：树模型仅浏览结构，选择请求交给工作区执行定位。"""

from __future__ import annotations

from copy import copy
from math import ceil, floor

from mindmap_qt_ui.icons import toolbar_icon
from mindmap_qt_ui.ui_style import UiColors, apply_ui_palette, menu_styles, outline_colors, scrollbar_styles
from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QPointF,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractItemView, QMenu, QTreeView

from .contracts import PanelHost

# 所有几何尺寸均使用 Qt 逻辑像素。原生 setIndentation 和树项 sizeHint 分别
# 使用 16 与 28；正文 drawRow 使用 12 像素字号。150% 下对应 24 像素缩进、
# 42 像素行距，不能把截图物理像素直接写进界面或再次手动乘系统缩放。
_INDENTATION = 16
_ROW_HEIGHT = 28
_FONT_SIZE = 12


class OutlineModel(QAbstractItemModel):
    """用稳定节点标识建立索引，不递归复制 Qt 树项，兼容深层导图。"""

    def __init__(self, document, parent=None):
        super().__init__(parent)
        self.document = copy(document)
        self.document.nodes = document.nodes.copy()
        self.document.assets = document.assets.copy()
        self._reindex()

    def _reindex(self):
        self.parents = self.document.parents()
        # QModelIndex 的 Python 对象指针不会自动持有字符串引用。使用整数 internalId，
        # 防止引擎快照替换后旧字符串被回收，导致 Qt 在查询 parent 时访问悬空指针。
        self.identifiers = {key: index for index, key in enumerate(self.document.nodes, 1)}
        self.keys = {index: key for key, index in self.identifiers.items()}
        self.rows = {
            child: row for node in self.document.nodes.values() for row, child in enumerate(node.children)
        }
        self.rows.update({root: row for row, root in enumerate(self.document.root_ids)})
        self._next_identifier = len(self.identifiers) + 1

    def apply_changes(self, event):
        """按提交差量维护稳定标识、父级和行号；文本/折叠不扫描整棵树。

        结构变化沿用 Qt 模型重置语义，但只重建实际改变父级的索引。文档字典由
        模型独立持有，节点只读共享，避免画布原地应用差量后丢失比较基线。
        """
        delta = event.changes.collections.get("nodes", {})
        index = 1 if event.forward else 0
        structural = any(pair[0] is None or pair[1] is None or pair[0].children != pair[1].children
                         for pair in delta.values())
        fresh = {key for key, pair in delta.items() if pair[index] is not None and pair[index].children
                 and (pair[1 - index] is None or not pair[1 - index].children)}
        if structural:
            self.beginResetModel()
        try:
            event.changes.apply(self.document, forward=event.forward)
            if structural:
                for key, pair in delta.items():
                    old = pair[1 - index]
                    if old is not None:
                        for child in old.children:
                            if self.parents.get(child) == key:
                                self.parents.pop(child)
                                self.rows.pop(child, None)
                    if pair[index] is None:
                        identifier = self.identifiers.pop(key, None)
                        self.keys.pop(identifier, None)
                        self.parents.pop(key, None)
                        self.rows.pop(key, None)
                    elif key not in self.identifiers:
                        identifier = self._next_identifier
                        self._next_identifier += 1
                        self.identifiers[key] = identifier
                        self.keys[identifier] = key
                for key, pair in delta.items():
                    node = pair[index]
                    if node is not None:
                        for row, child in enumerate(node.children):
                            self.parents[child], self.rows[child] = key, row
            else:
                for key, pair in delta.items():
                    if pair[0].text != pair[1].text:
                        model_index = self.index_for(key)
                        self.dataChanged.emit(model_index, model_index,
                                              [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole])
        finally:
            if structural:
                self.endResetModel()
        return structural, fresh

    def replace_document(self, document):
        """仅在结构变化时重置模型；普通文本变化不丢失用户的树展开和滚动状态。"""
        structural = (
            document.root_ids != self.document.root_ids or document.nodes.keys() != self.document.nodes.keys()
        )
        if not structural:
            structural = any(
                node.children != self.document.nodes[key].children for key, node in document.nodes.items()
            )
        if structural:
            self.beginResetModel()
            self.document = copy(document)
            self.document.nodes = document.nodes.copy()
            self.document.assets = document.assets.copy()
            self._reindex()
            self.endResetModel()
        else:
            old = self.document
            self.document = copy(document)
            self.document.nodes = document.nodes.copy()
            self.document.assets = document.assets.copy()
            for key, node in document.nodes.items():
                if node.text != old.nodes[key].text:
                    index = self.index_for(key)
                    self.dataChanged.emit(
                        index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole]
                    )
        return structural

    def index_for(self, node_id):
        if node_id not in self.rows:
            return QModelIndex()
        return self.createIndex(self.rows[node_id], 0, self.identifiers[node_id])

    def node_id(self, index):
        return self.keys.get(index.internalId()) if index.isValid() else None

    def index(self, row, column, parent=QModelIndex()):
        if column != 0 or row < 0 or (parent.isValid() and parent.column() != 0):
            return QModelIndex()
        children = (
            self.document.nodes[self.node_id(parent)].children
            if parent.isValid()
            else self.document.root_ids
        )
        return self.index_for(children[row]) if row < len(children) else QModelIndex()

    def parent(self, index):
        key = self.parents.get(self.node_id(index)) if index.isValid() else None
        return self.index_for(key) if key else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return len(self.document.nodes[self.node_id(parent)].children) if parent.column() == 0 else 0
        return len(self.document.root_ids)

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = self.document.nodes[self.node_id(index)]
        if role == Qt.ItemDataRole.DisplayRole:
            if node.is_image_node:
                return "图片"
            return " ".join(node.text.split()) or "未命名节点"
        if role == Qt.ItemDataRole.ToolTipRole:
            return "图片" if node.is_image_node else node.text
        if role == Qt.ItemDataRole.UserRole:
            return node.id
        return None


class OutlinePanel(QTreeView):
    """完整树的独立视图；展开按钮只影响本面板，不写入文档折叠状态。"""

    nodes_selected = Signal(list)
    display_depth_changed = Signal(object)

    def __init__(self, canvas: PanelHost, parent=None, *, ui_colors: UiColors | None = None):
        """创建大纲，可在首次设置样式前指定宿主色板，不改变文档及展开偏好。"""
        if ui_colors is not None and not isinstance(ui_colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        super().__init__(parent)
        self.canvas = canvas
        self._syncing = False
        self._theme_signature = None
        self._ui_colors_override = ui_colors
        self._branch_hover_position = None
        self._display_depth = 2
        # 菜单和标题栏共用动作；动作由面板持有，宿主只创建按钮，不接管生命周期。
        self.depth_menu = QMenu(self)
        self.depth_action = QAction("2层", self)
        self.depth_action.setToolTip("显示层级数：2层")
        self.depth_action.setMenu(self.depth_menu)
        self._depth_group = QActionGroup(self)
        self._depth_actions = {}
        for depth in (None, *range(1, 10)):
            action = self.depth_menu.addAction("显示所有层" if depth is None else f"显示{depth}层")
            action.setCheckable(True)
            action.setData(depth)
            self._depth_group.addAction(action)
            self._depth_actions[depth] = action
            action.triggered.connect(lambda checked=False, value=depth: self.set_display_depth(value))
        self._depth_actions[2].setChecked(True)
        self.tree_model = OutlineModel(canvas.document_view, self)
        self.setModel(self.tree_model)
        self._expanded_ids = set()
        self.expanded.connect(lambda index: self._expanded_ids.add(self.tree_model.node_id(index)))
        self.collapsed.connect(lambda index: self._expanded_ids.discard(self.tree_model.node_id(index)))
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setAccessibleName("导图大纲")
        self.setMinimumWidth(180)
        self.setIndentation(_INDENTATION)
        font = self.font()
        font.setPixelSize(_FONT_SIZE)
        self.setFont(font)
        self.setRootIsDecorated(True)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.selectionModel().selectionChanged.connect(self._selected)
        canvas.selection_changed.connect(self.sync_selection)
        canvas.document_changed.connect(self._document_changed)
        self._apply_display_depth()
        self.sync_selection(canvas.selected_node_ids())
        self.set_theme(canvas.document_view.theme)

    @property
    def display_depth(self) -> int | None:
        """最后应用的层级预设：根主题为第1层，None 表示所有层。

        手动展开、收起不会改变此值；它是宿主应保存的偏好，而非当前展开状态的推断。
        """
        return self._display_depth

    def set_display_depth(self, depth: int | None) -> None:
        """应用1～9层或所有层；重复设置也重新应用，但只在偏好变化时发出信号。

        本接口仅改变大纲视图，不调用引擎、不导航画布；只读时也可使用。
        非法值抛出 ValueError，且保持原有状态。宿主自行通过信号持久化本机偏好。
        """
        if depth is not None and (type(depth) is not int or not 1 <= depth <= 9):
            raise ValueError("大纲显示层级必须为1～9的整数或 None")
        changed = depth != self._display_depth
        self._display_depth = depth
        self.depth_action.setText("所有层" if depth is None else f"{depth}层")
        self.depth_action.setToolTip(f"显示层级数：{self.depth_action.text()}")
        self._depth_actions[depth].setChecked(True)
        self._apply_display_depth()
        if changed:
            self.display_depth_changed.emit(depth)

    def _nodes_with_depth(self):
        """以显式栈迭代整棵树，避免深层导图触发 Python 递归上限。"""
        document = self.tree_model.document
        pending = [(root, 1) for root in reversed(document.root_ids)]
        while pending:
            key, depth = pending.pop()
            yield key, depth
            pending.extend((child, depth + 1) for child in reversed(document.nodes[key].children))

    def _apply_display_depth(self, previous=None):
        """已有分支恢复原状态，其余分支按深度初始化；省略 previous 时重套整个预设。

        只记录曾经拥有孩子的节点，因此原叶节点首次添加孩子也能正确初始化。
        显式设置隐藏分支的状态，避免手动展开父节点后残留上一次预设的深层展开。
        """
        syncing, updates = self._syncing, self.updatesEnabled()
        self._syncing = True
        self.setUpdatesEnabled(False)
        try:
            for key, depth in self._nodes_with_depth():
                if self.tree_model.document.nodes[key].children:
                    expanded = (
                        previous[key] if previous is not None and key in previous
                        else self._display_depth is None or depth < self._display_depth
                    )
                    self.setExpanded(self.tree_model.index_for(key), expanded)
        finally:
            self.setUpdatesEnabled(updates)
            self._syncing = syncing

    def _is_visible(self, key):
        """检查祖先是否全部展开；不能用 scrollTo 探测可见性，因为 Qt 会主动展开祖先。"""
        parent = self.tree_model.parents.get(key)
        while parent is not None:
            if not self.isExpanded(self.tree_model.index_for(parent)):
                return False
            parent = self.tree_model.parents.get(parent)
        return True

    def scrollTo(self, index, hint=QAbstractItemView.ScrollHint.EnsureVisible):
        """阻止 Qt 的内部自动滚动展开隐藏祖先。

        即使同步逻辑不显式调用 scrollTo，currentChanged 或延迟布局仍可能调用
        此虚函数；在统一入口拦截，确保画布选择和模型刷新都不能冲破层级预设。
        """
        key = self.tree_model.node_id(index)
        if key is not None and self._is_visible(key):
            super().scrollTo(index, hint)

    def _selected(self, selected, deselected):
        if self._syncing:
            return
        selected_ids = {self.tree_model.node_id(index) for index in self.selectionModel().selectedRows()}
        self.nodes_selected.emit(
            [
                key
                for key in self.tree_model.document.ordered_nodes()
                if key in selected_ids
            ]
        )

    def sync_selection(self, ids):
        """同步包括隐藏节点在内的选择，严格保留当前展开状态，避免选择信号循环。"""
        if self._syncing:
            return
        self._syncing = True
        try:
            self.selectionModel().clearSelection()
            for key in ids:
                if key not in self.tree_model.document.nodes:
                    continue
                index = self.tree_model.index_for(key)
                self.selectionModel().select(
                    index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
                )
            if ids and ids[0] in self.tree_model.document.nodes:
                index = self.tree_model.index_for(ids[0])
                self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                # 隐藏选择也保留 currentIndex，但只滚动第一个可见选中项。
                visible = next((key for key in ids if key in self.tree_model.document.nodes
                                and self._is_visible(key)), None)
                if visible is not None:
                    self.scrollTo(self.tree_model.index_for(visible))
        finally:
            self._syncing = False

    def _document_changed(self, event):
        """同步宿主当前页；新增分支按所选层级初始化，已有分支保留手动展开状态。

        宿主可提供增量变化或整页快照，两种通知都必须识别新分支及首次拥有孩子的
        原叶节点。只有替换文档、切页或根列表变化才对整棵树重新应用层级预设。
        """
        # 保存只改变历史保存标记，既不改变树也不改变主题；无需重新扫描所有节点。
        if event.kind == "saved":
            return
        self._syncing = True
        try:
            previous = set(self._expanded_ids)
            scroll = self.verticalScrollBar().value()
            page_changed = self.tree_model.document.root_ids != self.canvas.document_view.root_ids
            if event.changes is not None:
                changed, fresh = self.tree_model.apply_changes(event)
            else:
                document = self.canvas.document_view
                old_nodes = self.tree_model.document.nodes
                # 灵感及跨页成员变动会只通知整页快照。必须在替换模型前比较新旧节点，
                # 否则粘贴出的分支与刚获得孩子的原叶节点会一直保持重置后的收起状态，
                # 即使已选择“所有层”也不显示后代。普通增量通知仍沿用上面的差量路径。
                fresh = {key for key, node in document.nodes.items()
                         if node.children and (key not in old_nodes or not old_nodes[key].children)}
                changed = self.tree_model.replace_document(document)
            # reset 即使结构完全相同，也代表整份导图替换，必须重新应用预设。
            if event.kind in ("reset", "navigation") or page_changed:
                self._expanded_ids.clear()
                self._apply_display_depth()
                self.verticalScrollBar().setValue(scroll)
            elif changed:
                self._expanded_ids.clear()
                for key in previous:
                    node = self.tree_model.document.nodes.get(key)
                    if node is not None and node.children:
                        self.setExpanded(self.tree_model.index_for(key), True)
                for key in fresh - previous:
                    depth, current = 1, key
                    while current in self.tree_model.parents:
                        depth += 1
                        current = self.tree_model.parents[current]
                    self.setExpanded(self.tree_model.index_for(key),
                                     self._display_depth is None or depth < self._display_depth)
                self.verticalScrollBar().setValue(scroll)
        finally:
            self._syncing = False
        self.set_theme(self.canvas.document_view.theme)
        self.sync_selection(self.canvas.selected_node_ids())

    def close_popups(self) -> None:
        """关闭本大纲拥有的层级菜单；可重复调用，不改变展开状态、选择或文档。"""
        self.depth_menu.close()

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """设置独立界面色板；None 恢复当前宿主文档主题，不修改模型或选择。

        非 UiColors 参数抛出 TypeError；宿主通过此接口刷新外观，无需写入私有字段。
        """
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        self._ui_colors_override = colors
        self.set_theme(self.canvas.document_view.theme)

    def set_theme(self, theme):
        """同步整张大纲卡片共用的色板，不修改宿主、画布主题或树模型。

        标题、视口和底部圆角留白使用同一底色；文字和柔和蓝灰选中态跟随
        工作区主题，三角及叶节点使用次要文字色，保留原有层级几何和交互。
        """
        theme = self._ui_colors_override or theme
        colors = outline_colors(theme)
        if self._theme_signature == colors:
            return
        self._theme_signature = colors
        dark = QColor(colors.surface).lightnessF() < 0.5
        self._outline_colors = colors
        self._guide_color = QColor("#45474b" if dark else "#dfe2e8")
        self._branch_color = QColor(colors.muted)
        self._leaf_color = QColor(colors.muted)
        self.setStyleSheet(
            f"QTreeView {{ background: {colors.surface}; color: {colors.text}; border: none; "
            "padding: 5px 7px; outline: none; }"
            f"QTreeView::item {{ height: {_ROW_HEIGHT}px; border: none; border-radius: 4px; "
            "padding: 0 3px; margin-right: 8px; }"
            f"QTreeView::item:hover {{ background: {colors.hover}; }}"
            f"QTreeView::item:selected {{ background: {colors.selected}; color: {colors.text}; }}"
            + scrollbar_styles(colors)
        )
        apply_ui_palette(self, colors)
        # 标题入口只呈现低对比度线条图标，当前层级由悬停提示和菜单勾选表达。
        # 图标随界面色板刷新，不读取导图主题之外的全局样式，明暗宿主保持一致。
        self.depth_action.setIcon(toolbar_icon("outline_depth", colors.muted, colors.muted, colors.text))
        self.depth_menu.setStyleSheet(menu_styles(colors))
        apply_ui_palette(self.depth_menu, colors)
        self.viewport().update()

    def drawBranches(self, painter, rect, index):
        """绘制祖先竖线与本行标记，保留 QTreeView 原生的展开命中和键盘操作。

        Qt 传入的 rect 已包含当前行所有层级的分支区域，并随横向滚动移动。
        只给祖先列画贯穿整行的实线，不画横线；当前列有子节点时画三角，否则
        画灰色圆点。这样末尾子树的竖线也会延续到最后一行的下边缘，与参考一致。
        """
        indentation = self.indentation()
        if not index.isValid() or indentation <= 0 or rect.isEmpty():
            return
        painter.save()
        try:
            painter.fillRect(rect, QColor(self._outline_colors.surface))
            rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
            marker_x = rect.left() + indentation / 2 if rtl else rect.right() + 1 - indentation / 2
            marker_y = rect.top() + rect.height() / 2
            ancestors = max(0, rect.width() // indentation - 1)
            first_x = marker_x + indentation if rtl else rect.left() + indentation / 2

            # 深层大纲的分支矩形可能远宽于视口。按可见范围裁剪列号，不逐个遍历
            # 离屏祖先，也不递归访问模型；开销只与当前能看到的导线数量有关。
            visible = rect.intersected(self.viewport().rect())
            first = max(0, ceil((visible.left() - first_x) / indentation))
            stop = min(ancestors, floor((visible.right() - first_x) / indentation) + 1)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            # 先把细线宽度取整到设备像素再换回逻辑尺寸，防止 150% 时的 1.5
            # 设备像素被光栅器截成单像素，造成导线比参考图明显偏细。
            ratio = painter.device().devicePixelRatioF()
            guide_width = max(1, floor(ratio + 0.5)) / ratio
            painter.setPen(QPen(self._guide_color, guide_width))
            for column in range(first, stop):
                x = first_x + column * indentation
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom() + 1))

            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            if self.model().hasChildren(index):
                painter.setBrush(self._branch_color)
                hover = self._branch_hover_position
                hovered = (
                    hover is not None
                    and rect.contains(hover)
                    and abs(hover.x() - marker_x) < indentation / 2
                )
                if self.isExpanded(index):
                    points = ((-3.5, -2.5), (3.5, -2.5), (0, 3)) if hovered else (
                        (-2.5, -1.5), (2.5, -1.5), (0, 2)
                    )
                else:
                    direction = -1 if rtl else 1
                    points = (
                        ((-2.5 * direction, -3.5), (-2.5 * direction, 3.5), (3 * direction, 0))
                        if hovered else
                        ((-1.5 * direction, -2.5), (-1.5 * direction, 2.5), (2 * direction, 0))
                    )
                painter.drawPolygon(QPolygonF([QPointF(marker_x + x, marker_y + y) for x, y in points]))
            else:
                painter.setBrush(self._leaf_color)
                painter.drawEllipse(QPointF(marker_x, marker_y), 2, 2)
        finally:
            painter.restore()

    def viewportEvent(self, event):
        """记录分支悬停位置，只重画涉及的行，事件仍由 Qt 处理选择和展开。

        使用视口坐标而不是保存 QModelIndex：树结构变化、滚动或重排之后，
        鼠标仍指向屏幕上的同一个位置，不会把旧节点的悬停状态带到其他行。
        """
        if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Leave):
            previous = self._branch_hover_position
            self._branch_hover_position = (
                event.position().toPoint() if event.type() == QEvent.Type.MouseMove else None
            )
            if previous != self._branch_hover_position:
                for position in (previous, self._branch_hover_position):
                    if position is not None:
                        rect = self.visualRect(self.indexAt(position))
                        if rect.isValid():
                            self.viewport().update(QRect(0, rect.top(), self.viewport().width(), rect.height()))
        return super().viewportEvent(event)
