"""最近页面的只读列表；宿主提供摘要及当前页，面板仅发出导航意图。"""

from collections.abc import Sequence

from mindmap_core import PageSummary, Theme
from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from mindmap_qt_editor.icons import toolbar_icon
from mindmap_qt_editor.theme import UiColors, apply_ui_palette, outline_colors, scrollbar_styles

_ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
_KINDS = {"main": "主导图", "submap": "子导图", "detail": "详情页", "inspiration": "独立想法"}
_ICONS = {"main": "outline", "submap": "submap", "detail": "detail", "inspiration": "inspiration"}


def _text(value: str) -> str:
    """把多行正文压缩为列表文字，空标题仍提供可辨认的中文占位。"""
    return " ".join(value.split()) or "（空主题）"


def _subtitle(entry: PageSummary) -> str:
    return f"{_KINDS[entry.kind]} · " + " › ".join(_text(item.text) for item in entry.path)


class _RecentPagesModel(QAbstractListModel):
    """只持有不可变摘要；标题更新不重置索引，顺序变化由面板按稳定键恢复焦点。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.rows: tuple[PageSummary, ...] = ()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.model() is not self or not 0 <= index.row() < len(self.rows):
            return None
        entry = self.rows[index.row()]
        if role == _ENTRY_ROLE:
            return entry
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return f"{_text(entry.title)}\n{_subtitle(entry)}"
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            return f"{entry.title or '（空主题）'}\n{_subtitle(entry)}"
        return None

    def index_for(self, key):
        """按页面身份找当前索引；不能把重排前的行号用于后续导航。"""
        return next((self.index(row) for row, entry in enumerate(self.rows) if entry.key == key), QModelIndex())

    def set_entries(self, entries):
        if entries == self.rows:
            return
        structural = tuple(item.key for item in entries) != tuple(item.key for item in self.rows)
        if structural:
            self.beginResetModel()
        self.rows = entries
        if structural:
            self.endResetModel()
        elif entries:
            self.dataChanged.emit(self.index(0), self.index(len(entries) - 1))


class _RecentPagesDelegate(QStyledItemDelegate):
    """按现有灵感侧栏的双行样式绘制，活动页高亮与键盘待访问项分别表达。"""

    def sizeHint(self, option, index):
        return QSize(180, 58)

    def paint(self, painter, option, index):
        entry = index.data(_ENTRY_ROLE)
        if entry is None:
            return
        panel = self.parent()
        colors, current = panel._colors, entry.key == panel.current_key
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(option.rect)
            rect = QRect(option.rect)
            rect.setRight(min(rect.right(), panel.list_view.viewport().rect().right()))
            rect.adjust(3, 2, -3, -2)
            if current or option.state & QStyle.StateFlag.State_MouseOver:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors.selected if current else colors.hover))
                painter.drawRoundedRect(rect, 5, 5)
            toolbar_icon(_ICONS[entry.kind], colors.accent if current else colors.muted,
                         colors.muted, colors.accent).paint(painter, QRect(rect.left() + 8, rect.top() + 10, 16, 16))
            area = rect.adjusted(32, 7, -8, -6)
            font = painter.font()
            font.setBold(current)
            painter.setFont(font)
            painter.setPen(QColor(colors.text))
            title = painter.fontMetrics().elidedText(_text(entry.title), Qt.TextElideMode.ElideRight,
                                                     max(0, area.width()))
            painter.drawText(QRect(area.left(), area.top(), area.width(), 20), Qt.AlignmentFlag.AlignVCenter, title)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(colors.muted))
            subtitle = painter.fontMetrics().elidedText(_subtitle(entry), Qt.TextElideMode.ElideRight,
                                                        max(0, area.width()))
            painter.drawText(QRect(area.left(), area.top() + 22, area.width(), 18),
                             Qt.AlignmentFlag.AlignVCenter, subtitle)
            if option.state & QStyle.StateFlag.State_HasFocus:
                painter.setPen(QPen(QColor(colors.accent), 1, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
        finally:
            painter.restore()


class _RecentPagesList(QListView):
    """方向键只浏览，回车才请求导航；双击不再次打开重排后占据同一位置的另一行。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.panel._navigate(self.currentIndex())
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        event.accept()


