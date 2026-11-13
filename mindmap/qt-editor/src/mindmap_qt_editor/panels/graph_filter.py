"""停靠侧栏中的连线筛选，修改和临时预览都交给活动画布。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mindmap_qt_editor.theme import UiColors, apply_ui_palette

from ._sidebar_tree import SidebarTreeView


class _FilterCheckDelegate(QStyledItemDelegate):
    """共用原生复选框的键鼠命中，按侧栏色板绘制可辨认的深浅主题边框。"""

    def paint(self, painter, option, index):
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        super().paint(painter, item, index)
        if item.widget is None:
            return
        rect = item.widget.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, item, item.widget)
        if rect.isEmpty():
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            checked = item.checkState == Qt.CheckState.Checked
            accent = item.palette.color(QPalette.ColorRole.Link)
            muted = item.palette.color(QPalette.ColorRole.PlaceholderText)
            painter.setPen(QPen(accent if checked else muted))
            painter.setBrush(accent if checked else item.palette.base())
            painter.drawRoundedRect(rect.adjusted(1, 1, -2, -2), 2, 2)
            if checked:
                ink = QColor("#111827" if accent.lightnessF() > 0.6 else "#ffffff")
                painter.setPen(QPen(ink, 1.6))
                center = rect.center()
                painter.drawLine(center.x() - 3, center.y(), center.x() - 1, center.y() + 2)
                painter.drawLine(center.x() - 1, center.y() + 2, center.x() + 3, center.y() - 2)
        finally:
            painter.restore()


class GraphFilterPanel(QWidget):
    """单个停靠面板跟随活动图页。节点列表用稳定标识关联，切换标签先解绑旧信号。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("graph_filter_panel")
        self.setAccessibleName("连线筛选")
        self.setAutoFillBackground(True)
        self._canvas = None
        self._rows = {}
        self._labels = ()
        self._syncing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)
        controls = QHBoxLayout()
        controls.setContentsMargins(7, 0, 7, 0)
        self.bulk = QToolButton(self)
        self.bulk.setText("隐藏全部连线")
        self.bulk.clicked.connect(self._toggle_all)
        self.preview = QToolButton(self)
        self.preview.setText("临时显示全部")
        self.preview.setCheckable(True)
        self.preview.clicked.connect(self._toggle_preview)
        controls.addWidget(self.bulk)
        controls.addWidget(self.preview)
        controls.addStretch()
        layout.addLayout(controls)
        self.empty = QLabel("图中暂无节点", self)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)
        self.tree = SidebarTreeView(self)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setAccessibleName("按节点筛选连线")
        self.tree.setAccessibleDescription("勾选节点后隐藏与其相连的入边和出边")
        self.model = QStandardItemModel(self.tree)
        self.tree.setModel(self.model)
        self.tree.setItemDelegate(_FilterCheckDelegate(self.tree))
        self.model.itemChanged.connect(self._set_checked)
        self.setFocusProxy(self.tree)
        layout.addWidget(self.tree, 1)
        self._sync()

    def set_ui_colors(self, colors: UiColors) -> None:
        """沿用大纲和收藏的行样式及宿主色板，保留焦点和勾选状态。"""
        apply_ui_palette(self, colors, window=colors.surface)
        self.tree.set_tree_colors(colors)
        self.empty.setStyleSheet(f"color:{colors.muted}; padding:8px;")

    def bind_canvas(self, canvas) -> None:
        """接管当前活动图页；无图或后台标签不会驱动筛选侧栏。"""
        if canvas is not self._canvas:
            if self._canvas is not None:
                for signal in (self._canvas.document_changed, self._canvas.editable_changed,
                               self._canvas.editing_changed, self._canvas.graph_preview_changed):
                    signal.disconnect(self._sync)
            self._canvas = canvas
            if canvas is not None:
                for signal in (canvas.document_changed, canvas.editable_changed,
                               canvas.editing_changed, canvas.graph_preview_changed):
                    signal.connect(self._sync)
        self._sync()

    def _sync(self, *events):
        """只有节点列表变化才重建行，其余变化原地更新勾选与按钮权限。"""
        if events and getattr(events[0], "kind", None) == "saved":
            return
        canvas = self._canvas
        graph = canvas.graph if canvas is not None else None
        labels = tuple((node.id, node.title) for node in graph.nodes) if graph is not None else ()
        hidden = graph.hidden_node_ids if graph is not None else frozenset()
        can_edit = bool(canvas and canvas.editable and not canvas.is_editing)
        if labels != self._labels:
            # 插入行须发出模型结构信号，让停靠面板可立即更新。勾选回显在下方单独阻断。
            self._labels = labels
            self.model.removeRows(0, self.model.rowCount())
            self._rows.clear()
            for key, title in labels:
                item = QStandardItem(" ".join(title.splitlines()) or "（空标题）")
                item.setEditable(False)
                item.setCheckable(True)
                item.setData(key, Qt.ItemDataRole.UserRole)
                item.setToolTip(f"{title}\n节点标识：{key}")
                self.model.appendRow(item)
                self._rows[key] = item
        self._syncing = True
        try:
            for key, item in self._rows.items():
                item.setCheckState(Qt.CheckState.Checked if key in hidden else Qt.CheckState.Unchecked)
                flags = item.flags()
                item.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable if can_edit else
                              flags & ~Qt.ItemFlag.ItemIsUserCheckable)
        finally:
            self._syncing = False
        self.empty.setVisible(not labels)
        self.tree.setVisible(bool(labels))
        all_hidden = bool(labels) and len(hidden) == len(labels)
        self.bulk.setText("清除筛选" if all_hidden else "隐藏全部连线")
        self.bulk.setEnabled(bool(can_edit and labels))
        show_all = bool(canvas and canvas.graph_shows_all_edges)
        self.preview.setChecked(show_all)
        self.preview.setText("恢复筛选" if show_all else "临时显示全部")
        self.preview.setEnabled(bool(graph and hidden and graph.edges))

    def _toggle_all(self):
        canvas = self._canvas
        if canvas is not None and (graph := canvas.graph) is not None:
            ids = {node.id for node in graph.nodes}
            canvas.call(lambda: canvas.set_graph_hidden_nodes(set() if graph.hidden_node_ids == ids else ids))
            self._sync()

    def _set_checked(self, item):
        if self._syncing:
            return
        canvas = self._canvas
        if canvas is not None and (graph := canvas.graph) is not None:
            key = item.data(Qt.ItemDataRole.UserRole)
            hidden = graph.hidden_node_ids
            wanted = hidden | {key} if item.checkState() == Qt.CheckState.Checked else hidden - {key}
            if wanted != hidden:
                canvas.call(lambda: canvas.set_graph_hidden_nodes(wanted))
            self._sync()

    def _toggle_preview(self, checked):
        if self._canvas is not None:
            self._canvas.set_graph_show_all_edges(checked)
