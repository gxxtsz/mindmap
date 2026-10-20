"""完整导图大纲：树模型仅浏览结构，选择请求交给工作区执行定位。"""

from __future__ import annotations

from math import ceil, floor

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
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractItemView, QTreeView

from .ui_style import apply_ui_palette, outline_colors, scrollbar_styles

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
        self.document = document
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
        self.rows[self.document.root_id] = 0

    def replace_document(self, document):
        """仅在结构变化时重置模型；普通文本变化不丢失用户的树展开和滚动状态。"""
        structural = (
            document.root_id != self.document.root_id or document.nodes.keys() != self.document.nodes.keys()
        )
        if not structural:
            structural = any(
                node.children != self.document.nodes[key].children for key, node in document.nodes.items()
            )
        if structural:
            self.beginResetModel()
            self.document = document
            self._reindex()
            self.endResetModel()
        else:
            old = self.document
            self.document = document
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
            else [self.document.root_id]
        )
        return self.index_for(children[row]) if row < len(children) else QModelIndex()

    def parent(self, index):
        key = self.parents.get(self.node_id(index)) if index.isValid() else None
        return self.index_for(key) if key else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return len(self.document.nodes[self.node_id(parent)].children) if parent.column() == 0 else 0
        return 1

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = self.document.nodes[self.node_id(index)]
        if role == Qt.ItemDataRole.DisplayRole:
            return " ".join(node.text.split()) or "未命名节点"
        if role == Qt.ItemDataRole.ToolTipRole:
            return node.text
        if role == Qt.ItemDataRole.UserRole:
            return node.id
        return None


class OutlinePanel(QTreeView):
    """完整树的独立视图；展开按钮只影响本面板，不写入文档折叠状态。"""

    nodes_selected = Signal(list)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._syncing = False
        self._theme_signature = None
        self._branch_hover_position = None
        self.tree_model = OutlineModel(canvas.engine.document, self)
        self.setModel(self.tree_model)
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
        self.expand(self.tree_model.index_for(canvas.engine.root_id))
        self.sync_selection(canvas.selected_node_ids())
        self.set_theme(canvas.engine.document.theme)

    def _selected(self, selected, deselected):
        if self._syncing:
            return
        selected_ids = {self.tree_model.node_id(index) for index in self.selectionModel().selectedRows()}
        self.nodes_selected.emit(
            [
                key
                for key in self.tree_model.document.descendants(self.tree_model.document.root_id)
                if key in selected_ids
            ]
        )

    def sync_selection(self, ids):
        """画布到大纲只展开大纲祖先，不反向请求导航，避免选择信号循环。"""
        if self._syncing:
            return
        self._syncing = True
        try:
            self.selectionModel().clearSelection()
            for key in ids:
                if key not in self.tree_model.document.nodes:
                    continue
                parent = self.tree_model.parents.get(key)
                while parent is not None:
                    self.expand(self.tree_model.index_for(parent))
                    parent = self.tree_model.parents.get(parent)
                index = self.tree_model.index_for(key)
                self.selectionModel().select(
                    index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
                )
            if ids and ids[0] in self.tree_model.document.nodes:
                index = self.tree_model.index_for(ids[0])
                self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                self.scrollTo(index)
        finally:
            self._syncing = False

    def _document_changed(self, event):
        self._syncing = True
        try:
            expanded = {
                key
                for key in self.tree_model.document.nodes
                if self.isExpanded(self.tree_model.index_for(key))
            }
            scroll = self.verticalScrollBar().value()
            changed = self.tree_model.replace_document(self.canvas.engine.document)
            if changed:
                if event.kind == "reset":
                    expanded = {self.canvas.engine.root_id}
                for key in expanded:
                    if key in self.tree_model.document.nodes:
                        self.expand(self.tree_model.index_for(key))
                self.verticalScrollBar().setValue(scroll)
        finally:
            self._syncing = False
        self.set_theme(self.canvas.engine.document.theme)
        self.sync_selection(self.canvas.selected_node_ids())

    def set_theme(self, theme):
        """同步整张大纲卡片共用的色板，不修改宿主、画布主题或树模型。

        标题、视口和底部圆角留白使用同一底色；文字和柔和蓝灰选中态跟随
        工作区主题，三角及叶节点使用次要文字色，保留原有层级几何和交互。
        """
        theme = getattr(self, "_ui_colors_override", None) or theme
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