class RecentPagesPanel(QWidget):
    """最近访问面板，不依赖画布、引擎实例或停靠框架。

    宿主通过 set_entries 提供已验证摘要，set_current_page 同步实际当前页。
    navigate_requested(page_id, inspiration_root_id) 仅表达意图；宿主负责输入
    检查和导航，并在失败时重新同步实际当前页。全部方法仅在 Qt GUI 线程调用。
    """

    navigate_requested = Signal(object, object)

    def __init__(self, parent: QWidget | None = None):
        """创建列表和中文空态；parent 管理 Qt 所有权，永久关闭前调用 dispose。"""
        super().__init__(parent)
        self._disposed = False
        self.current_key = None
        self.setObjectName("mindmap_recent_pages")
        self.setAccessibleName("最近访问")
        # 自定义 QWidget 显式绘制样式背景，避免原生主题在内容边缘透出系统底色。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        # 与收藏侧栏一样铺满内容区；原有 5 像素留白交给列表自身绘制，避免形成灰边。
        layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)
        self.empty_label = QLabel("暂无访问记录\n\n访问导图页面后，可在这里快速切换。", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setMargin(17)
        self._stack.addWidget(self.empty_label)
        self.list_view = _RecentPagesList(self)
        self.list_view.setAccessibleName("最近访问页面列表")
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setMouseTracking(True)
        self.model = _RecentPagesModel(self)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(_RecentPagesDelegate(self))
        self.list_view.clicked.connect(self._navigate)
        self._stack.addWidget(self.list_view)
        self.setFocusProxy(self.list_view)
        self.set_ui_colors(None)

    def set_entries(self, entries: Sequence[PageSummary]) -> None:
        """更新只读摘要，保留键盘所选页面和滚动位置，不发送导航请求。

        宿主按最近访问顺序传入摘要；面板不自行排序、校验文档或持久化。
        复制为元组避免后续容器修改，摘要本身不可变；释放后忽略更新。
        """
        if self._disposed:
            return
        selected = self.list_view.currentIndex().data(_ENTRY_ROLE)
        scroll = self.list_view.verticalScrollBar().value()
        self.model.set_entries(tuple(entries))
        self._stack.setCurrentWidget(self.list_view if entries else self.empty_label)
        self.list_view.setCurrentIndex(self.model.index_for(selected.key if selected else self.current_key))
        self.list_view.verticalScrollBar().setValue(scroll)

    def set_current_page(self, page_id: str | None, inspiration_root_id: str | None = None,
                         *, reset_selection: bool = False) -> None:
        """同步真实当前页的高亮；导航失败可强制还原选择，普通刷新保留方向键浏览。

        主导图使用 (None, None)。未列入历史的页面不高亮其他条目；所有状态同步
        均不发送导航信号，也不滚动列表或影响宿主画布。
        """
        if self._disposed:
            return
        key = page_id, inspiration_root_id
        changed = key != self.current_key
        self.current_key = key
        if changed or reset_selection:
            scroll = self.list_view.verticalScrollBar().value()
            self.list_view.setCurrentIndex(self.model.index_for(key))
            self.list_view.verticalScrollBar().setValue(scroll)
        self.list_view.viewport().update()

    def set_ui_colors(self, colors: UiColors | None) -> None:
        """沿用收藏侧栏的背景色板，None 使用相同的默认浅色。

        外壳、堆叠页、列表及原生视口的 Window／Base 统一使用侧栏底色，防止
        空态、滚动及主题切换后暴露系统灰边；列表内部留白保持原有条目位置。
        """
        if colors is not None and not isinstance(colors, UiColors):
            raise TypeError("界面色板必须为 UiColors 或 None")
        if self._disposed:
            return
        self._colors = colors = outline_colors(colors or Theme())
        for widget in (self, self._stack, self.list_view, self.list_view.viewport()):
            apply_ui_palette(widget, colors)
        self.setStyleSheet(
            f"QWidget#mindmap_recent_pages, QStackedWidget, QListView {{background:{colors.surface}; border:0;}}"
            f"QLabel {{background:transparent; color:{colors.muted};}}"
            "QListView {padding:5px; outline:none;}" + scrollbar_styles(colors)
        )
        self.list_view.viewport().update()

    def dispose(self) -> None:
        """幂等停止更新与导航请求；控件仍由宿主删除，不释放任何文档。"""
        self._disposed = True

    def _navigate(self, index):
        """在发信号前捕获稳定目标，宿主置顶并重排后不再读取旧索引。"""
        entry = index.data(_ENTRY_ROLE)
        if not self._disposed and entry is not None:
            self.navigate_requested.emit(*entry.key)
