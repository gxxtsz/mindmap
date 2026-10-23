"""子导图收藏列表：只读查询引擎，以信号把导航和内容修改交回宿主。"""

from __future__ import annotations

from mindmap_core import MindMapEngine, Theme
from mindmap_qt_ui.icons import toolbar_icon
from mindmap_qt_ui.ui_style import UiColors, apply_ui_palette, menu_styles, scrollbar_styles, ui_colors
from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListView,
    QMenu,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

_PAGE_ROLE = Qt.ItemDataRole.UserRole + 1
_PATH_ROLE = Qt.ItemDataRole.UserRole + 2
_CURRENT_ROLE = Qt.ItemDataRole.UserRole + 3


class _FavoritesModel(QAbstractListModel):
    """仅持有不可变路径项及路径文字，不借用引擎内部文档、节点或页面记录。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.rows = ()
        self.current_page_id = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        entry, path = self.rows[index.row()]
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

    def set_rows(self, rows):
        if rows != self.rows:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()


class _FavoriteDelegate(QStyledItemDelegate):
    """用两行省略文字绘制收藏项；所有尺寸使用 Qt 逻辑像素，由 Qt 适配高 DPI。"""

    def sizeHint(self, option, index):
        return QSize(180, 58)

    def paint(self, painter, option, index):
        colors = self.parent()._colors
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = option.rect.adjusted(3, 2, -3, -2)
            current = bool(index.data(_CURRENT_ROLE))
            if current or option.state & QStyle.StateFlag.State_MouseOver:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors.selected if current else colors.hover))
                painter.drawRoundedRect(rect, 5, 5)
            icon = toolbar_icon("submap", colors.accent if current else colors.muted,
                                colors.muted, colors.accent)
            icon.paint(painter, QRect(rect.left() + 8, rect.top() + 10, 16, 16))
            text_rect = rect.adjusted(32, 7, -8, -6)
            font = option.font
            font.setBold(current)
            painter.setFont(font)
            painter.setPen(QColor(colors.text))
            title = painter.fontMetrics().elidedText(index.data(), Qt.TextElideMode.ElideRight,
                                                     max(0, text_rect.width()))
            painter.drawText(QRect(text_rect.left(), text_rect.top(), text_rect.width(), 20),
                             Qt.AlignmentFlag.AlignVCenter, title)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(colors.muted))
            path = painter.fontMetrics().elidedText(index.data(_PATH_ROLE), Qt.TextElideMode.ElideMiddle,
                                                    max(0, text_rect.width()))
            painter.drawText(QRect(text_rect.left(), text_rect.top() + 22, text_rect.width(), 18),
                             Qt.AlignmentFlag.AlignVCenter, path)
            if option.state & QStyle.StateFlag.State_HasFocus:
                painter.setPen(QPen(QColor(colors.accent), 1, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
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
        """为借用的 engine 建立独立列表，parent 决定 Qt 控件所有权。

        构造不会改写收藏或产生历史；内容按收藏时间倒序查询，缺少收藏时显示中文
        操作提示。永久关闭会话前必须调用 dispose() 解除引擎订阅，再由宿主销毁控件。
        """
        super().__init__(parent)
        self.engine = engine
        self._disposed = False
        self._editable = True
        self._current_page_id = None
        self._colors = ui_colors(Theme())
        self.setObjectName("mindmap_submap_favorites")
        self.setAccessibleName("子导图收藏")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)
        self.empty_label = QLabel("暂无收藏\n\n在子导图路径栏点击星标，\n或右键入口节点收藏子导图。", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setMargin(12)
        self._stack.addWidget(self.empty_label)
        self.list_view = QListView(self)
        self.list_view.setAccessibleName("已收藏的子导图")
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setMouseTracking(True)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.model = _FavoritesModel(self)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(_FavoriteDelegate(self))
        self.list_view.clicked.connect(self._navigate)
        self.list_view.activated.connect(self._navigate)
        self._stack.addWidget(self.list_view)
        self._menu = QMenu(self)
        self.remove_action = QAction("取消收藏", self)
        self.remove_action.triggered.connect(self._unfavorite)
        self._menu.addAction(self.remove_action)
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        self._unsubscribe = engine.subscribe(self._engine_changed)
        self.set_ui_colors(None)
        self._refresh()

    def set_current_page_id(self, page_id: str | None) -> None:
        """同步当前显示页并高亮对应收藏，返回 None，不触发导航或修改。

        主导图使用 None，详情页或未收藏页面会清除当前收藏高亮；无效 ID 同样视为
        未命中。该调用不会自动滚动列表，保留用户浏览位置；释放后调用无操作。
        """
        if self._disposed:
            return
        scroll = self.list_view.verticalScrollBar().value()
        self._current_page_id = page_id
        self.model.current_page_id = page_id
        index = next((self.model.index(row, 0) for row, (entry, _) in enumerate(self.model.rows)
                      if entry.page_id == page_id), QModelIndex())
        self.list_view.setCurrentIndex(index)
        if not index.isValid():
            self.list_view.clearSelection()
        self.list_view.verticalScrollBar().setValue(scroll)
        self.list_view.viewport().update()

    def set_editable(self, editable: bool) -> None:
        """同步取消收藏入口的可用状态，返回 None，列表浏览与导航仍可使用。

        宿主应传入只读及正文编辑约束合并后的布尔值；本接口只控制界面，实际修改
        仍由宿主在收到 unfavorite_requested 后再次校验，避免菜单期间权限发生变化。
        """
        self._editable = bool(editable) and not self._disposed
        self.remove_action.setEnabled(self._editable)

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """应用宿主界面色板，None 使用默认浅色，返回 None，不修改导图主题。

        非 UiColors、None 的参数抛出 TypeError；已释放面板忽略有效调用。色板同时
        覆盖列表、空态、滚动条和右键菜单，不依赖 QApplication 的全局样式。
        """
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        if self._disposed:
            return
        self._colors = colors or ui_colors(Theme())
        colors = self._colors
        apply_ui_palette(self, colors)
        apply_ui_palette(self.list_view, colors)
        self.setStyleSheet(
            f"QWidget#mindmap_submap_favorites {{background:{colors.surface};}}"
            f"QStackedWidget, QListView {{background:{colors.surface}; border:0; outline:0;}}"
            f"QLabel {{background:transparent; color:{colors.muted};}}" + scrollbar_styles(colors))
        self._menu.setStyleSheet(menu_styles(colors))
        apply_ui_palette(self._menu, colors)
        self.remove_action.setIcon(toolbar_icon("favorite", colors.muted, colors.border, colors.accent))
        self.list_view.viewport().update()

    def close_popups(self) -> None:
        """关闭本面板菜单并清除待操作目标，返回 None，可重复调用。

        会话切换及释放前调用，防止旧菜单的迟到动作修改已离开的文档；不提交草稿、
        不导航、不改变收藏。该方法在 dispose() 后仍安全。
        """
        self._menu.close()
        self.remove_action.setData(None)

    def dispose(self) -> None:
        """幂等解除引擎订阅并关闭菜单，返回 None，不销毁借用的引擎或本控件。

        宿主应先调用本方法，再删除控件或释放引擎；释放后的列表不再更新或发送
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
            self._refresh()

    def _refresh(self):
        # 页面根投影与路径栏保持一致；普通祖先节点不混入页面层级。
        rows = tuple((entry, " › ".join(item.text.replace("\n", " ")
                                       for item in self.engine.node_path(entry.node_id) if item.page_root))
                     for entry in self.engine.favorite_submaps())
        scroll = self.list_view.verticalScrollBar().value()
        self.model.set_rows(rows)
        self._stack.setCurrentWidget(self.list_view if rows else self.empty_label)
        self.set_current_page_id(self._current_page_id)
        self.list_view.doItemsLayout()
        self.list_view.verticalScrollBar().setValue(scroll)

    def _navigate(self, index):
        if not self._disposed and index.isValid():
            self.navigate_requested.emit(index.data(_PAGE_ROLE))

    def _show_context_menu(self, position):
        index = self.list_view.indexAt(position)
        if not self._disposed and index.isValid():
            self.remove_action.setData(index.data(_PAGE_ROLE))
            self.remove_action.setEnabled(self._editable)
            self._menu.popup(self.list_view.viewport().mapToGlobal(position))

    def _unfavorite(self):
        page_id = self.remove_action.data()
        if not self._disposed and self._editable and isinstance(page_id, str):
            self.unfavorite_requested.emit(page_id)
